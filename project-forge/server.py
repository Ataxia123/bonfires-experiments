#!/usr/bin/env python3
"""Project Forge server — serves pre-generated gallery and manages background worker.

Endpoints:
  GET  /                                     → index.html
  GET  /healthz                              → health check
  GET  /forge/projects                       → all projects (current versions)
  GET  /forge/projects/{id}                  → single project with all versions
  GET  /forge/status                         → worker status + poll log
  GET  /forge/mockups/{id}/v{ver}/{file}     → serve mockup HTML file
  GET  /forge/mockups/{id}/latest/{file}     → alias for current version
  POST /forge/trigger                        → force immediate poll cycle
  POST /api/*                                → proxy to Bonfires API (CORS bypass)

Usage:
  python3 server.py
"""

import http.server
import json
import os
import socketserver
import urllib.request
import urllib.error
from pathlib import Path

from worker import ForgeWorker

PORT = int(os.environ.get("PORT", 9999))
API_BASE = os.environ.get("DELVE_BASE_URL", "https://tnt-v2.api.bonfires.ai")
FORGE_DIR = Path(__file__).parent

# Shared worker instance
worker = ForgeWorker()


class ForgeHandler(http.server.SimpleHTTPRequestHandler):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, directory=str(FORGE_DIR), **kwargs)

    # -- CORS --
    def _cors_headers(self):
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type, Authorization")

    def do_OPTIONS(self):
        self.send_response(200)
        self._cors_headers()
        self.send_header("Access-Control-Max-Age", "600")
        self.end_headers()

    # -- Routing --
    def do_GET(self):
        if self.path == "/":
            self.path = "/index.html"

        if self.path == "/healthz":
            self._json_response(200, {"status": "ok"})
        elif self.path == "/forge/projects":
            self._handle_projects_list()
        elif self.path.startswith("/forge/projects/"):
            self._handle_project_detail()
        elif self.path.startswith("/forge/mockups/"):
            self._serve_mockup()
        elif self.path == "/forge/status":
            self._json_response(200, worker.get_status())
        else:
            super().do_GET()

    def do_POST(self):
        if self.path == "/forge/trigger":
            worker.trigger_now()
            self._json_response(202, {"status": "triggered"})
        elif self.path.startswith("/api/"):
            self._proxy_api("POST")
        else:
            self.send_error(404)

    # -- JSON helpers --
    def _json_response(self, status: int, data):
        body = json.dumps(data).encode()
        self.send_response(status)
        self._cors_headers()
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    # -- Gallery endpoints --
    def _handle_projects_list(self):
        """Return all projects with their current (latest) version data."""
        state = worker.load_state()
        projects_out = []
        for p in state.get("projects", []):
            if p.get("retired_at"):
                continue
            versions = p.get("versions", [])
            if not versions:
                continue
            latest = versions[-1]
            projects_out.append({
                "id": p["id"],
                "current_version": p["current_version"],
                "version_count": len(versions),
                "created_at": p.get("created_at"),
                "updated_at": p.get("updated_at"),
                "project_data": latest.get("project_data", {}),
                "mockup_dir": latest.get("mockup_dir", ""),
                "mockup_files": latest.get("mockup_files", []),
            })
        self._json_response(200, {
            "projects": projects_out,
            "last_generation_at": state.get("last_generation_at"),
            "generation_count": state.get("generation_count", 0),
        })

    def _handle_project_detail(self):
        """Return a single project with all its versions."""
        # Parse: /forge/projects/{id}
        parts = self.path.rstrip("/").split("/")
        if len(parts) < 4:
            self.send_error(404)
            return
        project_id = parts[3]

        state = worker.load_state()
        for p in state.get("projects", []):
            if p["id"] == project_id:
                self._json_response(200, p)
                return
        self._json_response(404, {"error": f"Project '{project_id}' not found"})

    def _serve_mockup(self):
        """Serve mockup HTML files.

        Paths:
          /forge/mockups/{project_id}/v{version}/{filename}
          /forge/mockups/{project_id}/latest/{filename}
        """
        # Parse path: /forge/mockups/project-id/v1/index.html
        parts = self.path.split("/")
        # parts = ['', 'forge', 'mockups', 'project-id', 'v1', 'index.html']
        if len(parts) < 6:
            self.send_error(404)
            return

        project_id = parts[3]
        version_part = parts[4]
        filename = "/".join(parts[5:])  # handle nested paths

        # Resolve "latest" to actual version number
        if version_part == "latest":
            state = worker.load_state()
            for p in state.get("projects", []):
                if p["id"] == project_id:
                    version_part = f"v{p['current_version']}"
                    break

        mockup_path = FORGE_DIR / "mockups" / project_id / version_part / filename

        if mockup_path.exists() and mockup_path.is_file():
            body = mockup_path.read_bytes()
            self.send_response(200)
            self._cors_headers()
            content_type = "text/html; charset=utf-8"
            if filename.endswith(".json"):
                content_type = "application/json"
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        else:
            self.send_error(404)

    # -- API Proxy --
    def _proxy_api(self, method):
        target = API_BASE + self.path[4:]
        content_length = int(self.headers.get("Content-Length", 0))
        body = self.rfile.read(content_length) if content_length else None
        headers = {}
        for key in ("Content-Type", "Authorization", "X-API-Key"):
            val = self.headers.get(key)
            if val:
                headers[key] = val

        req = urllib.request.Request(target, data=body, headers=headers, method=method)
        try:
            with urllib.request.urlopen(req, timeout=60) as resp:
                resp_body = resp.read()
                self.send_response(resp.status)
                self._cors_headers()
                self.send_header("Content-Type", resp.headers.get("Content-Type", "application/json"))
                self.end_headers()
                self.wfile.write(resp_body)
        except urllib.error.HTTPError as exc:
            resp_body = exc.read()
            self.send_response(exc.code)
            self._cors_headers()
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(resp_body)
        except Exception as exc:
            self._json_response(502, {"error": str(exc)})

    # Suppress default logging noise
    def log_message(self, fmt, *args):
        path = str(args[0]) if args else ""
        if "/healthz" in path or "/favicon" in path:
            return
        print(f"  [{self.command}] {path}")


if __name__ == "__main__":
    socketserver.TCPServer.allow_reuse_address = True

    # Start the background worker
    worker.start()

    with socketserver.TCPServer(("0.0.0.0", PORT), ForgeHandler) as httpd:
        print(f"""
  ╔══════════════════════════════════════════╗
  ║         PROJECT FORGE  v2               ║
  ║         EthBoulder 2026                 ║
  ╚══════════════════════════════════════════╝

  Server:     http://localhost:{PORT}
  Gallery:    /forge/projects
  Status:     /forge/status
  Trigger:    POST /forge/trigger
  API proxy:  /api/* → {API_BASE}

  Worker polling every {worker.lock and 'started' or 'idle'}
  Open:       http://localhost:{PORT}/
""")
        try:
            httpd.serve_forever()
        except KeyboardInterrupt:
            worker.stop()
            print("\n  Shutting down.")
