"""Kindling Bonfires — G2G agreement negotiation I/O and LLM helpers.

All external calls (Bonfires REST API, OpenRouter) live here. No graph logic.
Pattern mirrors project-forge/forge.py.
"""

import json
import os
import urllib.request
from datetime import datetime, timezone

import openai

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------
DELVE_API_KEY = os.environ.get("DELVE_API_KEY", "8n5l-sJnrHjywrTnJ3rJCjo1f1uLyTPYy_yLgq_bf-d")
DELVE_BASE_URL = os.environ.get("DELVE_BASE_URL", "https://tnt-v2.api.bonfires.ai")

OPENROUTER_API_KEY = os.environ.get("OPENROUTER_API_KEY", "")
if not OPENROUTER_API_KEY:
    raise OSError("OPENROUTER_API_KEY environment variable is required")
OPENROUTER_MODEL = os.environ.get("OPENROUTER_MODEL", "anthropic/claude-sonnet-4-5")

_llm_client = openai.AsyncOpenAI(
    base_url="https://openrouter.ai/api/v1",
    api_key=OPENROUTER_API_KEY,
)

# ---------------------------------------------------------------------------
# Delve (KG retrieval)
# ---------------------------------------------------------------------------


def delve(query: str, bonfire_id: str, num_results: int = 20) -> dict:
    """Synchronous POST /delve call. Returns {entities, episodes, edges}."""
    payload = json.dumps({
        "query": query,
        "bonfire_id": bonfire_id,
        "num_results": num_results,
    }).encode()
    req = urllib.request.Request(
        f"{DELVE_BASE_URL}/delve",
        data=payload,
        headers={
            "Content-Type": "application/json",
            "Authorization": f"Bearer {DELVE_API_KEY}",
        },
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=60) as resp:
        return json.loads(resp.read())


def get_bonfire_taxonomy_labels(bonfire_id: str) -> list[str]:
    """Best-effort fetch of taxonomy labels for a bonfire. Returns [] on failure."""
    try:
        req = urllib.request.Request(
            f"{DELVE_BASE_URL}/bonfires/{bonfire_id}",
            headers={"Authorization": f"Bearer {DELVE_API_KEY}"},
            method="GET",
        )
        with urllib.request.urlopen(req, timeout=30) as resp:
            data = json.loads(resp.read())
        labels = data.get("taxonomy_labels") or data.get("labels") or []
        return labels if isinstance(labels, list) else []
    except Exception:
        pass
    try:
        req = urllib.request.Request(
            f"{DELVE_BASE_URL}/bonfires",
            headers={"Authorization": f"Bearer {DELVE_API_KEY}"},
            method="GET",
        )
        with urllib.request.urlopen(req, timeout=30) as resp:
            data = json.loads(resp.read())
        bonfires = data.get("bonfires") or data.get("data") or []
        if isinstance(bonfires, list):
            for bf in bonfires:
                bf_id = bf.get("id") or bf.get("_id") or bf.get("bonfire_id")
                if str(bf_id) == str(bonfire_id):
                    labels = bf.get("taxonomy_labels") or bf.get("labels") or []
                    return labels if isinstance(labels, list) else []
    except Exception:
        pass
    return []


def build_role_aware_delve_query(
    target_bonfire_id: str,
    target_taxonomy_labels: list[str],
    reader_role: str,
    counterparty_role: str,
    counterparty_taxonomy_labels: list[str],
) -> str:
    """Construct a natural-language query for /delve with role and taxonomy context."""
    target_labels = ", ".join(target_taxonomy_labels) if target_taxonomy_labels else "(none)"
    counterparty_labels = ", ".join(counterparty_taxonomy_labels) if counterparty_taxonomy_labels else "(none)"
    return (
        f"You are building context for a donor↔applicant negotiation. "
        f"Target bonfire taxonomy labels: {target_labels}. "
        f"Counterparty taxonomy labels: {counterparty_labels}. "
        f"Retrieve the most important people/organizations/concepts, current goals/needs, "
        f"capabilities/resources, constraints/risks, and any existing collaborations or "
        f"agreements that would matter to a {reader_role} negotiating with a {counterparty_role}."
    )


def read_bonfire(
    bonfire_id: str,
    reader_role: str,
    counterparty_role: str,
    counterparty_bonfire_id: str,
) -> dict:
    """
    Run one /delve call for the target bonfire using a role-aware query.
    Returns {entities, episodes, edges, bonfire_id, taxonomy_labels, query}.
    Episodes are normalized to {name, content_preview} (content truncated to 500 chars).
    """
    target_labels = get_bonfire_taxonomy_labels(bonfire_id)
    counterparty_labels = get_bonfire_taxonomy_labels(counterparty_bonfire_id)
    query = build_role_aware_delve_query(
        bonfire_id, target_labels, reader_role, counterparty_role, counterparty_labels
    )
    data = delve(query, bonfire_id=bonfire_id, num_results=20)

    episodes_out: list[dict] = []
    for ep in data.get("episodes", []):
        content = ep.get("content", "")
        if isinstance(content, str) and content.startswith("{"):
            try:
                parsed = json.loads(content)
                content = parsed.get("content", content)
            except json.JSONDecodeError:
                pass
        preview = str(content)[:500]
        episodes_out.append({
            "name": ep.get("name", ""),
            "content_preview": preview,
        })

    entities_out = [
        {"name": ent.get("name", ""), "uuid": ent.get("uuid", "")}
        for ent in data.get("entities", [])
        if ent.get("uuid")
    ]
    edges_out = [
        {
            "name": e.get("name", ""),
            "source_uuid": e.get("source_uuid", ""),
            "target_uuid": e.get("target_uuid", ""),
        }
        for e in data.get("edges", [])
    ]

    return {
        "entities": entities_out,
        "episodes": episodes_out,
        "edges": edges_out,
        "bonfire_id": bonfire_id,
        "taxonomy_labels": target_labels,
        "query": query,
    }


# ---------------------------------------------------------------------------
# Role context for LLM prompts
# ---------------------------------------------------------------------------


def build_role_context(
    self_kg: dict,
    other_kg: dict,
    self_role: str,
    other_role: str,
) -> str:
    """Format both KG datasets with explicit YOUR BONFIRE / THEIR BONFIRE headers."""
    self_id = self_kg.get("bonfire_id", "")
    other_id = other_kg.get("bonfire_id", "")
    role_cap = self_role.capitalize()
    other_cap = other_role.capitalize()

    def section(kg: dict, title: str) -> str:
        episodes = kg.get("episodes", [])
        entities = kg.get("entities", [])
        edges = kg.get("edges", [])
        ep_lines = "\n".join(f"- {e.get('name', '')}: {e.get('content_preview', '')[:200]}" for e in episodes[:40])
        ent_names = ", ".join(ent.get("name", "") for ent in entities[:60])
        edge_lines = "\n".join(f"- {e.get('name', '')}" for e in edges[:30])
        return f"""## {title}
### Episodes
{ep_lines or "(none)"}

### Key Entities
{ent_names or "(none)"}

### Relationships
{edge_lines or "(none)"}"""

    self_block = section(self_kg, f"YOUR BONFIRE (role: {role_cap} | bonfire_id: {self_id})")
    other_block = section(other_kg, f"THEIR BONFIRE (role: {other_cap} | bonfire_id: {other_id})")
    return f"{self_block}\n\n{other_block}"


# ---------------------------------------------------------------------------
# LLM
# ---------------------------------------------------------------------------


async def call_llm(prompt: str) -> str:
    """Call LLM via OpenRouter (OpenAI-compatible API)."""
    response = await _llm_client.chat.completions.create(
        model=OPENROUTER_MODEL,
        messages=[{"role": "user", "content": prompt}],
    )
    return response.choices[0].message.content or ""


# ---------------------------------------------------------------------------
# Bonfire agents and stack publishing
# ---------------------------------------------------------------------------


def get_bonfire_agents(bonfire_id: str) -> list[dict]:
    """GET /bonfires/{bonfire_id}/agents. Returns list of agents or [] on failure."""
    try:
        req = urllib.request.Request(
            f"{DELVE_BASE_URL}/bonfires/{bonfire_id}/agents",
            headers={"Authorization": f"Bearer {DELVE_API_KEY}"},
            method="GET",
        )
        with urllib.request.urlopen(req, timeout=30) as resp:
            data = json.loads(resp.read())
        agents = data.get("agents") or data.get("data") or []
        return agents if isinstance(agents, list) else []
    except Exception:
        return []


def select_representative_agent(agents: list[dict]) -> dict | None:
    """Deterministic: first is_active=true agent; else first agent; else None."""
    if not agents:
        return None
    for a in agents:
        if a.get("is_active") is True:
            return a
    return agents[0]


def add_agreement_message_to_stack(
    agent_id: str,
    agreement_text: str,
    run_id: str,
    self_role: str,
    self_bonfire_id: str,
    other_bonfire_id: str,
) -> dict:
    """
    POST /agents/{agent_id}/stack/add with synthetic agreement message.
    Returns response body or error dict.
    """
    header = "[Kindling Agreement]\n\n"
    text = header + agreement_text
    now = datetime.now(timezone.utc).isoformat()
    payload = {
        "text": text,
        "chatId": f"kindling:{run_id}",
        "userId": f"bonfire:{other_bonfire_id}",
        "agentId": agent_id,
        "timestamp": now,
        "telegramMeta": {
            "kindling_run_id": run_id,
            "type": "agreement",
            "self_role": self_role,
            "self_bonfire_id": self_bonfire_id,
            "other_bonfire_id": other_bonfire_id,
        },
    }
    body = json.dumps(payload).encode()
    try:
        req = urllib.request.Request(
            f"{DELVE_BASE_URL}/agents/{agent_id}/stack/add",
            data=body,
            headers={
                "Content-Type": "application/json",
                "Authorization": f"Bearer {DELVE_API_KEY}",
            },
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=30) as resp:
            return json.loads(resp.read())
    except urllib.error.HTTPError as e:
        try:
            err_body = json.loads(e.read().decode())
        except Exception:
            err_body = {"error": str(e)}
        return err_body
    except Exception as e:
        return {"error": str(e)}
