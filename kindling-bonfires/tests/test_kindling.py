"""Unit tests for kindling.py and kindling_graph.py — pure functions and state schema."""

import sys
from pathlib import Path
from typing import get_type_hints
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

# Import after conftest sets OPENROUTER_API_KEY
import kindling
from kindling_graph import KindlingState, kindling_graph


# ---------------------------------------------------------------------------
# build_role_aware_delve_query
# ---------------------------------------------------------------------------


class TestBuildRoleAwareDelveQuery:
    def test_includes_target_and_counterparty_labels(self):
        out = kindling.build_role_aware_delve_query(
            target_bonfire_id="bf1",
            target_taxonomy_labels=["research", "protocols"],
            reader_role="donor",
            counterparty_role="applicant",
            counterparty_taxonomy_labels=["design", "open-source"],
        )
        assert "donor↔applicant" in out
        assert "research" in out and "protocols" in out
        assert "design" in out and "open-source" in out
        assert "donor" in out and "applicant" in out
        assert "Retrieve the most important" in out

    def test_handles_empty_labels(self):
        out = kindling.build_role_aware_delve_query(
            target_bonfire_id="bf1",
            target_taxonomy_labels=[],
            reader_role="applicant",
            counterparty_role="donor",
            counterparty_taxonomy_labels=[],
        )
        assert "(none)" in out
        assert "applicant" in out and "donor" in out


# ---------------------------------------------------------------------------
# build_role_context
# ---------------------------------------------------------------------------


class TestBuildRoleContext:
    def test_includes_your_bonfire_and_their_bonfire_headers(self):
        self_kg = {
            "bonfire_id": "bf-self",
            "entities": [{"name": "A", "uuid": "u1"}],
            "episodes": [{"name": "Ep1", "content_preview": "preview"}],
            "edges": [{"name": "rel", "source_uuid": "u1", "target_uuid": "u2"}],
        }
        other_kg = {
            "bonfire_id": "bf-other",
            "entities": [{"name": "B", "uuid": "u2"}],
            "episodes": [],
            "edges": [],
        }
        out = kindling.build_role_context(self_kg, other_kg, "donor", "applicant")
        assert "YOUR BONFIRE" in out
        assert "THEIR BONFIRE" in out
        assert "role: Donor" in out
        assert "role: Applicant" in out
        assert "bonfire_id: bf-self" in out
        assert "bonfire_id: bf-other" in out
        assert "Episodes" in out and "Key Entities" in out and "Relationships" in out
        assert "A" in out and "B" in out

    def test_handles_empty_kg(self):
        out = kindling.build_role_context({}, {}, "applicant", "donor")
        assert "YOUR BONFIRE" in out and "THEIR BONFIRE" in out
        assert "(none)" in out


# ---------------------------------------------------------------------------
# select_representative_agent
# ---------------------------------------------------------------------------


class TestSelectRepresentativeAgent:
    def test_returns_first_active_agent(self):
        agents = [
            {"id": "ag1", "is_active": False},
            {"id": "ag2", "is_active": True},
            {"id": "ag3", "is_active": True},
        ]
        selected = kindling.select_representative_agent(agents)
        assert selected is not None
        assert selected["id"] == "ag2"

    def test_returns_first_agent_when_none_active(self):
        agents = [
            {"id": "ag1", "is_active": False},
            {"id": "ag2", "is_active": False},
        ]
        selected = kindling.select_representative_agent(agents)
        assert selected is not None
        assert selected["id"] == "ag1"

    def test_returns_none_for_empty_list(self):
        assert kindling.select_representative_agent([]) is None

    def test_returns_only_agent(self):
        agents = [{"id": "solo", "is_active": False}]
        assert kindling.select_representative_agent(agents)["id"] == "solo"


# ---------------------------------------------------------------------------
# KindlingState TypedDict
# ---------------------------------------------------------------------------


class TestKindlingState:
    def test_has_all_twelve_fields(self):
        required = {
            "run_id",
            "donor_id",
            "applicant_id",
            "mongo_collection",
            "donor_kg",
            "applicant_kg",
            "applicant_statement",
            "formal_agreement",
            "donor_agent_id",
            "applicant_agent_id",
            "stack_publish_status",
            "errors",
        }
        try:
            annotations = get_type_hints(KindlingState)
        except Exception:
            annotations = getattr(KindlingState, "__annotations__", {})
        for field in required:
            assert field in annotations, f"missing {field}"
        assert len(required) == 12


# ---------------------------------------------------------------------------
# kindling_graph compiled
# ---------------------------------------------------------------------------


class TestKindlingGraphCompiled:
    def test_kindling_graph_is_importable(self):
        assert kindling_graph is not None

    def test_kindling_graph_has_ainvoke(self):
        assert hasattr(kindling_graph, "ainvoke")

    @pytest.mark.asyncio
    async def test_graph_invocation_with_mocked_io(self):
        """Graph runs with mocked kindling I/O and a mock Mongo collection."""
        mock_coll = MagicMock()
        kg = {
            "entities": [],
            "episodes": [],
            "edges": [],
            "bonfire_id": "bf1",
            "taxonomy_labels": [],
            "query": "q",
        }
        with (
            patch("kindling_graph.kindling.read_bonfire", return_value=kg),
            patch("kindling_graph.kindling.call_llm", new_callable=AsyncMock, side_effect=["proposal", "formal agreement text"]),
            patch("kindling_graph.kindling.get_bonfire_agents", return_value=[]),
        ):
            result = await kindling_graph.ainvoke({
                "run_id": "run-1",
                "donor_id": "donor-bf",
                "applicant_id": "applicant-bf",
                "mongo_collection": mock_coll,
            })
        assert result.get("applicant_kg") is not None
        assert result.get("donor_kg") is not None
        assert result.get("applicant_statement") == "proposal"
        assert result.get("formal_agreement") == "formal agreement text"
        assert mock_coll.update_one.call_count >= 5
