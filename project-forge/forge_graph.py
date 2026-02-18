"""LangGraph pipeline for Project Forge.

Owns the full pipeline: KG extraction -> change detection -> synthesis -> mockup generation.
The worker invokes this graph via `forge_graph.ainvoke(initial_state)`.
"""

import hashlib
from datetime import datetime, timezone
from pathlib import Path
from typing import TypedDict

from langgraph.graph import END, StateGraph

import forge

# ---------------------------------------------------------------------------
# Helpers (moved from worker.py)
# ---------------------------------------------------------------------------

FORGE_DIR = Path(__file__).parent


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _build_snapshot(themes_data: dict) -> dict:
    """Build a diffable snapshot from raw themes data."""
    return {
        "polled_at": _now_iso(),
        "episode_count": len(themes_data.get("episodes", [])),
        "entity_count": len(themes_data.get("entities", [])),
        "edge_count": len(themes_data.get("edges", [])),
        "episode_hashes": sorted(set(
            hashlib.md5(ep["name"].encode()).hexdigest()[:12]
            for ep in themes_data.get("episodes", [])
        )),
        "entity_uuids": sorted(set(
            ent.get("uuid", ent["name"])
            for ent in themes_data.get("entities", [])
        )),
        "edge_fingerprints": sorted(set(
            f"{e.get('source_uuid', '')}|{e.get('target_uuid', '')}|{e.get('name', '')}"
            for e in themes_data.get("edges", [])
        )),
    }


def compute_change_score(old_snapshot: dict, new_snapshot: dict) -> tuple[float, str]:
    """Deterministic diff between two KG snapshots. Returns (score, reason)."""
    W_EPISODE = 0.5
    W_ENTITY = 0.3
    W_EDGE = 0.2

    old_eps = set(old_snapshot.get("episode_hashes", []))
    new_eps = set(new_snapshot.get("episode_hashes", []))
    old_ents = set(old_snapshot.get("entity_uuids", []))
    new_ents = set(new_snapshot.get("entity_uuids", []))
    old_edges = set(old_snapshot.get("edge_fingerprints", []))
    new_edges = set(new_snapshot.get("edge_fingerprints", []))

    added_eps = new_eps - old_eps
    added_ents = new_ents - old_ents
    added_edges = new_edges - old_edges

    ep_score = min(len(added_eps) / 5.0, 1.0)
    ent_score = min(len(added_ents) / 10.0, 1.0)
    edge_score = min(len(added_edges) / 15.0, 1.0)

    score = (ep_score * W_EPISODE) + (ent_score * W_ENTITY) + (edge_score * W_EDGE)

    reasons: list[str] = []
    if added_eps:
        reasons.append(f"{len(added_eps)} new episodes")
    if added_ents:
        reasons.append(f"{len(added_ents)} new entities")
    if added_edges:
        reasons.append(f"{len(added_edges)} new edges")

    return round(score, 3), ", ".join(reasons) if reasons else "no changes"


# ---------------------------------------------------------------------------
# State schema
# ---------------------------------------------------------------------------

class ForgeState(TypedDict, total=False):
    """Shared state flowing through all graph nodes."""
    # Worker-supplied inputs
    bonfire_id: str
    is_first_run: bool
    existing_projects: list[dict]
    old_kg_snapshot: dict
    change_threshold: float
    project_versions: dict[str, int]
    # Node outputs
    themes_data: dict
    new_kg_snapshot: dict
    change_score: float
    change_summary: str
    synthesized_projects: list[dict]
    mockup_results: list[dict]
    # Error accumulator
    errors: list[str]


# ---------------------------------------------------------------------------
# Slug helper
# ---------------------------------------------------------------------------

def _slugify(name: str) -> str:
    return name.lower().replace(" ", "-").replace("/", "-").replace("'", "")[:60]


# ---------------------------------------------------------------------------
# Node functions
# ---------------------------------------------------------------------------

async def extract_themes_node(state: ForgeState) -> dict:
    """Query the KG, build snapshot, compute change score."""
    bonfire_id = state["bonfire_id"]
    old_snapshot = state.get("old_kg_snapshot", {})

    themes_data = forge.extract_themes(bonfire_id=bonfire_id)
    new_snapshot = _build_snapshot(themes_data)
    score, reason = compute_change_score(old_snapshot, new_snapshot)

    return {
        "themes_data": themes_data,
        "new_kg_snapshot": new_snapshot,
        "change_score": score,
        "change_summary": reason,
    }


async def synthesize_initial_node(state: ForgeState) -> dict:
    """Generate initial batch of project ideas."""
    themes_data = state["themes_data"]
    result = await forge.synthesize_projects(themes_data)
    projects = result.get("projects", [])
    for p in projects:
        p.setdefault("status", "new")
    return {"synthesized_projects": projects}


async def synthesize_incremental_node(state: ForgeState) -> dict:
    """Update/add projects given new KG data and existing project list."""
    themes_data = state["themes_data"]
    existing_projects = state.get("existing_projects", [])
    change_summary = state.get("change_summary", "")
    result = await forge.synthesize_projects_with_existing(
        themes_data, existing_projects, change_summary,
    )
    return {"synthesized_projects": result.get("projects", [])}


async def generate_mockups_node(state: ForgeState) -> dict:
    """Generate HTML mockups for new/updated projects."""
    bonfire_id = state["bonfire_id"]
    project_versions = state.get("project_versions", {})
    synthesized = state.get("synthesized_projects", [])
    errors: list[str] = list(state.get("errors", []))
    mockup_results: list[dict] = []

    for project_data in synthesized:
        status = project_data.get("status", "new")
        if status in ("unchanged", "retired"):
            continue

        proj_id = _slugify(project_data.get("name", "unnamed"))
        current_version = project_versions.get(proj_id, 0)
        new_version = current_version + 1

        mockup_rel_dir = f"mockups/{bonfire_id}/{proj_id}/v{new_version}"
        mockup_abs_dir = str(FORGE_DIR / mockup_rel_dir)

        try:
            clean_data = {k: v for k, v in project_data.items() if k != "status"}
            result = await forge.generate_multi_mockup(clean_data, mockup_abs_dir)
            mockup_files = result.get("files", [])
        except Exception as exc:
            errors.append(f"Mockup failed for {proj_id}: {exc}")
            mockup_files = []

        mockup_results.append({
            "project_id": proj_id,
            "project_data": {k: v for k, v in project_data.items() if k != "status"},
            "status": status,
            "mockup_dir": mockup_rel_dir,
            "mockup_files": mockup_files,
        })

    return {"mockup_results": mockup_results, "errors": errors}


# ---------------------------------------------------------------------------
# Conditional routing
# ---------------------------------------------------------------------------

def route_after_extract(state: ForgeState) -> str:
    """Decide which synthesis path to take after theme extraction."""
    if state.get("is_first_run", False):
        return "synthesize_initial"
    change_score = state.get("change_score", 0.0)
    threshold = state.get("change_threshold", 0.3)
    if change_score >= threshold:
        return "synthesize_incremental"
    return END


# ---------------------------------------------------------------------------
# Graph assembly
# ---------------------------------------------------------------------------

def _build_graph() -> StateGraph:
    graph = StateGraph(ForgeState)

    graph.add_node("extract_themes", extract_themes_node)
    graph.add_node("synthesize_initial", synthesize_initial_node)
    graph.add_node("synthesize_incremental", synthesize_incremental_node)
    graph.add_node("generate_mockups", generate_mockups_node)

    graph.set_entry_point("extract_themes")

    graph.add_conditional_edges(
        "extract_themes",
        route_after_extract,
        {
            "synthesize_initial": "synthesize_initial",
            "synthesize_incremental": "synthesize_incremental",
            END: END,
        },
    )

    graph.add_edge("synthesize_initial", "generate_mockups")
    graph.add_edge("synthesize_incremental", "generate_mockups")
    graph.add_edge("generate_mockups", END)

    return graph


forge_graph = _build_graph().compile()
