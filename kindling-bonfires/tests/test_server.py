"""Tests for Kindling Bonfires server — endpoints, MongoDB interaction, truncation."""

import http.client
import json
import socket
import socketserver
import sys
import threading
import time
from pathlib import Path
from unittest.mock import patch

import pytest

KINDLING_DIR = Path(__file__).parent.parent
sys.path.insert(0, str(KINDLING_DIR))

import server


# ---------------------------------------------------------------------------
# In-memory fake MongoDB collection
# ---------------------------------------------------------------------------


class _FakeCursor:
    """Cursor-like object: sort(key, direction) and limit(n) return self; iteration yields docs."""

    def __init__(self, docs: list[dict]) -> None:
        self._docs = list(docs)
        self._sort_key: str | None = None
        self._sort_asc = True
        self._limit: int | None = None

    def sort(self, key: str, direction: int) -> "_FakeCursor":
        self._sort_key = key
        self._sort_asc = direction == 1
        return self

    def limit(self, n: int) -> "_FakeCursor":
        self._limit = n
        return self

    def __iter__(self):
        out = list(self._docs)
        if self._sort_key is not None:
            out.sort(key=lambda d: d.get(self._sort_key, ""), reverse=not self._sort_asc)
        if self._limit is not None:
            out = out[: self._limit]
        return iter(out)


class FakeCollection:
    """In-memory collection for testing: insert_one, find_one, update_one, find."""

    def __init__(self) -> None:
        self._docs: list[dict] = []

    def insert_one(self, doc: dict) -> None:
        self._docs.append(dict(doc))

    def find_one(self, query: dict) -> dict | None:
        for d in reversed(self._docs):
            if all(d.get(k) == v for k, v in query.items()):
                return dict(d)
        return None

    def update_one(self, query: dict, update: dict) -> None:
        for i, d in enumerate(self._docs):
            if all(d.get(k) == v for k, v in query.items()):
                doc = dict(self._docs[i])
                if "$set" in update:
                    doc.update(update["$set"])
                if "$push" in update:
                    for key, val in update["$push"].items():
                        doc.setdefault(key, []).append(val)
                self._docs[i] = doc
                return
        raise ValueError(f"No document matched query {query}")

    def find(
        self,
        query: dict | None = None,
    ) -> _FakeCursor:
        q = query or {}
        out = [d for d in self._docs if all(d.get(k) == v for k, v in q.items())]
        return _FakeCursor(out)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _start_server(port: int, collection: FakeCollection) -> socketserver.ThreadingTCPServer:
    Handler = server._handler_factory(collection)
    socketserver.ThreadingTCPServer.allow_reuse_address = True
    httpd = socketserver.ThreadingTCPServer(("127.0.0.1", port), Handler)
    t = threading.Thread(target=httpd.serve_forever, daemon=True)
    t.start()
    time.sleep(0.15)
    return httpd


def _get(port: int, path: str) -> tuple[int, dict]:
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
    conn.request("GET", path)
    resp = conn.getresponse()
    body = resp.read()
    conn.close()
    try:
        return resp.status, json.loads(body)
    except json.JSONDecodeError:
        return resp.status, {"_raw": body.decode(errors="replace")}


def _post(port: int, path: str, body: dict | None = None) -> tuple[int, dict]:
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
    payload = json.dumps(body).encode() if body else b""
    headers = {"Content-Type": "application/json"} if body else {}
    conn.request("POST", path, body=payload, headers=headers)
    resp = conn.getresponse()
    raw = resp.read()
    conn.close()
    try:
        return resp.status, json.loads(raw)
    except json.JSONDecodeError:
        return resp.status, {"_raw": raw.decode(errors="replace")}


@pytest.fixture()
def fake_coll() -> FakeCollection:
    return FakeCollection()


@pytest.fixture()
def live_server(fake_coll: FakeCollection) -> tuple[int, FakeCollection, socketserver.ThreadingTCPServer]:
    port = _free_port()
    httpd = _start_server(port, fake_coll)
    yield port, fake_coll, httpd
    httpd.shutdown()


# ---------------------------------------------------------------------------
# Health
# ---------------------------------------------------------------------------


class TestHealthz:
    def test_healthz_returns_ok(self, live_server: tuple[int, FakeCollection, socketserver.ThreadingTCPServer]) -> None:
        port, _, _ = live_server
        status, data = _get(port, "/healthz")
        assert status == 200
        assert data == {"status": "ok"}


# ---------------------------------------------------------------------------
# POST /kindle/run
# ---------------------------------------------------------------------------


class TestPostRun:
    def test_post_run_returns_run_id_and_inserts_doc(
        self,
        live_server: tuple[int, FakeCollection, socketserver.ThreadingTCPServer],
    ) -> None:
        port, coll, _ = live_server
        with patch.object(server, "_run_pipeline"):
            status, data = _post(port, "/kindle/run", {"donor_id": "bf_donor", "applicant_id": "bf_app"})
        assert status == 200
        assert "run_id" in data
        run_id = data["run_id"]
        doc = coll.find_one({"run_id": run_id})
        assert doc is not None
        assert doc["donor_id"] == "bf_donor"
        assert doc["applicant_id"] == "bf_app"
        assert doc["status"] == "running"
        assert "started_at" in doc
        assert doc["steps"] == []
        assert doc["errors"] == []

    def test_post_run_requires_donor_and_applicant(
        self,
        live_server: tuple[int, FakeCollection, socketserver.ThreadingTCPServer],
    ) -> None:
        port, _, _ = live_server
        status, data = _post(port, "/kindle/run", {})
        assert status == 400
        assert "donor_id" in (data.get("error") or "").lower() or "required" in (data.get("error") or "").lower()

    def test_post_run_rejects_empty_ids(
        self,
        live_server: tuple[int, FakeCollection, socketserver.ThreadingTCPServer],
    ) -> None:
        port, _, _ = live_server
        status, _ = _post(port, "/kindle/run", {"donor_id": "  ", "applicant_id": "bf_app"})
        assert status == 400


# ---------------------------------------------------------------------------
# GET /kindle/run/{run_id}
# ---------------------------------------------------------------------------


class TestGetRun:
    def test_get_run_returns_doc_with_truncated_arrays(
        self,
        live_server: tuple[int, FakeCollection, socketserver.ThreadingTCPServer],
    ) -> None:
        port, coll, _ = live_server
        # Insert a run with steps that have >50 entities/episodes/edges
        big_list = [{"name": f"e{i}", "uuid": f"u{i}"} for i in range(60)]
        coll.insert_one({
            "run_id": "run-123",
            "donor_id": "d",
            "applicant_id": "a",
            "status": "completed",
            "started_at": "2025-01-01T00:00:00Z",
            "completed_at": "2025-01-01T00:01:00Z",
            "formal_agreement": "We agree.",
            "steps": [
                {
                    "step": 1,
                    "name": "read_applicant_bonfire",
                    "entities": big_list,
                    "episodes": big_list,
                    "edges": big_list,
                }
            ],
            "errors": [],
        })
        status, data = _get(port, "/kindle/run/run-123")
        assert status == 200
        assert data["run_id"] == "run-123"
        assert data["status"] == "completed"
        steps = data.get("steps") or []
        assert len(steps) == 1
        assert len(steps[0]["entities"]) == 50
        assert len(steps[0]["episodes"]) == 50
        assert len(steps[0]["edges"]) == 50

    def test_get_run_404_when_missing(
        self,
        live_server: tuple[int, FakeCollection, socketserver.ThreadingTCPServer],
    ) -> None:
        port, _, _ = live_server
        status, data = _get(port, "/kindle/run/nonexistent-id")
        assert status == 404
        assert "not found" in (data.get("error") or "").lower()


# ---------------------------------------------------------------------------
# GET /kindle/history
# ---------------------------------------------------------------------------


class TestHistory:
    def test_history_pair_returns_filtered_sorted(
        self,
        live_server: tuple[int, FakeCollection, socketserver.ThreadingTCPServer],
    ) -> None:
        port, coll, _ = live_server
        coll.insert_one({
            "run_id": "r1", "donor_id": "d1", "applicant_id": "a1",
            "status": "completed", "started_at": "2025-01-01T10:00:00Z",
            "formal_agreement": "Agreement one",
        })
        coll.insert_one({
            "run_id": "r2", "donor_id": "d1", "applicant_id": "a1",
            "status": "completed", "started_at": "2025-01-01T11:00:00Z",
            "formal_agreement": "Agreement two",
        })
        coll.insert_one({
            "run_id": "r3", "donor_id": "d2", "applicant_id": "a2",
            "status": "running", "started_at": "2025-01-01T12:00:00Z",
            "formal_agreement": None,
        })
        status, data = _get(port, "/kindle/history?donor_id=d1&applicant_id=a1")
        assert status == 200
        runs = data.get("runs") or []
        assert len(runs) == 2
        assert runs[0]["run_id"] == "r2"
        assert runs[1]["run_id"] == "r1"
        assert "agreement_preview" in runs[0]

    def test_history_recent_returns_agreement_preview(
        self,
        live_server: tuple[int, FakeCollection, socketserver.ThreadingTCPServer],
    ) -> None:
        port, coll, _ = live_server
        long_agreement = "A" * 200
        coll.insert_one({
            "run_id": "r1", "donor_id": "d1", "applicant_id": "a1",
            "status": "completed", "started_at": "2025-01-01T10:00:00Z",
            "formal_agreement": long_agreement,
        })
        status, data = _get(port, "/kindle/history/recent?limit=5")
        assert status == 200
        runs = data.get("runs") or []
        assert len(runs) == 1
        assert len(runs[0]["agreement_preview"]) == 120
        assert runs[0]["agreement_preview"] == "A" * 120


# ---------------------------------------------------------------------------
# Truncation helper
# ---------------------------------------------------------------------------


class TestTruncateRunForUI:
    def test_truncate_limits_to_50(self) -> None:
        big = list(range(60))
        doc = {"steps": [{"entities": big, "episodes": big, "edges": big}]}
        out = server._truncate_run_for_ui(doc)
        assert len(out["steps"][0]["entities"]) == 50
        assert len(out["steps"][0]["episodes"]) == 50
        assert len(out["steps"][0]["edges"]) == 50

    def test_truncate_leaves_small_arrays_unchanged(self) -> None:
        small = [{"name": "a"}]
        doc = {"steps": [{"entities": small, "episodes": [], "edges": small}]}
        out = server._truncate_run_for_ui(doc)
        assert len(out["steps"][0]["entities"]) == 1
        assert len(out["steps"][0]["edges"]) == 1


# ---------------------------------------------------------------------------
# Startup: MONGO_URI / MONGO_DB_NAME required
# ---------------------------------------------------------------------------


class TestStartupEnv:
    def test_get_mongo_collection_fails_without_mongo_uri(self) -> None:
        with patch.dict("os.environ", {"MONGO_URI": "", "MONGO_DB_NAME": "db"}, clear=False):
            with pytest.raises(SystemExit) as exc_info:
                server._get_mongo_collection()
        assert "MONGO_URI" in str(exc_info.value)

    def test_get_mongo_collection_fails_without_mongo_db_name(self) -> None:
        with patch.dict("os.environ", {"MONGO_URI": "mongodb://localhost", "MONGO_DB_NAME": ""}, clear=False):
            with pytest.raises(SystemExit) as exc_info:
                server._get_mongo_collection()
        assert "MONGO_DB_NAME" in str(exc_info.value)


# ---------------------------------------------------------------------------
# ThreadingTCPServer is used (not plain TCPServer)
# ---------------------------------------------------------------------------


class TestServerType:
    def test_threading_tcpserver_is_used(self) -> None:
        source = Path(server.__file__).read_text()
        assert "ThreadingTCPServer" in source
