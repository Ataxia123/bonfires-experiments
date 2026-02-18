#!/usr/bin/env python3
"""Kindling Bonfires server — HTTP API, MongoDB persistence, background pipeline runs.

Endpoints:
  GET  /                              → index.html
  GET  /healthz                       → health check
  POST /kindle/run                   → start pipeline, return {run_id}
  GET  /kindle/run/{run_id}          → poll run status (UI-safe truncated view)
  GET  /kindle/history?donor_id=&applicant_id=  → runs for pair
  GET  /kindle/history/recent?limit=5 → recent runs with agreement_preview

Uses ThreadingTCPServer so poll requests are served while pipeline runs.
"""

import asyncio
import json
import os
import threading
import urllib.parse
import uuid
from datetime import datetime, timezone
from pathlib import Path

import http.server
import socketserver

import pymongo

KINDLING_DIR = Path(__file__).parent
PORT = int(os.environ.get("PORT", "9998"))
MAX_TRUNCATE = 50
AGREEMENT_PREVIEW_LEN = 120


def _get_mongo_collection():
    """Connect to MongoDB and return bonfire_agreements collection. Fail fast if env missing."""
    mongo_uri = os.environ.get("MONGO_URI")
    mongo_db_name = os.environ.get("MONGO_DB_NAME")
    if not mongo_uri:
        raise SystemExit("MONGO_URI environment variable is required")
    if not mongo_db_name:
        raise SystemExit("MONGO_DB_NAME environment variable is required")
    client = pymongo.MongoClient(mongo_uri)
    db = client[mongo_db_name]
    coll = db["bonfire_agreements"]
    coll.create_index("run_id", unique=True)
    coll.create_index([("donor_id", 1), ("applicant_id", 1), ("started_at", -1)])
    return coll


def _truncate_run_for_ui(doc: dict) -> dict:
    """Truncate entities, episodes, edges in each step to MAX_TRUNCATE for response size."""
    if not doc:
        return doc
    out = dict(doc)
    steps = out.get("steps") or []
    truncated_steps = []
    for step in steps:
        s = dict(step)
        for key in ("entities", "episodes", "edges"):
            if key in s and isinstance(s[key], list) and len(s[key]) > MAX_TRUNCATE:
                s[key] = s[key][:MAX_TRUNCATE]
        truncated_steps.append(s)
    out["steps"] = truncated_steps
    return out


def _run_pipeline(run_id: str, donor_id: str, applicant_id: str, collection: pymongo.collection.Collection) -> None:
    """Run kindling_graph in a daemon thread. Updates MongoDB on completion or failure."""
    from kindling_graph import kindling_graph

    try:
        state = {
            "run_id": run_id,
            "donor_id": donor_id,
            "applicant_id": applicant_id,
            "mongo_collection": collection,
        }
        result = asyncio.run(kindling_graph.ainvoke(state))
        agreement = (result.get("formal_agreement") or "").strip()
        collection.update_one(
            {"run_id": run_id},
            {
                "$set": {
                    "status": "completed",
                    "formal_agreement": agreement,
                    "completed_at": _now_iso(),
                }
            },
        )
    except Exception as e:
        collection.update_one(
            {"run_id": run_id},
            {
                "$set": {"status": "failed", "completed_at": _now_iso()},
                "$push": {"errors": str(e)},
            },
        )


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


class KindlingHandler(http.server.SimpleHTTPRequestHandler):
    def __init__(self, *args, collection: pymongo.collection.Collection | None = None, **kwargs):
        self._collection = collection
        super().__init__(*args, directory=str(KINDLING_DIR), **kwargs)

    def _cors_headers(self) -> None:
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type, Authorization")

    def do_OPTIONS(self) -> None:
        self.send_response(200)
        self._cors_headers()
        self.send_header("Access-Control-Max-Age", "600")
        self.end_headers()

    def _strip_path(self) -> str:
        return urllib.parse.urlparse(self.path).path

    def _json_response(self, status: int, data: dict) -> None:
        body = json.dumps(data).encode()
        self.send_response(status)
        self._cors_headers()
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self) -> None:
        path = self._strip_path()

        if path == "/":
            self.path = "/index.html"
            path = "/index.html"

        if path == "/healthz":
            self._json_response(200, {"status": "ok"})
            return
        if path.startswith("/kindle/run/"):
            run_id = path.split("/kindle/run/", 1)[-1].split("/")[0].strip()
            if run_id:
                self._handle_get_run(run_id)
                return
        if path == "/kindle/history/recent":
            self._handle_history_recent()
            return
        if path == "/kindle/history":
            self._handle_history()
            return

        super().do_GET()

    def do_POST(self) -> None:
        path = self._strip_path()
        if path == "/kindle/run":
            self._handle_post_run()
            return
        self.send_error(404)

    def _handle_get_run(self, run_id: str) -> None:
        coll = self._collection
        if not coll:
            self._json_response(500, {"error": "Database not configured"})
            return
        doc = coll.find_one({"run_id": run_id})
        if not doc:
            self._json_response(404, {"error": f"Run '{run_id}' not found"})
            return
        # Remove MongoDB _id for JSON serialization; use run_id as identifier
        out = {k: v for k, v in doc.items() if k != "_id"}
        self._json_response(200, _truncate_run_for_ui(out))

    def _handle_history(self) -> None:
        parsed = urllib.parse.urlparse(self.path)
        qs = urllib.parse.parse_qs(parsed.query)
        donor_id = (qs.get("donor_id") or [""])[0].strip()
        applicant_id = (qs.get("applicant_id") or [""])[0].strip()
        coll = self._collection
        if not coll:
            self._json_response(500, {"error": "Database not configured"})
            return
        filter_query = {}
        if donor_id:
            filter_query["donor_id"] = donor_id
        if applicant_id:
            filter_query["applicant_id"] = applicant_id
        cursor = coll.find(filter_query).sort("started_at", -1)
        runs = []
        for doc in cursor:
            runs.append({
                "run_id": doc.get("run_id"),
                "donor_id": doc.get("donor_id"),
                "applicant_id": doc.get("applicant_id"),
                "status": doc.get("status"),
                "started_at": doc.get("started_at"),
                "completed_at": doc.get("completed_at"),
                "agreement_preview": (doc.get("formal_agreement") or "")[:AGREEMENT_PREVIEW_LEN],
            })
        self._json_response(200, {"runs": runs})

    def _handle_history_recent(self) -> None:
        parsed = urllib.parse.urlparse(self.path)
        qs = urllib.parse.parse_qs(parsed.query)
        try:
            limit = int((qs.get("limit") or ["5"])[0])
        except (ValueError, TypeError):
            limit = 5
        limit = max(1, min(limit, 100))
        coll = self._collection
        if not coll:
            self._json_response(500, {"error": "Database not configured"})
            return
        cursor = coll.find({}).sort("started_at", -1).limit(limit)
        runs = []
        for doc in cursor:
            runs.append({
                "run_id": doc.get("run_id"),
                "donor_id": doc.get("donor_id"),
                "applicant_id": doc.get("applicant_id"),
                "status": doc.get("status"),
                "started_at": doc.get("started_at"),
                "agreement_preview": (doc.get("formal_agreement") or "")[:AGREEMENT_PREVIEW_LEN],
            })
        self._json_response(200, {"runs": runs})

    def _handle_post_run(self) -> None:
        coll = self._collection
        if not coll:
            self._json_response(500, {"error": "Database not configured"})
            return
        content_length = int(self.headers.get("Content-Length", 0))
        if content_length <= 0:
            self._json_response(400, {"error": "Request body required"})
            return
        try:
            body = self.rfile.read(content_length).decode()
            data = json.loads(body)
        except (json.JSONDecodeError, UnicodeDecodeError) as e:
            self._json_response(400, {"error": f"Invalid JSON: {e}"})
            return
        donor_id = (data.get("donor_id") or "").strip()
        applicant_id = (data.get("applicant_id") or "").strip()
        if not donor_id or not applicant_id:
            self._json_response(400, {"error": "donor_id and applicant_id are required"})
            return
        run_id = str(uuid.uuid4())
        started_at = _now_iso()
        doc = {
            "run_id": run_id,
            "donor_id": donor_id,
            "applicant_id": applicant_id,
            "status": "running",
            "started_at": started_at,
            "completed_at": None,
            "formal_agreement": None,
            "steps": [],
            "errors": [],
        }
        coll.insert_one(doc)
        thread = threading.Thread(
            target=_run_pipeline,
            args=(run_id, donor_id, applicant_id, coll),
            daemon=True,
        )
        thread.start()
        self._json_response(200, {"run_id": run_id})

    def log_message(self, fmt: str, *args: object) -> None:
        path = str(args[0]) if args else ""
        if "/healthz" in path or "favicon" in path:
            return
        print(f"  [{self.command}] {path}")


def _handler_factory(collection: pymongo.collection.Collection):
    """Build a handler class that has the collection bound."""
    class Handler(KindlingHandler):
        def __init__(self, *args, **kwargs):
            super().__init__(*args, collection=collection, **kwargs)
    return Handler


if __name__ == "__main__":
    collection = _get_mongo_collection()
    Handler = _handler_factory(collection)
    socketserver.ThreadingTCPServer.allow_reuse_address = True

    with socketserver.ThreadingTCPServer(("0.0.0.0", PORT), Handler) as httpd:
        print(f"""
  Kindling Bonfires server
  http://localhost:{PORT}
  POST /kindle/run   GET /kindle/run/{{id}}   GET /kindle/history
""")
        try:
            httpd.serve_forever()
        except KeyboardInterrupt:
            print("\n  Shutting down.")
