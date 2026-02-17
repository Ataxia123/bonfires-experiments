"""Tests for forge_graph.py — LangGraph pipeline."""

import asyncio
import sys
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

from forge_graph import (
    ForgeState,
    _build_snapshot,
    compute_change_score,
    extract_themes_node,
    forge_graph,
    generate_mockups_node,
    route_after_extract,
    synthesize_incremental_node,
    synthesize_initial_node,
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def sample_themes_data() -> dict:
    return {
        "episodes": [
            {"name": "Ep 1", "content_preview": "content1"},
            {"name": "Ep 2", "content_preview": "content2"},
        ],
        "entities": [
            {"name": "Entity A", "uuid": "uuid-a"},
            {"name": "Entity B", "uuid": "uuid-b"},
        ],
        "edges": [
            {"name": "relates", "source_uuid": "uuid-a", "target_uuid": "uuid-b"},
        ],
        "query_count": 7,
        "episode_count": 2,
        "entity_count": 2,
        "edge_count": 1,
    }


@pytest.fixture
def sample_project() -> dict:
    return {
        "name": "Test Project",
        "tagline": "A test",
        "description": "Test description",
        "themes": ["ai"],
        "tech_stack": ["python"],
        "complexity": "weekend",
        "key_insight": "insight",
        "first_step": "step1",
        "status": "new",
    }


# ---------------------------------------------------------------------------
# _build_snapshot
# ---------------------------------------------------------------------------

class TestBuildSnapshot:
    def test_returns_required_keys(self, sample_themes_data: dict):
        snapshot = _build_snapshot(sample_themes_data)
        assert "polled_at" in snapshot
        assert "episode_count" in snapshot
        assert "entity_count" in snapshot
        assert "edge_count" in snapshot
        assert "episode_hashes" in snapshot
        assert "entity_uuids" in snapshot
        assert "edge_fingerprints" in snapshot

    def test_counts_match_input(self, sample_themes_data: dict):
        snapshot = _build_snapshot(sample_themes_data)
        assert snapshot["episode_count"] == 2
        assert snapshot["entity_count"] == 2
        assert snapshot["edge_count"] == 1

    def test_hashes_are_sorted_and_unique(self, sample_themes_data: dict):
        snapshot = _build_snapshot(sample_themes_data)
        assert snapshot["episode_hashes"] == sorted(set(snapshot["episode_hashes"]))
        assert snapshot["entity_uuids"] == sorted(set(snapshot["entity_uuids"]))

    def test_empty_input(self):
        snapshot = _build_snapshot({})
        assert snapshot["episode_count"] == 0
        assert snapshot["entity_count"] == 0
        assert snapshot["edge_count"] == 0
        assert snapshot["episode_hashes"] == []
        assert snapshot["entity_uuids"] == []
        assert snapshot["edge_fingerprints"] == []


# ---------------------------------------------------------------------------
# compute_change_score
# ---------------------------------------------------------------------------

class TestComputeChangeScore:
    def test_identical_snapshots_score_zero(self, sample_themes_data: dict):
        snapshot = _build_snapshot(sample_themes_data)
        score, reason = compute_change_score(snapshot, snapshot)
        assert score == 0.0
        assert reason == "no changes"

    def test_empty_old_snapshot_scores_positive(self, sample_themes_data: dict):
        new_snapshot = _build_snapshot(sample_themes_data)
        score, reason = compute_change_score({}, new_snapshot)
        assert score > 0.0
        assert "new episodes" in reason

    def test_score_capped_at_one(self):
        huge_data = {
            "episodes": [{"name": f"ep-{i}"} for i in range(100)],
            "entities": [{"name": f"ent-{i}", "uuid": f"u-{i}"} for i in range(100)],
            "edges": [
                {"name": f"e-{i}", "source_uuid": f"s-{i}", "target_uuid": f"t-{i}"}
                for i in range(100)
            ],
        }
        new_snapshot = _build_snapshot(huge_data)
        score, _ = compute_change_score({}, new_snapshot)
        assert score <= 1.0


# ---------------------------------------------------------------------------
# route_after_extract
# ---------------------------------------------------------------------------

class TestRouteAfterExtract:
    def test_first_run_routes_to_synthesize_initial(self):
        state: ForgeState = {
            "is_first_run": True,
            "change_score": 0.0,
            "change_threshold": 0.3,
            "bonfire_id": "test",
        }
        assert route_after_extract(state) == "synthesize_initial"

    def test_high_score_routes_to_synthesize_incremental(self):
        state: ForgeState = {
            "is_first_run": False,
            "change_score": 0.5,
            "change_threshold": 0.3,
            "bonfire_id": "test",
        }
        assert route_after_extract(state) == "synthesize_incremental"

    def test_low_score_routes_to_end(self):
        from langgraph.graph import END
        state: ForgeState = {
            "is_first_run": False,
            "change_score": 0.1,
            "change_threshold": 0.3,
            "bonfire_id": "test",
        }
        assert route_after_extract(state) == END

    def test_score_at_threshold_routes_to_synthesize_incremental(self):
        state: ForgeState = {
            "is_first_run": False,
            "change_score": 0.3,
            "change_threshold": 0.3,
            "bonfire_id": "test",
        }
        assert route_after_extract(state) == "synthesize_incremental"

    def test_first_run_takes_priority_over_score(self):
        state: ForgeState = {
            "is_first_run": True,
            "change_score": 0.0,
            "change_threshold": 0.3,
            "bonfire_id": "test",
        }
        assert route_after_extract(state) == "synthesize_initial"


# ---------------------------------------------------------------------------
# Node functions
# ---------------------------------------------------------------------------

class TestExtractThemesNode:
    @pytest.mark.asyncio
    async def test_populates_state_fields(self, sample_themes_data: dict):
        with patch("forge_graph.forge.extract_themes", return_value=sample_themes_data):
            result = await extract_themes_node({
                "bonfire_id": "test-bf",
                "old_kg_snapshot": {},
            })

        assert "themes_data" in result
        assert "new_kg_snapshot" in result
        assert "change_score" in result
        assert "change_summary" in result
        assert isinstance(result["change_score"], float)


class TestSynthesizeInitialNode:
    @pytest.mark.asyncio
    async def test_calls_forge_synthesize_projects(self, sample_themes_data: dict):
        mock_result = {
            "projects": [
                {"name": "P1", "status": "new"},
                {"name": "P2"},
            ]
        }
        with patch("forge_graph.forge.synthesize_projects", new_callable=AsyncMock, return_value=mock_result):
            result = await synthesize_initial_node({"themes_data": sample_themes_data})

        assert len(result["synthesized_projects"]) == 2
        assert all(p.get("status") == "new" for p in result["synthesized_projects"])


class TestSynthesizeIncrementalNode:
    @pytest.mark.asyncio
    async def test_calls_forge_synthesize_with_existing(self, sample_themes_data: dict):
        mock_result = {
            "projects": [
                {"name": "P1", "status": "unchanged"},
                {"name": "P2", "status": "updated"},
            ]
        }
        with patch(
            "forge_graph.forge.synthesize_projects_with_existing",
            new_callable=AsyncMock,
            return_value=mock_result,
        ):
            result = await synthesize_incremental_node({
                "themes_data": sample_themes_data,
                "existing_projects": [{"name": "P1"}],
                "change_summary": "2 new episodes",
            })

        assert len(result["synthesized_projects"]) == 2


class TestGenerateMockupsNode:
    @pytest.mark.asyncio
    async def test_skips_unchanged_and_retired(self):
        projects = [
            {"name": "Kept", "status": "unchanged"},
            {"name": "Old", "status": "retired"},
            {"name": "Fresh", "status": "new"},
        ]
        with patch(
            "forge_graph.forge.generate_multi_mockup",
            new_callable=AsyncMock,
            return_value={"files": [{"name": "index.html"}]},
        ):
            result = await generate_mockups_node({
                "bonfire_id": "test-bf",
                "project_versions": {},
                "synthesized_projects": projects,
            })

        assert len(result["mockup_results"]) == 1
        assert result["mockup_results"][0]["project_id"] == "fresh"

    @pytest.mark.asyncio
    async def test_version_numbering_new_project(self):
        projects = [{"name": "Brand New", "status": "new"}]
        with patch(
            "forge_graph.forge.generate_multi_mockup",
            new_callable=AsyncMock,
            return_value={"files": []},
        ):
            result = await generate_mockups_node({
                "bonfire_id": "bf-1",
                "project_versions": {},
                "synthesized_projects": projects,
            })

        assert "v1" in result["mockup_results"][0]["mockup_dir"]

    @pytest.mark.asyncio
    async def test_version_numbering_existing_project(self):
        projects = [{"name": "Updated Proj", "status": "updated"}]
        with patch(
            "forge_graph.forge.generate_multi_mockup",
            new_callable=AsyncMock,
            return_value={"files": []},
        ):
            result = await generate_mockups_node({
                "bonfire_id": "bf-1",
                "project_versions": {"updated-proj": 3},
                "synthesized_projects": projects,
            })

        assert "v4" in result["mockup_results"][0]["mockup_dir"]

    @pytest.mark.asyncio
    async def test_mockup_error_accumulated(self):
        projects = [{"name": "Broken", "status": "new"}]
        with patch(
            "forge_graph.forge.generate_multi_mockup",
            new_callable=AsyncMock,
            side_effect=RuntimeError("LLM down"),
        ):
            result = await generate_mockups_node({
                "bonfire_id": "bf-1",
                "project_versions": {},
                "synthesized_projects": projects,
            })

        assert len(result["errors"]) == 1
        assert "Broken" in result["errors"][0] or "broken" in result["errors"][0]
        assert result["mockup_results"][0]["mockup_files"] == []


# ---------------------------------------------------------------------------
# Compiled graph
# ---------------------------------------------------------------------------

class TestCompiledGraph:
    def test_forge_graph_is_importable(self):
        assert forge_graph is not None

    def test_forge_graph_has_ainvoke(self):
        assert hasattr(forge_graph, "ainvoke")

    @pytest.mark.asyncio
    async def test_full_initial_run(self, sample_themes_data: dict, sample_project: dict):
        """End-to-end: first run goes extract -> synthesize_initial -> generate_mockups."""
        mock_synth = {"projects": [sample_project]}

        with (
            patch("forge_graph.forge.extract_themes", return_value=sample_themes_data),
            patch("forge_graph.forge.synthesize_projects", new_callable=AsyncMock, return_value=mock_synth),
            patch("forge_graph.forge.generate_multi_mockup", new_callable=AsyncMock, return_value={"files": [{"name": "index.html"}]}),
        ):
            result = await forge_graph.ainvoke({
                "bonfire_id": "test-bf",
                "is_first_run": True,
                "existing_projects": [],
                "old_kg_snapshot": {},
                "change_threshold": 0.3,
                "project_versions": {},
            })

        assert result["themes_data"] is not None
        assert result["change_score"] >= 0
        assert len(result["synthesized_projects"]) == 1
        assert len(result["mockup_results"]) == 1

    @pytest.mark.asyncio
    async def test_skip_path(self, sample_themes_data: dict):
        """Low change score + not first run -> END without synthesis."""
        old_snapshot = _build_snapshot(sample_themes_data)

        with patch("forge_graph.forge.extract_themes", return_value=sample_themes_data):
            result = await forge_graph.ainvoke({
                "bonfire_id": "test-bf",
                "is_first_run": False,
                "existing_projects": [{"name": "P1"}],
                "old_kg_snapshot": old_snapshot,
                "change_threshold": 0.3,
                "project_versions": {},
            })

        assert result["change_score"] == 0.0
        assert result.get("synthesized_projects") is None or result.get("synthesized_projects") == []
        assert result.get("mockup_results") is None or result.get("mockup_results") == []

    @pytest.mark.asyncio
    async def test_incremental_path(self, sample_themes_data: dict, sample_project: dict):
        """High change score + not first run -> synthesize_incremental -> generate_mockups."""
        sample_project["status"] = "updated"
        mock_synth = {"projects": [sample_project]}

        with (
            patch("forge_graph.forge.extract_themes", return_value=sample_themes_data),
            patch(
                "forge_graph.forge.synthesize_projects_with_existing",
                new_callable=AsyncMock,
                return_value=mock_synth,
            ),
            patch(
                "forge_graph.forge.generate_multi_mockup",
                new_callable=AsyncMock,
                return_value={"files": [{"name": "index.html"}]},
            ),
        ):
            result = await forge_graph.ainvoke({
                "bonfire_id": "test-bf",
                "is_first_run": False,
                "existing_projects": [{"name": "Old Proj"}],
                "old_kg_snapshot": {},
                "change_threshold": 0.2,
                "project_versions": {},
            })

        assert result["change_score"] > 0
        assert len(result["synthesized_projects"]) == 1
        assert len(result["mockup_results"]) == 1
