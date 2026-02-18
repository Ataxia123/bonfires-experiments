"""LangGraph pipeline for Kindling Bonfires — G2G agreement negotiation.

5-node linear pipeline: read applicant KG → read donor KG → applicant proposes →
donor formalizes → publish agreement to stacks. Each node writes its step to MongoDB
immediately. Orchestration only; all I/O delegated to kindling.py.
"""

from datetime import datetime, timezone
from typing import Any, TypedDict

from langgraph.graph import END, StateGraph

import kindling

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _write_step(collection: Any, run_id: str, step_record: dict) -> None:
    """Append a step record to the run document."""
    collection.update_one(
        {"run_id": run_id},
        {"$push": {"steps": step_record}},
    )


# ---------------------------------------------------------------------------
# State schema
# ---------------------------------------------------------------------------


class KindlingState(TypedDict, total=False):
    """Shared state for the Kindling pipeline. All 12 fields."""

    run_id: str
    donor_id: str
    applicant_id: str
    mongo_collection: Any
    donor_kg: dict
    applicant_kg: dict
    applicant_statement: str
    formal_agreement: str
    donor_agent_id: str | None
    applicant_agent_id: str | None
    stack_publish_status: str | None
    errors: list[str]


# ---------------------------------------------------------------------------
# Node functions
# ---------------------------------------------------------------------------


async def read_applicant_bonfire(state: KindlingState) -> dict:
    """Step 1: Donor agent reads applicant's bonfire. Store applicant_kg, write Step 1 to MongoDB."""
    run_id = state["run_id"]
    applicant_id = state["applicant_id"]
    donor_id = state["donor_id"]
    collection = state["mongo_collection"]
    errors = list(state.get("errors", []))

    try:
        kg = kindling.read_bonfire(
            bonfire_id=applicant_id,
            reader_role="donor",
            counterparty_role="applicant",
            counterparty_bonfire_id=donor_id,
        )
    except Exception as e:
        errors.append(f"Step 1 read_applicant_bonfire: {e}")
        _write_step(collection, run_id, {
            "step": 1,
            "name": "read_applicant_bonfire",
            "agent_role": "donor",
            "bonfire_queried": applicant_id,
            "taxonomy_labels": [],
            "delve_query": "",
            "entities": [],
            "episodes": [],
            "edges": [],
            "completed_at": _now_iso(),
        })
        return {"applicant_kg": {}, "errors": errors}

    step_record = {
        "step": 1,
        "name": "read_applicant_bonfire",
        "agent_role": "donor",
        "bonfire_queried": applicant_id,
        "taxonomy_labels": kg.get("taxonomy_labels", []),
        "delve_query": kg.get("query", ""),
        "entities": kg.get("entities", []),
        "episodes": kg.get("episodes", []),
        "edges": kg.get("edges", []),
        "completed_at": _now_iso(),
    }
    _write_step(collection, run_id, step_record)
    return {"applicant_kg": kg, "errors": errors}


async def read_donor_bonfire(state: KindlingState) -> dict:
    """Step 2: Applicant agent reads donor's bonfire. Store donor_kg, write Step 2 to MongoDB."""
    run_id = state["run_id"]
    donor_id = state["donor_id"]
    applicant_id = state["applicant_id"]
    collection = state["mongo_collection"]
    errors = list(state.get("errors", []))

    try:
        kg = kindling.read_bonfire(
            bonfire_id=donor_id,
            reader_role="applicant",
            counterparty_role="donor",
            counterparty_bonfire_id=applicant_id,
        )
    except Exception as e:
        errors.append(f"Step 2 read_donor_bonfire: {e}")
        _write_step(collection, run_id, {
            "step": 2,
            "name": "read_donor_bonfire",
            "agent_role": "applicant",
            "bonfire_queried": donor_id,
            "taxonomy_labels": [],
            "delve_query": "",
            "entities": [],
            "episodes": [],
            "edges": [],
            "completed_at": _now_iso(),
        })
        return {"donor_kg": {}, "errors": errors}

    step_record = {
        "step": 2,
        "name": "read_donor_bonfire",
        "agent_role": "applicant",
        "bonfire_queried": donor_id,
        "taxonomy_labels": kg.get("taxonomy_labels", []),
        "delve_query": kg.get("query", ""),
        "entities": kg.get("entities", []),
        "episodes": kg.get("episodes", []),
        "edges": kg.get("edges", []),
        "completed_at": _now_iso(),
    }
    _write_step(collection, run_id, step_record)
    return {"donor_kg": kg, "errors": errors}


async def applicant_proposes(state: KindlingState) -> dict:
    """Step 3: Build role context (applicant=self, donor=other), call LLM for proposal, write Step 3."""
    run_id = state["run_id"]
    applicant_kg = state.get("applicant_kg") or {}
    donor_kg = state.get("donor_kg") or {}
    collection = state["mongo_collection"]
    errors = list(state.get("errors", []))

    context = kindling.build_role_context(
        applicant_kg, donor_kg, "applicant", "donor"
    )
    prompt = f"""You are the applicant in a donor↔applicant negotiation. Based on both knowledge graphs below, write a short, formal proposal statement (2–4 sentences) that you would offer to the donor. Be specific about what you can contribute and what you seek. Do not use markdown or headers — just the proposal text.

{context}

Proposal:"""

    try:
        statement = await kindling.call_llm(prompt)
    except Exception as e:
        errors.append(f"Step 3 applicant_proposes: {e}")
        statement = ""

    step_record = {
        "step": 3,
        "name": "applicant_proposes",
        "agent_role": "applicant",
        "llm_output": statement,
        "completed_at": _now_iso(),
    }
    _write_step(collection, run_id, step_record)
    return {"applicant_statement": statement.strip(), "errors": errors}


async def donor_formalizes(state: KindlingState) -> dict:
    """Step 4: Build role context (donor=self, applicant=other), call LLM for formal agreement, write Step 4."""
    run_id = state["run_id"]
    donor_kg = state.get("donor_kg") or {}
    applicant_kg = state.get("applicant_kg") or {}
    applicant_statement = state.get("applicant_statement", "")
    collection = state["mongo_collection"]
    errors = list(state.get("errors", []))

    context = kindling.build_role_context(
        donor_kg, applicant_kg, "donor", "applicant"
    )
    prompt = f"""You are the donor in a donor↔applicant negotiation. Below are both knowledge graphs and the applicant's proposal. Write a short, binding formal agreement (3–6 sentences) that incorporates the proposal and reflects what both parties commit to. Be specific and actionable. Do not use markdown or headers — just the agreement text.

{context}

Applicant's proposal:
{applicant_statement}

Formal agreement:"""

    try:
        agreement = await kindling.call_llm(prompt)
    except Exception as e:
        errors.append(f"Step 4 donor_formalizes: {e}")
        agreement = ""

    step_record = {
        "step": 4,
        "name": "donor_formalizes",
        "agent_role": "donor",
        "llm_output": agreement,
        "completed_at": _now_iso(),
    }
    _write_step(collection, run_id, step_record)
    return {"formal_agreement": agreement.strip(), "errors": errors}


async def publish_agreement_to_stacks(state: KindlingState) -> dict:
    """Step 5 (best-effort): Resolve representative agents, publish agreement to both stacks, write Step 5."""
    run_id = state["run_id"]
    donor_id = state["donor_id"]
    applicant_id = state["applicant_id"]
    formal_agreement = state.get("formal_agreement") or ""
    collection = state["mongo_collection"]

    donor_agent_id: str | None = None
    applicant_agent_id: str | None = None
    donor_message_ids: list[str] | None = None
    applicant_message_ids: list[str] | None = None
    status = "failed"
    err_msg: str | None = None

    try:
        donor_agents = kindling.get_bonfire_agents(donor_id)
        applicant_agents = kindling.get_bonfire_agents(applicant_id)
        donor_agent = kindling.select_representative_agent(donor_agents)
        applicant_agent = kindling.select_representative_agent(applicant_agents)

        if donor_agent:
            donor_agent_id = donor_agent.get("id") or donor_agent.get("_id") or str(donor_agent.get("agent_id", ""))
            if not donor_agent_id and "id" in donor_agent:
                donor_agent_id = str(donor_agent["id"])
        if applicant_agent:
            applicant_agent_id = applicant_agent.get("id") or applicant_agent.get("_id") or str(applicant_agent.get("agent_id", ""))
            if not applicant_agent_id and "id" in applicant_agent:
                applicant_agent_id = str(applicant_agent["id"])

        donor_ok = False
        applicant_ok = False

        if donor_agent_id and formal_agreement:
            resp = kindling.add_agreement_message_to_stack(
                donor_agent_id, formal_agreement, run_id,
                "donor", donor_id, applicant_id,
            )
            if "error" not in resp:
                donor_message_ids = resp.get("message_ids") or resp.get("message_id") or []
                if isinstance(donor_message_ids, str):
                    donor_message_ids = [donor_message_ids]
                donor_ok = True
            else:
                err_msg = resp.get("error", str(resp))

        if applicant_agent_id and formal_agreement:
            resp = kindling.add_agreement_message_to_stack(
                applicant_agent_id, formal_agreement, run_id,
                "applicant", applicant_id, donor_id,
            )
            if "error" not in resp:
                applicant_message_ids = resp.get("message_ids") or resp.get("message_id") or []
                if isinstance(applicant_message_ids, str):
                    applicant_message_ids = [applicant_message_ids]
                applicant_ok = True
            elif not err_msg:
                err_msg = resp.get("error", str(resp))

        if donor_ok and applicant_ok:
            status = "ok"
        elif donor_ok or applicant_ok:
            status = "partial"
    except Exception as e:
        err_msg = str(e)

    step_record = {
        "step": 5,
        "name": "publish_agreement_to_stacks",
        "agent_role": "system",
        "stack_publish": {
            "donor_agent_id": donor_agent_id,
            "applicant_agent_id": applicant_agent_id,
            "donor_message_ids": donor_message_ids,
            "applicant_message_ids": applicant_message_ids,
            "status": status,
            "error": err_msg,
        },
        "completed_at": _now_iso(),
    }
    _write_step(collection, run_id, step_record)
    return {
        "donor_agent_id": donor_agent_id,
        "applicant_agent_id": applicant_agent_id,
        "stack_publish_status": status,
    }


# ---------------------------------------------------------------------------
# Graph assembly
# ---------------------------------------------------------------------------


def _build_graph() -> StateGraph:
    graph = StateGraph(KindlingState)

    graph.add_node("read_applicant_bonfire", read_applicant_bonfire)
    graph.add_node("read_donor_bonfire", read_donor_bonfire)
    graph.add_node("applicant_proposes", applicant_proposes)
    graph.add_node("donor_formalizes", donor_formalizes)
    graph.add_node("publish_agreement_to_stacks", publish_agreement_to_stacks)

    graph.set_entry_point("read_applicant_bonfire")
    graph.add_edge("read_applicant_bonfire", "read_donor_bonfire")
    graph.add_edge("read_donor_bonfire", "applicant_proposes")
    graph.add_edge("applicant_proposes", "donor_formalizes")
    graph.add_edge("donor_formalizes", "publish_agreement_to_stacks")
    graph.add_edge("publish_agreement_to_stacks", END)

    return graph


kindling_graph = _build_graph().compile()
