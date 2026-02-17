"""Tests for refactored worker.py — Ticket 3 acceptance criteria.

Verifies:
  1. _regenerate(), compute_change_score(), _build_snapshot() are removed
  2. _do_poll_cycle() builds ForgeState and calls forge_graph.ainvoke()
  3. Poll log entries populated from graph output (change_score, change_summary)
  4. mockup_results from graph merged into state as versioned project entries
  5. Existing forge_state_*.json files not corrupted on first poll after refactor
  6. Skip path (no mockup_results) correctly logs decision="skip"
  7. Retired projects marked from synthesized_projects output
"""

import json
import sys
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest

FORGE_DIR = Path(__file__).parent
sys.path.insert(0, str(FORGE_DIR))

import worker
from worker import ForgeWorker, _find_project, _slugify


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture()
def tmp_forge(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """Redirect FORGE_DIR to a temp directory."""
    monkeypatch.setattr(worker, "FORGE_DIR", tmp_path)
    monkeypatch.setattr(worker, "MOCKUPS_DIR", tmp_path / "mockups")
    return tmp_path


@pytest.fixture()
def fresh_worker(tmp_forge: Path) -> ForgeWorker:
    """Return a ForgeWorker with a clean temp directory."""
    w = ForgeWorker()
    w.current_bonfire_id = "test-bonfire"
    return w


def _make_graph_result(
    *,
    change_score: float = 0.5,
    change_summary: str = "2 new episodes",
    synthesized_projects: list[dict] | None = None,
    mockup_results: list[dict] | None = None,
) -> dict:
    """Build a realistic graph result dict."""
    return {
        "bonfire_id": "test-bonfire",
        "is_first_run": True,
        "themes_data": {"episodes": [], "entities": [], "edges": []},
        "new_kg_snapshot": {
            "polled_at": "2026-02-17T00:00:00+00:00",
            "episode_count": 10,
            "entity_count": 5,
            "edge_count": 3,
            "episode_hashes": ["aaa", "bbb"],
            "entity_uuids": ["u1", "u2"],
            "edge_fingerprints": ["e1"],
        },
        "change_score": change_score,
        "change_summary": change_summary,
        "synthesized_projects": synthesized_projects or [],
        "mockup_results": mockup_results or [],
    }


# ---------------------------------------------------------------------------
# 1. Removed symbols no longer exist in worker module
# ---------------------------------------------------------------------------

class TestRemovedSymbols:
    """AC: _regenerate(), compute_change_score(), _build_snapshot() are gone."""

    def test_regenerate_removed(self):
        assert not hasattr(ForgeWorker, "_regenerate")

    def test_compute_change_score_removed(self):
        assert not hasattr(worker, "compute_change_score")

    def test_build_snapshot_removed(self):
        assert not hasattr(worker, "_build_snapshot")

    def test_no_hashlib_import(self):
        assert "hashlib" not in dir(worker)


# ---------------------------------------------------------------------------
# 2. _do_poll_cycle invokes forge_graph.ainvoke
# ---------------------------------------------------------------------------

class TestPollCycleInvokesGraph:
    """AC: _do_poll_cycle() builds ForgeState and calls forge_graph.ainvoke()."""

    def test_invokes_graph_with_correct_state(self, fresh_worker: ForgeWorker):
        graph_result = _make_graph_result()
        mock_ainvoke = AsyncMock(return_value=graph_result)

        with patch("worker.forge_graph") as mock_graph:
            mock_graph.ainvoke = mock_ainvoke
            fresh_worker._do_poll_cycle()

        mock_ainvoke.assert_called_once()
        call_args = mock_ainvoke.call_args[0][0]
        assert call_args["bonfire_id"] == "test-bonfire"
        assert call_args["is_first_run"] is True
        assert call_args["existing_projects"] == []
        assert call_args["old_kg_snapshot"] == {}
        assert call_args["change_threshold"] == worker.CHANGE_THRESHOLD
        assert call_args["project_versions"] == {}

    def test_passes_existing_projects_and_versions(self, fresh_worker: ForgeWorker):
        pre_state = {
            "version": 1,
            "projects": [{
                "id": "proj-a",
                "current_version": 2,
                "versions": [
                    {"version": 1, "project_data": {"name": "v1"}},
                    {"version": 2, "project_data": {"name": "Proj A"}},
                ],
            }],
            "kg_snapshot": {"episode_hashes": ["old"]},
            "poll_log": [],
        }
        fresh_worker.save_state(pre_state, "test-bonfire")

        graph_result = _make_graph_result(change_score=0.1, mockup_results=[])
        mock_ainvoke = AsyncMock(return_value=graph_result)

        with patch("worker.forge_graph") as mock_graph:
            mock_graph.ainvoke = mock_ainvoke
            fresh_worker._do_poll_cycle()

        call_args = mock_ainvoke.call_args[0][0]
        assert call_args["is_first_run"] is False
        assert call_args["existing_projects"] == [{"name": "Proj A"}]
        assert call_args["project_versions"] == {"proj-a": 2}
        assert call_args["old_kg_snapshot"]["episode_hashes"] == ["old"]


# ---------------------------------------------------------------------------
# 3. Poll log entries populated from graph output
# ---------------------------------------------------------------------------

class TestPollLogFromGraphOutput:
    """AC: Poll log entries use change_score and change_summary from graph."""

    def test_skip_poll_log(self, fresh_worker: ForgeWorker):
        graph_result = _make_graph_result(
            change_score=0.1,
            change_summary="no changes",
            mockup_results=[],
        )
        mock_ainvoke = AsyncMock(return_value=graph_result)

        with patch("worker.forge_graph") as mock_graph:
            mock_graph.ainvoke = mock_ainvoke
            fresh_worker._do_poll_cycle()

        state = fresh_worker.load_state("test-bonfire")
        assert len(state["poll_log"]) == 1
        entry = state["poll_log"][0]
        assert entry["change_score"] == 0.1
        assert entry["reason"] == "no changes"
        assert entry["decision"] == "skip"
        assert entry["bonfire_id"] == "test-bonfire"

    def test_regenerate_poll_log(self, fresh_worker: ForgeWorker):
        graph_result = _make_graph_result(
            change_score=0.7,
            change_summary="5 new episodes, 3 new entities",
            mockup_results=[{
                "project_id": "proj-x",
                "project_data": {"name": "Proj X"},
                "status": "new",
                "mockup_dir": "mockups/test-bonfire/proj-x/v1",
                "mockup_files": [{"name": "index.html", "label": "Home", "is_entry": True}],
            }],
        )
        mock_ainvoke = AsyncMock(return_value=graph_result)

        with patch("worker.forge_graph") as mock_graph:
            mock_graph.ainvoke = mock_ainvoke
            fresh_worker._do_poll_cycle()

        state = fresh_worker.load_state("test-bonfire")
        entry = state["poll_log"][0]
        assert entry["change_score"] == 0.7
        assert entry["decision"] == "regenerate"
        assert entry["reason"] == "5 new episodes, 3 new entities"

    def test_poll_log_snapshot_counts(self, fresh_worker: ForgeWorker):
        graph_result = _make_graph_result(change_score=0.0, mockup_results=[])
        mock_ainvoke = AsyncMock(return_value=graph_result)

        with patch("worker.forge_graph") as mock_graph:
            mock_graph.ainvoke = mock_ainvoke
            fresh_worker._do_poll_cycle()

        state = fresh_worker.load_state("test-bonfire")
        entry = state["poll_log"][0]
        assert entry["episode_count"] == 10
        assert entry["entity_count"] == 5
        assert entry["edge_count"] == 3


# ---------------------------------------------------------------------------
# 4. mockup_results merged into state as versioned project entries
# ---------------------------------------------------------------------------

class TestMockupResultsMerge:
    """AC: mockup_results from graph correctly merged into state file."""

    def test_new_project_created(self, fresh_worker: ForgeWorker):
        graph_result = _make_graph_result(mockup_results=[{
            "project_id": "new-project",
            "project_data": {"name": "New Project", "tagline": "Fresh idea"},
            "status": "new",
            "mockup_dir": "mockups/test-bonfire/new-project/v1",
            "mockup_files": [{"name": "index.html", "label": "Home", "is_entry": True}],
        }])
        mock_ainvoke = AsyncMock(return_value=graph_result)

        with patch("worker.forge_graph") as mock_graph:
            mock_graph.ainvoke = mock_ainvoke
            fresh_worker._do_poll_cycle()

        state = fresh_worker.load_state("test-bonfire")
        assert len(state["projects"]) == 1
        proj = state["projects"][0]
        assert proj["id"] == "new-project"
        assert proj["current_version"] == 1
        assert len(proj["versions"]) == 1
        ver = proj["versions"][0]
        assert ver["version"] == 1
        assert ver["project_data"]["name"] == "New Project"
        assert ver["mockup_dir"] == "mockups/test-bonfire/new-project/v1"
        assert ver["trigger"] == "initial_generation"
        assert ver["change_summary"] == "first generation"

    def test_existing_project_version_incremented(self, fresh_worker: ForgeWorker):
        pre_state = {
            "version": 1,
            "projects": [{
                "id": "existing-proj",
                "current_version": 1,
                "created_at": "2026-01-01T00:00:00Z",
                "updated_at": "2026-01-01T00:00:00Z",
                "versions": [{
                    "version": 1,
                    "project_data": {"name": "Existing Proj"},
                    "mockup_dir": "mockups/test-bonfire/existing-proj/v1",
                    "mockup_files": [],
                }],
            }],
            "kg_snapshot": {},
            "poll_log": [],
        }
        fresh_worker.save_state(pre_state, "test-bonfire")

        graph_result = _make_graph_result(
            change_score=0.5,
            change_summary="3 new episodes",
            mockup_results=[{
                "project_id": "existing-proj",
                "project_data": {"name": "Existing Proj", "tagline": "Updated"},
                "status": "updated",
                "mockup_dir": "mockups/test-bonfire/existing-proj/v2",
                "mockup_files": [{"name": "index.html"}],
            }],
        )
        # Not first_run since there are existing projects
        graph_result["is_first_run"] = False
        mock_ainvoke = AsyncMock(return_value=graph_result)

        with patch("worker.forge_graph") as mock_graph:
            mock_graph.ainvoke = mock_ainvoke
            fresh_worker._do_poll_cycle()

        state = fresh_worker.load_state("test-bonfire")
        proj = state["projects"][0]
        assert proj["current_version"] == 2
        assert len(proj["versions"]) == 2
        assert proj["versions"][-1]["version"] == 2
        assert proj["versions"][-1]["trigger"] == "kg_change"
        assert proj["versions"][-1]["change_summary"] == "3 new episodes"

    def test_multiple_mockup_results(self, fresh_worker: ForgeWorker):
        graph_result = _make_graph_result(mockup_results=[
            {
                "project_id": "proj-a",
                "project_data": {"name": "Proj A"},
                "status": "new",
                "mockup_dir": "mockups/test-bonfire/proj-a/v1",
                "mockup_files": [],
            },
            {
                "project_id": "proj-b",
                "project_data": {"name": "Proj B"},
                "status": "new",
                "mockup_dir": "mockups/test-bonfire/proj-b/v1",
                "mockup_files": [],
            },
        ])
        mock_ainvoke = AsyncMock(return_value=graph_result)

        with patch("worker.forge_graph") as mock_graph:
            mock_graph.ainvoke = mock_ainvoke
            fresh_worker._do_poll_cycle()

        state = fresh_worker.load_state("test-bonfire")
        assert len(state["projects"]) == 2
        ids = {p["id"] for p in state["projects"]}
        assert ids == {"proj-a", "proj-b"}

    def test_generation_count_incremented(self, fresh_worker: ForgeWorker):
        graph_result = _make_graph_result(mockup_results=[{
            "project_id": "p1",
            "project_data": {"name": "P1"},
            "status": "new",
            "mockup_dir": "mockups/test-bonfire/p1/v1",
            "mockup_files": [],
        }])
        mock_ainvoke = AsyncMock(return_value=graph_result)

        with patch("worker.forge_graph") as mock_graph:
            mock_graph.ainvoke = mock_ainvoke
            fresh_worker._do_poll_cycle()

        state = fresh_worker.load_state("test-bonfire")
        assert state["generation_count"] == 1
        assert state["last_generation_at"] is not None

    def test_version_cap_enforced(self, fresh_worker: ForgeWorker):
        versions = [
            {"version": i, "project_data": {"name": f"v{i}"}, "mockup_dir": f"m/v{i}", "mockup_files": []}
            for i in range(1, worker.MAX_VERSIONS + 2)
        ]
        pre_state = {
            "version": 1,
            "projects": [{
                "id": "capped",
                "current_version": worker.MAX_VERSIONS + 1,
                "created_at": "2026-01-01T00:00:00Z",
                "updated_at": "2026-01-01T00:00:00Z",
                "versions": versions,
            }],
            "kg_snapshot": {},
            "poll_log": [],
        }
        fresh_worker.save_state(pre_state, "test-bonfire")

        new_ver = worker.MAX_VERSIONS + 2
        graph_result = _make_graph_result(mockup_results=[{
            "project_id": "capped",
            "project_data": {"name": "Capped"},
            "status": "updated",
            "mockup_dir": f"mockups/test-bonfire/capped/v{new_ver}",
            "mockup_files": [],
        }])
        mock_ainvoke = AsyncMock(return_value=graph_result)

        with patch("worker.forge_graph") as mock_graph:
            mock_graph.ainvoke = mock_ainvoke
            fresh_worker._do_poll_cycle()

        state = fresh_worker.load_state("test-bonfire")
        assert len(state["projects"][0]["versions"]) <= worker.MAX_VERSIONS


# ---------------------------------------------------------------------------
# 5. Existing state files not corrupted after refactor
# ---------------------------------------------------------------------------

class TestExistingStatePreserved:
    """AC: Existing forge_state_*.json files remain compatible."""

    def test_legacy_state_survives_poll(self, fresh_worker: ForgeWorker):
        legacy_state = {
            "version": 1,
            "last_poll_at": "2026-02-16T00:00:00Z",
            "last_generation_at": "2026-02-16T00:00:00Z",
            "poll_count": 5,
            "generation_count": 2,
            "kg_snapshot": {
                "episode_count": 8,
                "entity_count": 4,
                "edge_count": 2,
                "episode_hashes": ["x1", "x2"],
                "entity_uuids": ["e1"],
                "edge_fingerprints": ["f1"],
            },
            "projects": [{
                "id": "legacy-proj",
                "current_version": 2,
                "created_at": "2026-02-10T00:00:00Z",
                "updated_at": "2026-02-15T00:00:00Z",
                "versions": [
                    {"version": 1, "project_data": {"name": "Legacy v1"}, "mockup_dir": "m/v1", "mockup_files": []},
                    {"version": 2, "project_data": {"name": "Legacy v2"}, "mockup_dir": "m/v2", "mockup_files": []},
                ],
            }],
            "poll_log": [{"polled_at": "2026-02-16T00:00:00Z", "decision": "skip"}],
        }
        fresh_worker.save_state(legacy_state, "test-bonfire")

        graph_result = _make_graph_result(
            change_score=0.1,
            change_summary="no changes",
            mockup_results=[],
        )
        mock_ainvoke = AsyncMock(return_value=graph_result)

        with patch("worker.forge_graph") as mock_graph:
            mock_graph.ainvoke = mock_ainvoke
            fresh_worker._do_poll_cycle()

        state = fresh_worker.load_state("test-bonfire")
        assert len(state["projects"]) == 1
        assert state["projects"][0]["id"] == "legacy-proj"
        assert state["projects"][0]["current_version"] == 2
        assert len(state["projects"][0]["versions"]) == 2
        assert state["poll_count"] == 6
        assert state["generation_count"] == 2
        assert len(state["poll_log"]) == 2


# ---------------------------------------------------------------------------
# 6. Skip path logs correctly
# ---------------------------------------------------------------------------

class TestSkipPath:
    """When graph routes to END, no projects are modified."""

    def test_no_projects_modified_on_skip(self, fresh_worker: ForgeWorker):
        pre_state = {
            "version": 1,
            "projects": [{
                "id": "stable",
                "current_version": 1,
                "versions": [{"version": 1, "project_data": {"name": "Stable"}}],
            }],
            "kg_snapshot": {},
            "poll_log": [],
        }
        fresh_worker.save_state(pre_state, "test-bonfire")

        graph_result = _make_graph_result(
            change_score=0.05,
            change_summary="no changes",
            mockup_results=[],
            synthesized_projects=[],
        )
        mock_ainvoke = AsyncMock(return_value=graph_result)

        with patch("worker.forge_graph") as mock_graph:
            mock_graph.ainvoke = mock_ainvoke
            fresh_worker._do_poll_cycle()

        state = fresh_worker.load_state("test-bonfire")
        assert state["projects"][0]["current_version"] == 1
        assert state["poll_log"][-1]["decision"] == "skip"
        assert "last_generation_at" not in state or state["last_generation_at"] is None


# ---------------------------------------------------------------------------
# 7. Retired projects marked
# ---------------------------------------------------------------------------

class TestRetiredProjects:
    """Retired projects from synthesized_projects get retired_at timestamp."""

    def test_retired_project_marked(self, fresh_worker: ForgeWorker):
        pre_state = {
            "version": 1,
            "projects": [{
                "id": "to-retire",
                "current_version": 1,
                "versions": [{"version": 1, "project_data": {"name": "To Retire"}}],
            }],
            "kg_snapshot": {},
            "poll_log": [],
        }
        fresh_worker.save_state(pre_state, "test-bonfire")

        graph_result = _make_graph_result(
            change_score=0.5,
            synthesized_projects=[
                {"name": "To Retire", "status": "retired"},
            ],
            mockup_results=[],
        )
        mock_ainvoke = AsyncMock(return_value=graph_result)

        with patch("worker.forge_graph") as mock_graph:
            mock_graph.ainvoke = mock_ainvoke
            fresh_worker._do_poll_cycle()

        state = fresh_worker.load_state("test-bonfire")
        proj = state["projects"][0]
        assert "retired_at" in proj


# ---------------------------------------------------------------------------
# 8. Error handling
# ---------------------------------------------------------------------------

class TestErrorHandling:
    """Worker handles graph errors gracefully."""

    def test_graph_error_sets_status(self, fresh_worker: ForgeWorker):
        mock_ainvoke = AsyncMock(side_effect=RuntimeError("graph exploded"))

        with patch("worker.forge_graph") as mock_graph:
            mock_graph.ainvoke = mock_ainvoke
            fresh_worker._do_poll_cycle()

        assert fresh_worker.status == "error"
        assert fresh_worker.last_error == "graph exploded"

    def test_no_bonfire_skips_poll(self):
        w = ForgeWorker()
        w.current_bonfire_id = None
        w._do_poll_cycle()
        assert w.status == "idle"


# ---------------------------------------------------------------------------
# 9. Utility functions
# ---------------------------------------------------------------------------

class TestUtilities:
    """Preserved utility functions work correctly."""

    def test_find_project_found(self):
        state = {"projects": [{"id": "abc"}, {"id": "def"}]}
        assert _find_project(state, "def") == {"id": "def"}

    def test_find_project_not_found(self):
        state = {"projects": [{"id": "abc"}]}
        assert _find_project(state, "xyz") is None

    def test_slugify(self):
        assert _slugify("My Cool Project") == "my-cool-project"
        assert _slugify("AI/ML Tools") == "ai-ml-tools"
        assert _slugify("Don't Stop") == "dont-stop"
