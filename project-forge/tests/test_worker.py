"""Tests for ForgeWorker multi-bonfire state management."""

import json
import os
import time
from pathlib import Path
from unittest.mock import patch, MagicMock

import pytest

# Ensure we can import from the parent package
import sys
sys.path.insert(0, str(Path(__file__).parent.parent))

from worker import ForgeWorker, _default_state, FORGE_DIR


@pytest.fixture
def tmp_forge_dir(tmp_path: Path) -> Path:
    """Provide a temporary directory standing in for FORGE_DIR."""
    return tmp_path


@pytest.fixture
def worker(tmp_forge_dir: Path) -> ForgeWorker:
    """Create a ForgeWorker whose state/mockup paths point at tmp_forge_dir."""
    w = ForgeWorker()
    # Patch module-level constants so all helpers use temp dir
    with patch("worker.FORGE_DIR", tmp_forge_dir), \
         patch("worker.MOCKUPS_DIR", tmp_forge_dir / "mockups"):
        yield w


# ── State file isolation ──────────────────────────────────────────────────


class TestPerBonfireStateFiles:
    """load_state / save_state produce separate files per bonfire_id."""

    def test_save_creates_bonfire_scoped_file(self, worker: ForgeWorker, tmp_forge_dir: Path):
        state = _default_state()
        state["projects"] = [{"id": "proj-1"}]

        with patch("worker.FORGE_DIR", tmp_forge_dir):
            worker.save_state(state, bonfire_id="bonfire-aaa")

        expected = tmp_forge_dir / "forge_state_bonfire-aaa.json"
        assert expected.exists(), "State file should be named forge_state_<bonfire_id>.json"

        data = json.loads(expected.read_text())
        assert data["bonfire_id"] == "bonfire-aaa"
        assert data["projects"] == [{"id": "proj-1"}]

    def test_load_reads_bonfire_scoped_file(self, worker: ForgeWorker, tmp_forge_dir: Path):
        state = _default_state()
        state["projects"] = [{"id": "proj-x"}]
        state["bonfire_id"] = "bonfire-bbb"
        (tmp_forge_dir / "forge_state_bonfire-bbb.json").write_text(json.dumps(state))

        with patch("worker.FORGE_DIR", tmp_forge_dir):
            loaded = worker.load_state(bonfire_id="bonfire-bbb")

        assert loaded["projects"] == [{"id": "proj-x"}]

    def test_different_bonfires_are_isolated(self, worker: ForgeWorker, tmp_forge_dir: Path):
        with patch("worker.FORGE_DIR", tmp_forge_dir):
            state_a = _default_state()
            state_a["projects"] = [{"id": "a-proj"}]
            worker.save_state(state_a, bonfire_id="alpha")

            state_b = _default_state()
            state_b["projects"] = [{"id": "b-proj"}]
            worker.save_state(state_b, bonfire_id="beta")

            loaded_a = worker.load_state(bonfire_id="alpha")
            loaded_b = worker.load_state(bonfire_id="beta")

        assert loaded_a["projects"] == [{"id": "a-proj"}]
        assert loaded_b["projects"] == [{"id": "b-proj"}]

    def test_load_nonexistent_bonfire_returns_default(self, worker: ForgeWorker, tmp_forge_dir: Path):
        with patch("worker.FORGE_DIR", tmp_forge_dir):
            state = worker.load_state(bonfire_id="does-not-exist")

        assert state == _default_state()


# ── current_bonfire_id restoration ────────────────────────────────────────


class TestCurrentBonfireRestore:
    """On startup, current_bonfire_id is restored from the newest state file."""

    def test_restore_picks_most_recent_state_file(self, worker: ForgeWorker, tmp_forge_dir: Path):
        with patch("worker.FORGE_DIR", tmp_forge_dir):
            # Write two state files with different mtimes
            old_file = tmp_forge_dir / "forge_state_old-bf.json"
            old_file.write_text(json.dumps(_default_state()))
            # Set old mtime
            os.utime(old_file, (time.time() - 3600, time.time() - 3600))

            new_file = tmp_forge_dir / "forge_state_new-bf.json"
            new_file.write_text(json.dumps(_default_state()))

            worker.restore_current_bonfire()

        assert worker.current_bonfire_id == "new-bf"

    def test_restore_with_no_state_files_leaves_none(self, worker: ForgeWorker, tmp_forge_dir: Path):
        with patch("worker.FORGE_DIR", tmp_forge_dir):
            worker.restore_current_bonfire()

        assert worker.current_bonfire_id is None


# ── trigger_now ───────────────────────────────────────────────────────────


class TestTriggerNow:
    """trigger_now(bonfire_id) updates current_bonfire_id and runs a cycle."""

    def test_trigger_updates_current_bonfire_id(self, worker: ForgeWorker, tmp_forge_dir: Path):
        with patch("worker.FORGE_DIR", tmp_forge_dir), \
             patch.object(worker, "_do_poll_cycle"):
            worker.trigger_now(bonfire_id="triggered-bf")

        assert worker.current_bonfire_id == "triggered-bf"

    def test_trigger_spawns_poll_cycle(self, worker: ForgeWorker, tmp_forge_dir: Path):
        with patch("worker.FORGE_DIR", tmp_forge_dir), \
             patch.object(worker, "_do_poll_cycle") as mock_cycle:
            worker.trigger_now(bonfire_id="trigger-test")
            # Give the thread a moment to start
            time.sleep(0.2)

        # _do_poll_cycle should have been called
        mock_cycle.assert_called()


# ── get_status with bonfire_id ────────────────────────────────────────────


class TestGetStatus:
    """get_status respects bonfire_id parameter."""

    def test_status_returns_bonfire_scoped_data(self, worker: ForgeWorker, tmp_forge_dir: Path):
        state = _default_state()
        state["poll_count"] = 42
        state["bonfire_id"] = "status-bf"
        (tmp_forge_dir / "forge_state_status-bf.json").write_text(json.dumps(state))

        with patch("worker.FORGE_DIR", tmp_forge_dir):
            status = worker.get_status(bonfire_id="status-bf")

        assert status["poll_count"] == 42


# ── Mockup directory namespacing ──────────────────────────────────────────


class TestMockupNamespacing:
    """Mockup directories include bonfire_id in the path."""

    def test_regenerate_creates_bonfire_namespaced_mockup_dir(self, worker: ForgeWorker, tmp_forge_dir: Path):
        """The mockup_rel_dir in generated version entries must include bonfire_id."""
        state = _default_state()
        worker.current_bonfire_id = "mock-bf"

        mock_result = {
            "projects": [{
                "name": "Test Project",
                "status": "new",
                "tagline": "t",
                "description": "d",
                "themes": [],
                "tech_stack": [],
                "complexity": "weekend",
                "key_insight": "k",
                "first_step": "f",
            }]
        }

        with patch("worker.FORGE_DIR", tmp_forge_dir), \
             patch("worker.MOCKUPS_DIR", tmp_forge_dir / "mockups"), \
             patch("forge.extract_themes", return_value={"episodes": [], "entities": [], "edges": []}), \
             patch("forge.synthesize_projects", return_value=mock_result), \
             patch("forge.generate_multi_mockup", return_value={"files": []}):

            worker._regenerate(state, {"episodes": [], "entities": [], "edges": []}, "test", True)

        # Check that the project's mockup_dir includes the bonfire_id
        assert len(state["projects"]) == 1
        proj = state["projects"][0]
        latest_version = proj["versions"][-1]
        assert "mock-bf" in latest_version["mockup_dir"], \
            f"Expected bonfire_id in mockup_dir, got: {latest_version['mockup_dir']}"
