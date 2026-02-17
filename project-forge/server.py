#!/usr/bin/env python3
"""Project Forge server — serves pre-generated gallery and manages background worker.

Endpoints:
  GET  /                                              → index.html
  GET  /healthz                                       → health check
  GET  /forge/projects?bonfire_id=X                   → all projects (current versions)
  GET  /forge/projects/{id}?bonfire_id=X              → single project with all versions
  GET  /forge/status?bonfire_id=X                     → worker status + poll log
  GET  /forge/mockups/{bf}/{id}/v{ver}/{file}         → serve mockup HTML file
  GET  /forge/mockups/{bf}/{id}/latest/{file}         → alias for current version
  POST /forge/trigger?bonfire_id=X                    → force immediate poll cycle
  GET  /api/*                                         → proxy to Bonfires API (CORS bypass)
  POST /api/*                                         → proxy to Bonfires API (CORS bypass)

Usage:
  python3 server.py
"""

import http.server
import json
import logging
import os
import socketserver
import urllib.parse
import urllib.request
import urllib.error
from pathlib import Path

from worker import ForgeWorker

PORT = int(os.environ.get("PORT", 9999))
API_BASE = os.environ.get("DELVE_BASE_URL", "https://tnt-v2.api.bonfires.ai")
FORGE_DIR = Path(__file__).parent

log = logging.getLogger("forge.server")

# Shared worker instance
worker = ForgeWorker()

# In-memory current bonfire tracker
current_bonfire_id: str | None = None


def _update_current_bonfire(bonfire_id: str):
    """Update the global current bonfire and notify the worker."""
    global current_bonfire_id
    current_bonfire_id = bonfire_id
    worker.set_current_bonfire(bonfire_id)


def _validate_public_bonfire(bonfire_id: str) -> bool:
    """Check if bonfire_id is a public bonfire via the Bonfires API.

    Returns True if valid, or True on API failure (best-effort mode).
    """
    try:
        req = urllib.request.Request(
            f"{API_BASE}/bonfires",
            headers={"Content-Type": "application/json"},
            method="GET",
        )
        with urllib.request.urlopen(req, timeout=10) as resp:
            data = json.loads(resp.read())
        bonfires = data if isinstance(data, list) else data.get("bonfires", [])
        for bf in bonfires:
            bf_id = bf.get("id") or bf.get("_id") or bf.get("bonfire_id", "")
            if str(bf_id) == bonfire_id:
                return True
        return False
    except Exception as exc:
        log.warning("Could not validate bonfire %s: %s — allowing (best-effort)", bonfire_id, exc)
        return True


def _restore_current_bonfire():
    """Restore current_bonfire_id from the most recently modified state file."""
    global current_bonfire_id
    best_mtime = 0.0
    best_bid: str | None = None
    for f in FORGE_DIR.glob("forge_state_*.json"):
        bid = f.stem[len("forge_state_"):]
        if bid and f.stat().st_mtime > best_mtime:
            best_mtime = f.stat().st_mtime
            best_bid = bid
    if best_bid:
        current_bonfire_id = best_bid
        worker.set_current_bonfire(best_bid)
        print(f"  [server] Restored current_bonfire_id={best_bid}")


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

    def _parse_bonfire_id(self) -> str | None:
        """Extract bonfire_id from query string."""
        parsed = urllib.parse.urlparse(self.path)
        qs = urllib.parse.parse_qs(parsed.query)
        values = qs.get("bonfire_id", [])
        return values[0] if values else None

    def _strip_path(self) -> str:
        """Return path without query string."""
        return urllib.parse.urlparse(self.path).path

    # -- Routing --
    def do_GET(self):
        path = self._strip_path()

        if path == "/":
            self.path = "/index.html"
            path = "/index.html"

        if path == "/healthz":
            self._json_response(200, {"status": "ok"})
        elif path == "/forge/projects":
            self._handle_projects_list()
        elif path.startswith("/forge/projects/"):
            self._handle_project_detail()
        elif path.startswith("/forge/mockups/"):
            self._serve_mockup()
        elif path == "/forge/status":
            bonfire_id = self._parse_bonfire_id()
            if bonfire_id:
                if not _validate_public_bonfire(bonfire_id):
                    self._json_response(403, {"error": f"Bonfire '{bonfire_id}' is not public"})
                    return
                _update_current_bonfire(bonfire_id)
            self._json_response(200, worker.get_status(bonfire_id))
        elif path.startswith("/api/"):
            self._proxy_api("GET")
        else:
            super().do_GET()

    def do_POST(self):
        path = self._strip_path()

        if path == "/forge/trigger":
            self._handle_trigger()
        elif path.startswith("/api/"):
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
        bonfire_id = self._parse_bonfire_id()
        if bonfire_id:
            if not _validate_public_bonfire(bonfire_id):
                self._json_response(403, {"error": f"Bonfire '{bonfire_id}' is not public"})
                return
            _update_current_bonfire(bonfire_id)

        state = worker.load_state(bonfire_id)
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
        bonfire_id = self._parse_bonfire_id()
        if bonfire_id:
            if not _validate_public_bonfire(bonfire_id):
                self._json_response(403, {"error": f"Bonfire '{bonfire_id}' is not public"})
                return
            _update_current_bonfire(bonfire_id)

        path = self._strip_path()
        parts = path.rstrip("/").split("/")
        if len(parts) < 4:
            self.send_error(404)
            return
        project_id = parts[3]

        state = worker.load_state(bonfire_id)
        for p in state.get("projects", []):
            if p["id"] == project_id:
                self._json_response(200, p)
                return
        self._json_response(404, {"error": f"Project '{project_id}' not found"})

    def _handle_trigger(self):
        """Force an immediate poll cycle, optionally for a specific bonfire."""
        bonfire_id = self._parse_bonfire_id()
        if bonfire_id:
            if not _validate_public_bonfire(bonfire_id):
                self._json_response(403, {"error": f"Bonfire '{bonfire_id}' is not public"})
                return
            _update_current_bonfire(bonfire_id)
            worker.trigger_now(bonfire_id)
        elif current_bonfire_id:
            worker.trigger_now(current_bonfire_id)
        else:
            self._json_response(400, {"error": "No bonfire_id provided and no current bonfire set"})
            return
        self._json_response(202, {"status": "triggered", "bonfire_id": bonfire_id or current_bonfire_id})

    def _serve_mockup(self):
        """Serve mockup HTML files.

        Paths:
          /forge/mockups/{bonfire_id}/{project_id}/v{version}/{filename}
          /forge/mockups/{bonfire_id}/{project_id}/latest/{filename}
        """
        path = self._strip_path()
        parts = path.split("/")
        # parts = ['', 'forge', 'mockups', 'bonfire-id', 'project-id', 'v1', 'index.html']
        if len(parts) < 7:
            self.send_error(404)
            return

        bonfire_id = parts[3]
        project_id = parts[4]
        version_part = parts[5]
        filename = "/".join(parts[6:])

        if version_part == "latest":
            state = worker.load_state(bonfire_id)
            for p in state.get("projects", []):
                if p["id"] == project_id:
                    version_part = f"v{p['current_version']}"
                    break

        mockup_path = FORGE_DIR / "mockups" / bonfire_id / project_id / version_part / filename

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
    def _proxy_api(self, method: str):
        api_path = urllib.parse.urlparse(self.path).path
        target = API_BASE + api_path[4:]
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

    # Restore current bonfire from most recent state file
    _restore_current_bonfire()

    # Start the background worker
    worker.start()

    with socketserver.TCPServer(("0.0.0.0", PORT), ForgeHandler) as httpd:
        bonfire_display = current_bonfire_id or "(none — waiting for selection)"
        print(f"""
  ╔══════════════════════════════════════════╗
  ║         PROJECT FORGE  v2               ║
  ╚══════════════════════════════════════════╝

  Server:     http://localhost:{PORT}
  Bonfire:    {bonfire_display}
  Gallery:    /forge/projects?bonfire_id=...
  Status:     /forge/status?bonfire_id=...
  Trigger:    POST /forge/trigger?bonfire_id=...
  API proxy:  /api/* → {API_BASE}

  Worker polling every {worker.lock and 'started' or 'idle'}
  Open:       http://localhost:{PORT}/
""")
        try:
            httpd.serve_forever()
        except KeyboardInterrupt:
            worker.stop()
            print("\n  Shutting down.")
