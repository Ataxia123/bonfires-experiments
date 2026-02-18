"""Tests for ForgeHandler multi-bonfire API routing."""

import json
import http.client
import threading
import socketserver
import time
from pathlib import Path
from unittest.mock import patch, MagicMock
from urllib.parse import urlencode

import pytest

import sys
sys.path.insert(0, str(Path(__file__).parent.parent))


@pytest.fixture
def tmp_forge_dir(tmp_path: Path) -> Path:
    return tmp_path


@pytest.fixture
def mock_worker():
    """A mock ForgeWorker for server tests."""
    from worker import ForgeWorker, _default_state
    w = MagicMock(spec=ForgeWorker)
    w.current_bonfire_id = None
    w.load_state.return_value = _default_state()
    w.get_status.return_value = {
        "status": "idle",
        "last_error": None,
        "last_poll_at": None,
        "last_generation_at": None,
        "poll_count": 0,
        "generation_count": 0,
        "project_count": 0,
        "poll_interval_seconds": 21600,
        "change_threshold": 0.3,
        "poll_log": [],
    }
    return w


@pytest.fixture
def test_server(mock_worker, tmp_forge_dir):
    """Spin up a real HTTP server on a random port for integration tests."""
    import server as server_mod

    with patch.object(server_mod, "worker", mock_worker), \
         patch.object(server_mod, "FORGE_DIR", tmp_forge_dir):

        socketserver.TCPServer.allow_reuse_address = True
        httpd = socketserver.TCPServer(("127.0.0.1", 0), server_mod.ForgeHandler)
        port = httpd.server_address[1]

        thread = threading.Thread(target=httpd.serve_forever, daemon=True)
        thread.start()

        yield port, mock_worker

        httpd.shutdown()


def _get(port: int, path: str) -> http.client.HTTPResponse:
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
    conn.request("GET", path)
    return conn.getresponse()


def _post(port: int, path: str, body: bytes = b"") -> http.client.HTTPResponse:
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
    conn.request("POST", path, body=body, headers={"Content-Type": "application/json"})
    return conn.getresponse()


# ── Query parameter parsing ───────────────────────────────────────────────


class TestQueryParamParsing:
    """bonfire_id is correctly extracted from query string."""

    def test_projects_with_bonfire_id(self, test_server):
        port, mock_worker = test_server
        resp = _get(port, "/forge/projects?bonfire_id=test-bf-123")
        assert resp.status == 200
        mock_worker.load_state.assert_called_with(bonfire_id="test-bf-123")

    def test_status_with_bonfire_id(self, test_server):
        port, mock_worker = test_server
        resp = _get(port, "/forge/status?bonfire_id=status-bf")
        assert resp.status == 200
        mock_worker.get_status.assert_called_with(bonfire_id="status-bf")

    def test_trigger_with_bonfire_id(self, test_server):
        port, mock_worker = test_server
        resp = _post(port, "/forge/trigger?bonfire_id=trig-bf")
        assert resp.status == 202
        mock_worker.trigger_now.assert_called_with(bonfire_id="trig-bf")

    def test_bonfire_id_updates_current_on_worker(self, test_server):
        port, mock_worker = test_server
        _get(port, "/forge/projects?bonfire_id=update-bf")
        mock_worker.set_current_bonfire.assert_called_with("update-bf")


# ── GET /api/* proxy ──────────────────────────────────────────────────────


class TestGetApiProxy:
    """GET requests to /api/* are proxied to upstream."""

    def test_get_api_bonfires_is_proxied(self, test_server):
        port, _ = test_server
        mock_response = MagicMock()
        mock_response.status = 200
        mock_response.read.return_value = json.dumps({"bonfires": []}).encode()
        mock_response.headers = {"Content-Type": "application/json"}
        mock_response.__enter__ = lambda s: s
        mock_response.__exit__ = MagicMock(return_value=False)

        with patch("urllib.request.urlopen", return_value=mock_response):
            resp = _get(port, "/api/bonfires")

        assert resp.status == 200


# ── Public-only validation ────────────────────────────────────────────────


class TestPublicBonfireValidation:
    """Requests with non-public bonfire_id are rejected when list is available."""

    def test_rejects_non_public_bonfire(self, test_server):
        port, mock_worker = test_server

        bonfires_response = json.dumps({
            "bonfires": [
                {"id": "public-bf", "name": "Public", "is_public": True},
                {"id": "private-bf", "name": "Private", "is_public": False},
            ]
        }).encode()

        mock_resp = MagicMock()
        mock_resp.status = 200
        mock_resp.read.return_value = bonfires_response
        mock_resp.headers = {"Content-Type": "application/json"}
        mock_resp.__enter__ = lambda s: s
        mock_resp.__exit__ = MagicMock(return_value=False)

        with patch("server.validate_bonfire_is_public", return_value=False):
            resp = _get(port, "/forge/projects?bonfire_id=private-bf")

        assert resp.status == 403

    def test_allows_public_bonfire(self, test_server):
        port, mock_worker = test_server

        with patch("server.validate_bonfire_is_public", return_value=True):
            resp = _get(port, "/forge/projects?bonfire_id=public-bf")

        assert resp.status == 200

    def test_best_effort_when_validation_fails(self, test_server):
        port, mock_worker = test_server

        with patch("server.validate_bonfire_is_public", return_value=None):
            resp = _get(port, "/forge/projects?bonfire_id=any-bf")

        # None = validation unavailable, should proceed (best-effort)
        assert resp.status == 200


# ── Mockup serving with bonfire namespace ─────────────────────────────────


class TestMockupServing:
    """Mockups are served from bonfire-namespaced directories."""

    def test_serves_bonfire_namespaced_mockup(self, test_server, tmp_forge_dir):
        port, mock_worker = test_server

        # Create a mockup file in the bonfire-namespaced directory
        mockup_dir = tmp_forge_dir / "mockups" / "bf-123" / "proj-1" / "v1"
        mockup_dir.mkdir(parents=True)
        (mockup_dir / "index.html").write_text("<html>test</html>")

        resp = _get(port, "/forge/mockups/bf-123/proj-1/v1/index.html")
        assert resp.status == 200
        body = resp.read().decode()
        assert "test" in body
