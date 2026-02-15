#!/usr/bin/env python3
"""Project Forge server — serves the UI, proxies to Bonfires API, and runs forge jobs.

Endpoints:
  GET  /                     → index.html
  POST /api/*                → proxy to Bonfires API (CORS bypass)
  POST /forge/themes         → extract themes from KG
  POST /forge/synthesize     → generate project ideas (Claude Agent SDK)
  POST /forge/mockup         → generate HTML mockup for a project
  POST /forge/scaffold       → scaffold a full project (returns job ID)
  GET  /forge/jobs/{id}      → check scaffold job status

Usage:
  python3 server.py
  open http://localhost:9999
"""

import asyncio
import http.server
import json
import os
import socketserver
import threading
import traceback
import urllib.request
import urllib.error
import uuid
from pathlib import Path

PORT = int(os.environ.get("PORT", 9999))
API_BASE = os.environ.get("DELVE_BASE_URL", "https://tnt-v2.api.bonfires.ai")
FORGE_DIR = Path(__file__).parent

# Job tracking for async scaffold operations
jobs: dict[str, dict] = {}


class ForgeHandler(http.server.SimpleHTTPRequestHandler):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, directory=str(FORGE_DIR), **kwargs)

    # -- CORS --
    def _cors_headers(self):
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, PUT, DELETE, OPTIONS")
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
        elif self.path.startswith("/forge/jobs/"):
            self._handle_job_status()
        elif self.path.startswith("/forge/mockups/"):
            self._serve_mockup()
        else:
            super().do_GET()

    def do_POST(self):
        if self.path.startswith("/api/"):
            self._proxy_api("POST")
        elif self.path == "/forge/themes":
            self._handle_themes()
        elif self.path == "/forge/synthesize":
            self._handle_synthesize()
        elif self.path == "/forge/mockup":
            self._handle_mockup()
        elif self.path == "/forge/scaffold":
            self._handle_scaffold()
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

    # -- Forge endpoints --
    def _read_json_body(self) -> dict:
        content_length = int(self.headers.get("Content-Length", 0))
        if content_length == 0:
            return {}
        body = self.rfile.read(content_length)
        return json.loads(body)

    def _json_response(self, status: int, data: dict):
        body = json.dumps(data).encode()
        self.send_response(status)
        self._cors_headers()
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _handle_themes(self):
        """Extract themes from the KG — synchronous, takes ~10-20s."""
        try:
            from forge import extract_themes
            data = extract_themes()
            # Cache for reuse
            cache_path = FORGE_DIR / "themes_cache.json"
            cache_path.write_text(json.dumps(data, indent=2))
            self._json_response(200, data)
        except Exception as exc:
            traceback.print_exc()
            self._json_response(500, {"error": str(exc)})

    def _handle_synthesize(self):
        """Generate project ideas — async via Claude Agent SDK."""
        try:
            body = self._read_json_body()
            selected_themes = body.get("themes", None)

            # Load themes (from cache or body)
            themes_data = body.get("themes_data")
            if not themes_data:
                cache_path = FORGE_DIR / "themes_cache.json"
                if cache_path.exists():
                    themes_data = json.loads(cache_path.read_text())
                else:
                    from forge import extract_themes
                    themes_data = extract_themes()

            from forge import synthesize_projects
            result = asyncio.run(synthesize_projects(themes_data, selected_themes))

            # Cache
            cache_path = FORGE_DIR / "projects_cache.json"
            cache_path.write_text(json.dumps(result, indent=2))

            self._json_response(200, result)
        except Exception as exc:
            traceback.print_exc()
            self._json_response(500, {"error": str(exc)})

    def _handle_mockup(self):
        """Generate an HTML mockup for a specific project."""
        try:
            body = self._read_json_body()
            project = body.get("project")
            if not project:
                self._json_response(400, {"error": "Missing 'project' in body"})
                return

            from forge import generate_mockup
            html = asyncio.run(generate_mockup(project))

            # Save the mockup
            safe_name = project["name"].lower().replace(" ", "-").replace("/", "-")[:40]
            mockup_dir = FORGE_DIR / "mockups"
            mockup_dir.mkdir(exist_ok=True)
            filename = f"{safe_name}.html"
            (mockup_dir / filename).write_text(html)

            self._json_response(200, {
                "html": html,
                "filename": filename,
                "url": f"/forge/mockups/{filename}",
            })
        except Exception as exc:
            traceback.print_exc()
            self._json_response(500, {"error": str(exc)})

    def _handle_scaffold(self):
        """Queue a scaffold job — returns job ID immediately."""
        try:
            body = self._read_json_body()
            project = body.get("project")
            if not project:
                self._json_response(400, {"error": "Missing 'project' in body"})
                return

            job_id = str(uuid.uuid4())[:8]
            safe_name = project["name"].lower().replace(" ", "-").replace("/", "-")[:40]
            output_dir = str(FORGE_DIR / "scaffolds" / safe_name)

            jobs[job_id] = {
                "status": "running",
                "project": project["name"],
                "output_dir": output_dir,
                "files": [],
                "error": None,
            }

            # Run scaffold in background thread
            def run_scaffold():
                from forge import scaffold_project
                try:
                    files = asyncio.run(scaffold_project(project, output_dir))
                    jobs[job_id]["files"] = files
                    jobs[job_id]["status"] = "completed"
                except Exception as exc:
                    jobs[job_id]["status"] = "failed"
                    jobs[job_id]["error"] = str(exc)
                    traceback.print_exc()

            thread = threading.Thread(target=run_scaffold, daemon=True)
            thread.start()

            self._json_response(202, {"job_id": job_id, "status": "running"})
        except Exception as exc:
            traceback.print_exc()
            self._json_response(500, {"error": str(exc)})

    def _handle_job_status(self):
        """Check on a scaffold job."""
        job_id = self.path.split("/")[-1]
        if job_id in jobs:
            self._json_response(200, {"job_id": job_id, **jobs[job_id]})
        else:
            self._json_response(404, {"error": f"Job {job_id} not found"})

    def _serve_mockup(self):
        """Serve a generated mockup file."""
        filename = self.path.split("/")[-1]
        mockup_path = FORGE_DIR / "mockups" / filename
        if mockup_path.exists():
            body = mockup_path.read_bytes()
            self.send_response(200)
            self._cors_headers()
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        else:
            self.send_error(404)

    # Suppress default logging noise
    def log_message(self, format, *args):
        if "/forge/" in (args[0] if args else ""):
            print(f"  [forge] {args[0]}")


if __name__ == "__main__":
    socketserver.TCPServer.allow_reuse_address = True
    with socketserver.TCPServer(("0.0.0.0", PORT), ForgeHandler) as httpd:
        print(f"""
  ╔══════════════════════════════════════════╗
  ║         PROJECT FORGE                    ║
  ║         EthBoulder 2026                  ║
  ╚══════════════════════════════════════════╝

  Server:  http://localhost:{PORT}
  API:     /api/* → {API_BASE}
  Forge:   /forge/themes, /forge/synthesize,
           /forge/mockup, /forge/scaffold

  Open:    http://localhost:{PORT}/
""")
        try:
            httpd.serve_forever()
        except KeyboardInterrupt:
            print("\n  Shutting down.")
