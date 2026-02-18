"""Tests for Project Forge server — multi-bonfire API & state management.

Covers all acceptance criteria from the Backend: Multi-Bonfire API ticket:
  1. /api/* proxy handles both GET and POST requests
  2. All Forge endpoints accept and respect bonfire_id query parameter
  3. POST /forge/trigger?bonfire_id=... triggers immediate generation
  4. Worker maintains separate state files per bonfire
  5. Mockups stored in bonfire-namespaced directories
  6. Server tracks current_bonfire_id and updates it on requests
  7. On server restart, current_bonfire_id restored from most recent state file
  8. Worker polls only current bonfire (not multiple)
  9. Backend validates bonfire is public when bonfire list is available
  10. Backend allows best-effort behaviour when bonfire list fetch fails
"""

import http.client
import json
import socket
import socketserver
import sys
import threading
import time
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

FORGE_DIR = Path(__file__).parent
sys.path.insert(0, str(FORGE_DIR))

import server
from worker import ForgeWorker


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _free_port() -> int:
    """Return a free TCP port on localhost."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _start_server(port: int) -> socketserver.TCPServer:
    socketserver.TCPServer.allow_reuse_address = True
    httpd = socketserver.TCPServer(("127.0.0.1", port), server.ForgeHandler)
    t = threading.Thread(target=httpd.serve_forever, daemon=True)
    t.start()
    time.sleep(0.1)
    return httpd


def _get(port: int, path: str) -> tuple[int, dict]:
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
    conn.request("GET", path)
    resp = conn.getresponse()
    body = resp.read()
    conn.close()
    try:
        data = json.loads(body)
    except json.JSONDecodeError:
        data = {"_raw": body.decode(errors="replace")}
    return resp.status, data


def _post(port: int, path: str, body: dict | None = None) -> tuple[int, dict]:
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
    payload = json.dumps(body).encode() if body else b""
    headers = {"Content-Type": "application/json"} if body else {}
    conn.request("POST", path, body=payload, headers=headers)
    resp = conn.getresponse()
    raw = resp.read()
    conn.close()
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        data = {"_raw": raw.decode(errors="replace")}
    return resp.status, data


@pytest.fixture(autouse=True)
def _reset_server_state():
    """Reset module-level server state between tests."""
    server.current_bonfire_id = None
    server.worker = ForgeWorker()
    yield
    server.current_bonfire_id = None


@pytest.fixture()
def tmp_forge(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """Redirect FORGE_DIR to a temp directory for file-based tests."""
    monkeypatch.setattr(server, "FORGE_DIR", tmp_path)
    monkeypatch.setattr("worker.FORGE_DIR", tmp_path)
    monkeypatch.setattr("worker.MOCKUPS_DIR", tmp_path / "mockups")
    server.worker = ForgeWorker()
    return tmp_path


@pytest.fixture()
def live_server(tmp_forge: Path):
    """Start a live HTTP server on a random free port (with validation mocked to True)."""
    port = _free_port()
    with patch("server._validate_public_bonfire", return_value=True):
        httpd = _start_server(port)
        yield port, tmp_forge
        httpd.shutdown()


# ---------------------------------------------------------------------------
# 1. URL parsing helpers
# ---------------------------------------------------------------------------

class TestURLParsing:
    """Verify _parse_bonfire_id and _strip_path parse query strings correctly."""

    @staticmethod
    def _make_handler(path: str) -> server.ForgeHandler:
        handler = object.__new__(server.ForgeHandler)
        handler.path = path
        return handler

    def test_parse_bonfire_id_present(self):
        h = self._make_handler("/forge/projects?bonfire_id=abc123")
        assert h._parse_bonfire_id() == "abc123"

    def test_parse_bonfire_id_missing(self):
        h = self._make_handler("/forge/projects")
        assert h._parse_bonfire_id() is None

    def test_strip_path_removes_query(self):
        h = self._make_handler("/forge/status?bonfire_id=x&foo=bar")
        assert h._strip_path() == "/forge/status"

    def test_strip_path_no_query(self):
        h = self._make_handler("/healthz")
        assert h._strip_path() == "/healthz"


# ---------------------------------------------------------------------------
# 2. /api/* proxy handles both GET and POST
# ---------------------------------------------------------------------------

class TestAPIProxy:
    """AC-1: /api/* routes for both GET and POST."""

    def test_get_api_routed(self, live_server: tuple[int, Path]):
        """GET /api/bonfires should reach the proxy (mocked upstream)."""
        port, _ = live_server
        mock_resp = MagicMock()
        mock_resp.status = 200
        mock_resp.read.return_value = b'[{"id":"bf1"}]'
        mock_resp.headers = {"Content-Type": "application/json"}
        mock_resp.__enter__ = lambda s: s
        mock_resp.__exit__ = MagicMock(return_value=False)

        with patch("server.urllib.request.urlopen", return_value=mock_resp):
            status, data = _get(port, "/api/bonfires")
        assert status == 200

    def test_post_api_routed(self, live_server: tuple[int, Path]):
        """POST /api/delve should reach the proxy."""
        port, _ = live_server
        mock_resp = MagicMock()
        mock_resp.status = 200
        mock_resp.read.return_value = b'{"ok":true}'
        mock_resp.headers = {"Content-Type": "application/json"}
        mock_resp.__enter__ = lambda s: s
        mock_resp.__exit__ = MagicMock(return_value=False)

        with patch("server.urllib.request.urlopen", return_value=mock_resp):
            status, data = _post(port, "/api/delve", {"query": "test"})
        assert status == 200


# ---------------------------------------------------------------------------
# 3. Forge endpoints accept bonfire_id query parameter
# ---------------------------------------------------------------------------

class TestBonfireIdRouting:
    """AC-2: All Forge endpoints accept and respect bonfire_id query parameter."""

    def test_projects_list_with_bonfire_id(self, live_server: tuple[int, Path]):
        port, _ = live_server
        status, data = _get(port, "/forge/projects?bonfire_id=bf001")
        assert status == 200
        assert "projects" in data
        assert server.current_bonfire_id == "bf001"

    def test_projects_list_without_bonfire_id(self, live_server: tuple[int, Path]):
        port, _ = live_server
        status, data = _get(port, "/forge/projects")
        assert status == 200
        assert "projects" in data

    def test_status_with_bonfire_id(self, live_server: tuple[int, Path]):
        port, _ = live_server
        status, data = _get(port, "/forge/status?bonfire_id=bf002")
        assert status == 200
        assert data["current_bonfire_id"] == "bf002"
        assert server.current_bonfire_id == "bf002"

    def test_project_detail_with_bonfire_id(self, live_server: tuple[int, Path]):
        port, _ = live_server
        status, data = _get(port, "/forge/projects/test-proj?bonfire_id=bf003")
        assert status == 404
        assert "error" in data
        assert server.current_bonfire_id == "bf003"

    def test_project_detail_returns_project(self, live_server: tuple[int, Path]):
        port, forge_dir = live_server
        bid = "bf010"
        state = {
            "version": 1,
            "projects": [{
                "id": "test-proj",
                "current_version": 1,
                "created_at": "2026-01-01T00:00:00Z",
                "updated_at": "2026-01-01T00:00:00Z",
                "versions": [{
                    "version": 1,
                    "generated_at": "2026-01-01T00:00:00Z",
                    "project_data": {"name": "Test", "tagline": "A test"},
                    "mockup_dir": f"mockups/{bid}/test-proj/v1",
                    "mockup_files": [],
                }],
            }],
            "last_generation_at": "2026-01-01T00:00:00Z",
            "generation_count": 1,
        }
        (forge_dir / f"forge_state_{bid}.json").write_text(json.dumps(state))

        status, data = _get(port, f"/forge/projects?bonfire_id={bid}")
        assert status == 200
        assert len(data["projects"]) == 1
        assert data["projects"][0]["id"] == "test-proj"


# ---------------------------------------------------------------------------
# 4. POST /forge/trigger?bonfire_id=... triggers immediate generation
# ---------------------------------------------------------------------------

class TestTriggerEndpoint:
    """AC-3: POST /forge/trigger?bonfire_id=... triggers generation."""

    def test_trigger_with_bonfire_id(self, live_server: tuple[int, Path]):
        port, _ = live_server
        with patch.object(server.worker, "trigger_now") as mock_trigger:
            status, data = _post(port, "/forge/trigger?bonfire_id=bf100")
            assert status == 202
            assert data["status"] == "triggered"
            assert data["bonfire_id"] == "bf100"
            mock_trigger.assert_called_once_with("bf100")

    def test_trigger_with_current_bonfire(self, live_server: tuple[int, Path]):
        port, _ = live_server
        server.current_bonfire_id = "bf_existing"
        server.worker.current_bonfire_id = "bf_existing"
        with patch.object(server.worker, "trigger_now") as mock_trigger:
            status, data = _post(port, "/forge/trigger")
            assert status == 202
            mock_trigger.assert_called_once_with("bf_existing")

    def test_trigger_no_bonfire_returns_400(self, live_server: tuple[int, Path]):
        port, _ = live_server
        status, data = _post(port, "/forge/trigger")
        assert status == 400
        assert "error" in data


# ---------------------------------------------------------------------------
# 5. Worker maintains separate state files per bonfire
# ---------------------------------------------------------------------------

class TestPerBonfireStateFiles:
    """AC-4: Worker maintains forge_state_<bonfire_id>.json per bonfire."""

    def test_state_file_path(self):
        w = ForgeWorker()
        w.current_bonfire_id = "abc"
        path = w._state_file()
        assert path.name == "forge_state_abc.json"

    def test_state_file_with_explicit_id(self):
        w = ForgeWorker()
        w.current_bonfire_id = "abc"
        path = w._state_file("xyz")
        assert path.name == "forge_state_xyz.json"

    def test_state_file_fallback_no_bonfire(self):
        w = ForgeWorker()
        w.current_bonfire_id = None
        path = w._state_file()
        assert path.name == "forge_state.json"

    def test_save_and_load_state(self, tmp_forge: Path):
        w = server.worker
        w.current_bonfire_id = "bf_save"
        state = {"version": 1, "projects": [], "custom": "data"}
        w.save_state(state, "bf_save")

        loaded = w.load_state("bf_save")
        assert loaded["custom"] == "data"
        assert loaded["bonfire_id"] == "bf_save"

    def test_isolated_state_per_bonfire(self, tmp_forge: Path):
        w = server.worker
        w.save_state({"version": 1, "projects": [{"id": "p1"}]}, "bf_a")
        w.save_state({"version": 1, "projects": [{"id": "p2"}]}, "bf_b")

        state_a = w.load_state("bf_a")
        state_b = w.load_state("bf_b")
        assert state_a["projects"][0]["id"] == "p1"
        assert state_b["projects"][0]["id"] == "p2"


# ---------------------------------------------------------------------------
# 6. Mockups stored in bonfire-namespaced directories
# ---------------------------------------------------------------------------

class TestBonfireNamespacedMockups:
    """AC-5: Mockups served from mockups/<bonfire_id>/<project_id>/v<ver>/."""

    @pytest.fixture()
    def mockup_server(self, tmp_forge: Path):
        mockup_dir = tmp_forge / "mockups" / "bf200" / "my-project" / "v1"
        mockup_dir.mkdir(parents=True)
        (mockup_dir / "index.html").write_text("<html><body>Hello</body></html>")

        port = _free_port()
        with patch("server._validate_public_bonfire", return_value=True):
            httpd = _start_server(port)
            yield port
            httpd.shutdown()

    def test_serve_bonfire_namespaced_mockup(self, mockup_server: int):
        status, data = _get(mockup_server, "/forge/mockups/bf200/my-project/v1/index.html")
        assert status == 200
        assert "Hello" in data.get("_raw", "")

    def test_mockup_404_wrong_bonfire(self, mockup_server: int):
        status, _ = _get(mockup_server, "/forge/mockups/wrong-bf/my-project/v1/index.html")
        assert status == 404

    def test_mockup_404_too_few_parts(self, mockup_server: int):
        status, _ = _get(mockup_server, "/forge/mockups/bf200/my-project")
        assert status == 404


# ---------------------------------------------------------------------------
# 7. Server tracks current_bonfire_id
# ---------------------------------------------------------------------------

class TestCurrentBonfireTracking:
    """AC-6: Server tracks current_bonfire_id and updates it on requests."""

    def test_current_bonfire_updated_on_projects(self, live_server: tuple[int, Path]):
        port, _ = live_server
        assert server.current_bonfire_id is None
        _get(port, "/forge/projects?bonfire_id=track01")
        assert server.current_bonfire_id == "track01"

    def test_current_bonfire_updated_on_status(self, live_server: tuple[int, Path]):
        port, _ = live_server
        _get(port, "/forge/status?bonfire_id=track02")
        assert server.current_bonfire_id == "track02"

    def test_current_bonfire_updated_on_trigger(self, live_server: tuple[int, Path]):
        port, _ = live_server
        with patch.object(server.worker, "trigger_now"):
            _post(port, "/forge/trigger?bonfire_id=track03")
        assert server.current_bonfire_id == "track03"

    def test_worker_notified_on_update(self, live_server: tuple[int, Path]):
        port, _ = live_server
        with patch.object(server.worker, "set_current_bonfire") as mock_set:
            _get(port, "/forge/projects?bonfire_id=track04")
            mock_set.assert_called_with("track04")


# ---------------------------------------------------------------------------
# 8. Restore current_bonfire_id on restart
# ---------------------------------------------------------------------------

class TestRestoreOnRestart:
    """AC-7: On server restart, current_bonfire_id restored from most recent state file."""

    def test_restore_from_most_recent_file(self, tmp_forge: Path):
        (tmp_forge / "forge_state_old_bf.json").write_text('{"version":1}')
        time.sleep(0.05)
        (tmp_forge / "forge_state_new_bf.json").write_text('{"version":1}')

        server._restore_current_bonfire()
        assert server.current_bonfire_id == "new_bf"

    def test_restore_no_files(self, tmp_forge: Path):
        server._restore_current_bonfire()
        assert server.current_bonfire_id is None

    def test_restore_skips_base_state_file(self, tmp_forge: Path):
        (tmp_forge / "forge_state.json").write_text('{"version":1}')
        server._restore_current_bonfire()
        assert server.current_bonfire_id is None

    def test_restore_notifies_worker(self, tmp_forge: Path):
        (tmp_forge / "forge_state_restored_bf.json").write_text('{"version":1}')
        with patch.object(server.worker, "set_current_bonfire") as mock_set:
            server._restore_current_bonfire()
            mock_set.assert_called_with("restored_bf")


# ---------------------------------------------------------------------------
# 9. Backend validates bonfire is public
# ---------------------------------------------------------------------------

class TestPublicBonfireValidation:
    """AC-9: Backend validates bonfire is public when bonfire list is available."""

    @pytest.fixture()
    def strict_server(self, tmp_forge: Path):
        """Server WITHOUT the validation mock — validation is real/patchable per test."""
        port = _free_port()
        httpd = _start_server(port)
        yield port
        httpd.shutdown()

    def test_reject_non_public_bonfire_on_projects(self, strict_server: int):
        with patch("server._validate_public_bonfire", return_value=False):
            status, data = _get(strict_server, "/forge/projects?bonfire_id=private_bf")
        assert status == 403
        assert "not public" in data["error"]

    def test_reject_non_public_bonfire_on_status(self, strict_server: int):
        with patch("server._validate_public_bonfire", return_value=False):
            status, _ = _get(strict_server, "/forge/status?bonfire_id=private_bf")
        assert status == 403

    def test_reject_non_public_bonfire_on_trigger(self, strict_server: int):
        with patch("server._validate_public_bonfire", return_value=False):
            status, _ = _post(strict_server, "/forge/trigger?bonfire_id=private_bf")
        assert status == 403

    def test_reject_non_public_bonfire_on_detail(self, strict_server: int):
        with patch("server._validate_public_bonfire", return_value=False):
            status, _ = _get(strict_server, "/forge/projects/some-proj?bonfire_id=private_bf")
        assert status == 403

    def test_allow_public_bonfire(self, strict_server: int):
        with patch("server._validate_public_bonfire", return_value=True):
            status, data = _get(strict_server, "/forge/projects?bonfire_id=public_bf")
        assert status == 200


# ---------------------------------------------------------------------------
# 10. Best-effort when bonfire list fetch fails
# ---------------------------------------------------------------------------

class TestBestEffortValidation:
    """AC-10: Backend allows best-effort when bonfire list fetch fails."""

    def test_validate_returns_true_on_api_failure(self):
        with patch("server.urllib.request.urlopen", side_effect=Exception("network error")):
            result = server._validate_public_bonfire("any_bonfire")
        assert result is True

    def test_validate_returns_true_for_listed_bonfire(self):
        mock_resp = MagicMock()
        mock_resp.read.return_value = json.dumps([
            {"id": "bf_pub", "name": "Public One"},
        ]).encode()
        mock_resp.__enter__ = lambda s: s
        mock_resp.__exit__ = MagicMock(return_value=False)

        with patch("server.urllib.request.urlopen", return_value=mock_resp):
            assert server._validate_public_bonfire("bf_pub") is True

    def test_validate_returns_false_for_unlisted_bonfire(self):
        mock_resp = MagicMock()
        mock_resp.read.return_value = json.dumps([
            {"id": "bf_pub", "name": "Public One"},
        ]).encode()
        mock_resp.__enter__ = lambda s: s
        mock_resp.__exit__ = MagicMock(return_value=False)

        with patch("server.urllib.request.urlopen", return_value=mock_resp):
            assert server._validate_public_bonfire("bf_private") is False

    def test_validate_handles_dict_response_with_bonfires_key(self):
        mock_resp = MagicMock()
        mock_resp.read.return_value = json.dumps({
            "bonfires": [{"_id": "bf_alt", "name": "Alt Bonfire"}]
        }).encode()
        mock_resp.__enter__ = lambda s: s
        mock_resp.__exit__ = MagicMock(return_value=False)

        with patch("server.urllib.request.urlopen", return_value=mock_resp):
            assert server._validate_public_bonfire("bf_alt") is True
            assert server._validate_public_bonfire("bf_missing") is False


# ---------------------------------------------------------------------------
# 11. Worker polls only current bonfire
# ---------------------------------------------------------------------------

class TestWorkerPollsBehavior:
    """AC-8: Worker polls only the current bonfire."""

    def test_set_current_bonfire(self):
        w = ForgeWorker()
        w.set_current_bonfire("bf_poll")
        assert w.current_bonfire_id == "bf_poll"

    def test_trigger_now_sets_bonfire(self):
        w = ForgeWorker()
        with patch.object(w, "_do_poll_cycle"):
            w.trigger_now("bf_triggered")
        assert w.current_bonfire_id == "bf_triggered"

    def test_trigger_now_without_bonfire_keeps_current(self):
        w = ForgeWorker()
        w.current_bonfire_id = "bf_existing"
        with patch.object(w, "_do_poll_cycle"):
            w.trigger_now()
        assert w.current_bonfire_id == "bf_existing"


# ---------------------------------------------------------------------------
# 12. Healthz
# ---------------------------------------------------------------------------

class TestHealthz:
    def test_healthz(self, live_server: tuple[int, Path]):
        port, _ = live_server
        status, data = _get(port, "/healthz")
        assert status == 200
        assert data["status"] == "ok"
