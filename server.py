#!/usr/bin/env python3
"""Local server that serves the HTML files AND proxies API calls to Bonfires.

Serves static files from this directory on port 8888.
Proxies any request to /api/* → https://tnt-v2.api.bonfires.ai/*
This avoids CORS issues entirely since the browser only talks to localhost.

Usage:
    python3 server.py
    # Then open http://localhost:8888/pulse.html
"""

import http.server
import json
import socketserver
import urllib.request
import urllib.error
from pathlib import Path

PORT = 8888
API_BASE = "https://tnt-v2.api.bonfires.ai"


class ProxyHandler(http.server.SimpleHTTPRequestHandler):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, directory=str(Path(__file__).parent), **kwargs)

    def do_OPTIONS(self):
        """Handle CORS preflight."""
        self.send_response(200)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, PUT, DELETE, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type, Authorization")
        self.send_header("Access-Control-Max-Age", "600")
        self.end_headers()

    def do_GET(self):
        if self.path.startswith("/api/"):
            self._proxy("GET")
        else:
            super().do_GET()

    def do_POST(self):
        if self.path.startswith("/api/"):
            self._proxy("POST")
        else:
            self.send_error(405)

    def _proxy(self, method):
        # /api/delve → https://tnt-v2.api.bonfires.ai/delve
        target = API_BASE + self.path[4:]  # strip "/api"

        # Read request body
        content_length = int(self.headers.get("Content-Length", 0))
        body = self.rfile.read(content_length) if content_length else None

        # Forward headers
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
                self.send_header("Access-Control-Allow-Origin", "*")
                self.send_header("Content-Type", resp.headers.get("Content-Type", "application/json"))
                self.end_headers()
                self.wfile.write(resp_body)
        except urllib.error.HTTPError as exc:
            resp_body = exc.read()
            self.send_response(exc.code)
            self.send_header("Access-Control-Allow-Origin", "*")
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(resp_body)
        except Exception as exc:
            self.send_response(502)
            self.send_header("Access-Control-Allow-Origin", "*")
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(json.dumps({"error": str(exc)}).encode())


if __name__ == "__main__":
    # Kill any existing server on this port
    socketserver.TCPServer.allow_reuse_address = True

    with socketserver.TCPServer(("", PORT), ProxyHandler) as httpd:
        print(f"\n  Bonfires local server running on http://localhost:{PORT}")
        print(f"  Static files: {Path(__file__).parent}")
        print(f"  API proxy:    /api/* → {API_BASE}/*")
        print(f"\n  Open: http://localhost:{PORT}/pulse.html")
        print(f"  Open: http://localhost:{PORT}/memory-explorer.html\n")
        try:
            httpd.serve_forever()
        except KeyboardInterrupt:
            print("\n  Shutting down.")
