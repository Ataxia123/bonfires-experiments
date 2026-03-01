"""FastAPI router for the bonfire quest game."""

from __future__ import annotations

import json
import time
import urllib.parse
from dataclasses import asdict
from datetime import UTC, datetime
from typing import Callable

from fastapi import APIRouter, Body, Depends, Header, Query, Request
from fastapi.responses import JSONResponse

import game_config as config
import gm_engine
import http_client
import stack_processing
from game_store import GameStore
from timers import GmBatchTimerRunner, StackTimerRunner

router = APIRouter()

# ---------------------------------------------------------------------------
# Dependency providers
# ---------------------------------------------------------------------------


def get_store(request: Request) -> GameStore:
    return request.app.state.store  # type: ignore[no-any-return]


def get_resolve_owner_wallet(request: Request) -> Callable[[int], str]:
    return request.app.state.resolve_owner_wallet  # type: ignore[no-any-return]


def get_stack_timer(request: Request) -> StackTimerRunner | None:
    return getattr(request.app.state, "stack_timer", None)


def get_gm_timer(request: Request) -> GmBatchTimerRunner | None:
    return getattr(request.app.state, "gm_timer", None)


def _get_agent_api_key(x_agent_api_key: str = Header(default="")) -> tuple[str, str]:
    """Return (api_key, source) from header or server config."""
    header_key = x_agent_api_key.strip()
    if header_key:
        return header_key, "header"
    if config.DELVE_API_KEY:
        return config.DELVE_API_KEY, "server"
    return "", "missing"


# ---------------------------------------------------------------------------
# Validation helpers
# ---------------------------------------------------------------------------


def _required_string(data: dict[str, object], key: str) -> str:
    value = data.get(key)
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{key} is required")
    return value.strip()


def _required_int(data: dict[str, object], key: str) -> int:
    value = data.get(key)
    if not isinstance(value, int):
        raise ValueError(f"{key} must be an integer")
    return value


def _resolve_graph_mode(data: dict[str, object], default: str = "regenerate") -> str:
    graph_mode = str(data.get("graph_mode", default)).strip().lower()
    valid_modes = {"adaptive", "static", "regenerate", "append"}
    if graph_mode not in valid_modes:
        raise ValueError(f"graph_mode must be one of: {sorted(valid_modes)}")
    return graph_mode


def _assert_owner(store: GameStore, bonfire_id: str, wallet: str) -> None:
    owner = store.get_owner_wallet(bonfire_id)
    if not owner:
        raise PermissionError("bonfire is not linked")
    if owner.lower() != wallet.lower():
        raise PermissionError("wallet is not the bonfire NFT owner")


# ---------------------------------------------------------------------------
# Static helpers
# ---------------------------------------------------------------------------


def _derive_keyword_from_text(text: str) -> str:
    words = [w.strip(".,!?;:()[]{}\"'").lower() for w in text.split()]
    filtered = [w for w in words if len(w) >= 4 and w.isalpha()]
    if not filtered:
        return "quest"
    return filtered[0]


def _safe_json_object(text: str) -> dict[str, object] | None:
    if not text:
        return None
    candidate = text.strip()
    try:
        obj = json.loads(candidate)
        if isinstance(obj, dict):
            return obj
    except json.JSONDecodeError:
        pass
    start = candidate.find("{")
    end = candidate.rfind("}")
    if start >= 0 and end > start:
        try:
            obj = json.loads(candidate[start : end + 1])
            if isinstance(obj, dict):
                return obj
        except json.JSONDecodeError:
            return None
    return None


def _extract_id_like(value: object) -> str:
    if isinstance(value, str) and value.strip():
        return value.strip()
    if isinstance(value, dict):
        oid_obj = value.get("$oid")
        if isinstance(oid_obj, str) and oid_obj.strip():
            return oid_obj.strip()
        for key in ("episode_id", "episodeId", "id", "_id", "oid"):
            nested = value.get(key)
            nested_id = _extract_id_like(nested)
            if nested_id:
                return nested_id
    return ""


def _extract_episode_id_from_payload(payload: dict[str, object]) -> str:
    candidates: list[object] = [
        payload.get("episode_id"),
        payload.get("latest_episode_id"),
        payload.get("new_episode_id"),
        payload.get("episodeId"),
        payload.get("id"),
        payload.get("_id"),
    ]
    for container_key in ("episode", "data", "result", "latest_episode"):
        container_obj = payload.get(container_key)
        if isinstance(container_obj, dict):
            candidates.extend(
                [
                    container_obj.get("episode_id"),
                    container_obj.get("episodeId"),
                    container_obj.get("id"),
                    container_obj.get("_id"),
                ]
            )
    for value in candidates:
        extracted = _extract_id_like(value)
        if extracted:
            return extracted
    return ""


def _extract_episode_summary(episode: dict[str, object]) -> str:
    for key in ("summary", "message", "content", "text", "body", "title"):
        value = episode.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return json.dumps(episode)


def _normalize_graph_nodes(raw_nodes: object) -> list[dict[str, object]]:
    if not isinstance(raw_nodes, list):
        return []
    normalized: list[dict[str, object]] = []
    for n in raw_nodes:
        if not isinstance(n, dict):
            continue
        normalized.append(
            {
                "uuid": n.get("uuid") or n.get("id") or "",
                "name": n.get("name") or n.get("label") or "?",
                "labels": n.get("labels") or [],
                "summary": n.get("summary") or n.get("description") or "",
                "group_id": n.get("group_id") or "",
            }
        )
    return normalized


def _normalize_graph_edges(raw_edges: object) -> list[dict[str, object]]:
    if not isinstance(raw_edges, list):
        return []
    normalized: list[dict[str, object]] = []
    for e in raw_edges:
        if not isinstance(e, dict):
            continue
        source = e.get("source_node_uuid") or e.get("source") or e.get("from") or ""
        target = e.get("target_node_uuid") or e.get("target") or e.get("to") or ""
        normalized.append(
            {
                "uuid": e.get("uuid") or e.get("id") or "",
                "source": source,
                "target": target,
                "name": e.get("name") or e.get("fact") or e.get("label") or "",
                "fact": e.get("fact") or "",
            }
        )
    return normalized


# ---------------------------------------------------------------------------
# Business-logic helpers
# ---------------------------------------------------------------------------


def _fetch_room_graph_context(bonfire_id: str, entity_uuid: str) -> str:
    url = f"{config.DELVE_BASE_URL}/knowledge_graph/expand/entity"
    body: dict[str, object] = {"entity_uuid": entity_uuid, "bonfire_id": bonfire_id, "limit": 30}
    status, payload = http_client._json_request("POST", url, body)
    if status != 200 or not isinstance(payload, dict):
        return ""
    nodes = payload.get("nodes") or payload.get("entities") or []
    if not isinstance(nodes, list):
        return ""
    summaries: list[str] = []
    for n in nodes[:15]:
        if not isinstance(n, dict):
            continue
        name = str(n.get("name", ""))
        summary = str(n.get("summary", ""))
        if name:
            summaries.append(f"- {name}: {summary[:150]}" if summary else f"- {name}")
    return "\n".join(summaries) if summaries else ""


def _try_pin_room_graph_entity(store: GameStore, bonfire_id: str, room_id: str) -> None:
    room = store.get_room_by_id(bonfire_id, room_id)
    if not room:
        return
    if room.get("graph_entity_uuid"):
        return
    room_name = str(room.get("name", ""))
    if not room_name:
        return
    url = f"{config.DELVE_BASE_URL}/delve"
    body: dict[str, object] = {"query": f"Room: {room_name}", "bonfire_id": bonfire_id, "limit": 5}
    status, payload = http_client._json_request("POST", url, body)
    if status != 200 or not isinstance(payload, dict):
        return
    entities = payload.get("entities") or payload.get("nodes") or []
    if not isinstance(entities, list):
        return
    for ent in entities:
        if not isinstance(ent, dict):
            continue
        ent_name = str(ent.get("name", "")).lower()
        ent_uuid = str(ent.get("uuid", "")).strip()
        if ent_uuid and room_name.lower() in ent_name:
            store.set_room_graph_entity(bonfire_id, room_id, ent_uuid)
            return


def _build_agent_chat_context(store: GameStore, agent_id: str) -> dict[str, object]:
    player = store.get_player(agent_id)
    if not player:
        return {}
    state = store.get_state(player.bonfire_id)
    events = store.get_events(player.bonfire_id, 12)
    game = store.get_game(player.bonfire_id)

    _players_raw = state.get("players")
    players: list[object] = _players_raw if isinstance(_players_raw, list) else []
    _quests_raw = state.get("quests")
    quests: list[object] = _quests_raw if isinstance(_quests_raw, list) else []
    _contexts_raw = state.get("agent_context")
    contexts: list[object] = _contexts_raw if isinstance(_contexts_raw, list) else []

    self_player: dict[str, object] | None = None
    for item in players:
        if isinstance(item, dict) and str(item.get("agent_id", "")) == agent_id:
            self_player = item
            break

    self_context: dict[str, object] | None = None
    for item in contexts:
        if isinstance(item, dict) and str(item.get("agent_id", "")) == agent_id:
            self_context = item
            break

    visible_agents: list[dict[str, object]] = [
        {
            "agent_id": str(item.get("agent_id", "")),
            "remaining_episodes": item.get("remaining_episodes"),
            "is_active": item.get("is_active"),
        }
        for item in players
        if isinstance(item, dict)
    ]

    active_quests: list[dict[str, object]] = [
        {
            "quest_id": str(item.get("quest_id", "")),
            "prompt": str(item.get("prompt", "")),
            "keyword": str(item.get("keyword", "")),
            "reward": item.get("reward"),
        }
        for item in quests
        if isinstance(item, dict) and str(item.get("status", "")) == "active"
    ]

    event_summaries: list[str] = []
    for event in events[-6:]:
        if not isinstance(event, dict):
            continue
        event_type = str(event.get("event_type", "event"))
        payload_obj = event.get("payload")
        payload = payload_obj if isinstance(payload_obj, dict) else {}
        if event_type == "quest_claimed":
            event_summaries.append(
                f"quest_claimed:{payload.get('agent_id')}:{payload.get('quest_id')}:{payload.get('verdict')}"
            )
        elif event_type == "agent_recharged":
            event_summaries.append(f"agent_recharged:{payload.get('agent_id')}:+{payload.get('amount')}")
        elif event_type == "game_master_context_updated":
            event_summaries.append(
                f"episode_created:{payload.get('agent_id')}:{payload.get('episode_id')}"
            )
        else:
            event_summaries.append(event_type)

    return {
        "game": {
            "bonfire_id": player.bonfire_id,
            "game_prompt": game.game_prompt if game else "",
            "initial_episode_summary": game.initial_episode_summary if game else "",
            "world_state_summary": game.world_state_summary if game else "",
            "last_gm_reaction": game.last_gm_reaction if game else "",
            "last_episode_id": game.last_episode_id if game else "",
        },
        "agent": self_player or {"agent_id": agent_id},
        "agent_game_context": self_context or {},
        "visible_agents": visible_agents,
        "active_quests": active_quests,
        "recent_events": event_summaries,
    }


def _build_game_context_preamble(store: GameStore, agent_id: str) -> str:
    ctx = _build_agent_chat_context(store, agent_id)
    if not ctx:
        return ""
    parts: list[str] = [
        "[NARRATOR ROLE]\n"
        "You are the inner voice of the player's character — a narrator who speaks as their "
        "internal monologue. Describe what they see, feel, and sense in the world around them. "
        'Guide them through the adventure with vivid, second-person narration ("You notice...", '
        '"A chill runs down your spine..."). React to the room, other players present, and the '
        "world state. When the player asks questions or states actions, narrate the outcome as "
        "an unfolding story. Keep responses concise (2-4 sentences) and atmospheric. Never break "
        "character. Never reference game mechanics directly."
    ]
    game_obj = ctx.get("game")
    game = game_obj if isinstance(game_obj, dict) else {}
    game_prompt = str(game.get("game_prompt", "")).strip()
    if game_prompt:
        parts.append(f"[GAME WORLD]\n{game_prompt}")
    initial_ep = str(game.get("initial_episode_summary", "")).strip()
    if initial_ep:
        parts.append(f"[INITIAL EPISODE]\n{initial_ep}")
    world_state = str(game.get("world_state_summary", "")).strip()
    if world_state:
        parts.append(f"[CURRENT WORLD STATE]\n{world_state}")
    gm_reaction = str(game.get("last_gm_reaction", "")).strip()
    if gm_reaction:
        parts.append(f"[LAST GM REACTION]\n{gm_reaction}")

    player = store.get_player(agent_id)
    if player and player.current_room:
        room = store.get_room_by_id(player.bonfire_id, player.current_room)
        if room:
            room_name = str(room.get("name", "Unknown"))
            room_desc = str(room.get("description", ""))
            conns = room.get("connections", [])
            exits = ", ".join(str(c) for c in conns) if isinstance(conns, list) and conns else "none"
            parts.append(
                f"[CURRENT ROOM]\nName: {room_name}\nDescription: {room_desc}\nExits: {exits}"
            )
        room_msgs = store.get_room_messages(player.current_room, limit=20)
        if room_msgs:
            lines: list[str] = []
            for msg in room_msgs:
                if not isinstance(msg, dict):
                    continue
                role = str(msg.get("role", ""))
                sender = str(msg.get("sender_agent_id", ""))
                text = str(msg.get("text", ""))
                if sender == agent_id:
                    continue
                label = f"[{role}:{sender[:8]}]" if sender else f"[{role}]"
                lines.append(f"{label} {text[:200]}")
            if lines:
                parts.append("[ROOM ACTIVITY]\n" + "\n".join(lines[-10:]))

        room_graph_uuid = str(room.get("graph_entity_uuid", "")) if room else ""
        if room_graph_uuid and player:
            graph_context = _fetch_room_graph_context(player.bonfire_id, room_graph_uuid)
            if graph_context:
                parts.append(f"[ROOM KNOWLEDGE]\n{graph_context}")

    if player:
        bonfire_id = player.bonfire_id
        current_room = player.current_room
        if current_room:
            room_npcs = store.get_npcs_in_room(bonfire_id, current_room)
            if room_npcs:
                npc_lines = []
                for npc in room_npcs:
                    line = f"- {npc.name}: {npc.description}" if npc.description else f"- {npc.name}"
                    if npc.personality:
                        line += f" ({npc.personality})"
                    npc_lines.append(line)
                parts.append("[ROOM NPCS]\n" + "\n".join(npc_lines))

            room_items = store.get_objects_in_room(bonfire_id, current_room)
            if room_items:
                item_lines = [f"- {o.name} [{o.obj_type}]: {o.description}" for o in room_items]
                parts.append("[ROOM ITEMS]\n" + "\n".join(item_lines))

        inv_items = store.get_player_inventory(bonfire_id, agent_id)
        if inv_items:
            inv_lines = [
                f"- {it['name']} [{it.get('obj_type', 'artifact')}]: {it['description']}"
                for it in inv_items
            ]
            parts.append("[YOUR INVENTORY]\n" + "\n".join(inv_lines))

    _quests_raw2 = ctx.get("active_quests")
    quests2: list[object] = _quests_raw2 if isinstance(_quests_raw2, list) else []
    if quests2:
        quest_lines = [
            f"- {q.get('keyword', '?')}: {q.get('prompt', '')}"
            for q in quests2
            if isinstance(q, dict)
        ]
        if quest_lines:
            parts.append("[ACTIVE QUESTS]\n" + "\n".join(quest_lines))
    _events_raw = ctx.get("recent_events")
    events2: list[object] = _events_raw if isinstance(_events_raw, list) else []
    if events2:
        parts.append("[RECENT EVENTS]\n" + "\n".join(str(e) for e in events2[-6:]))
    _agent_ctx_raw = ctx.get("agent_game_context")
    agent_ctx: dict[str, object] = _agent_ctx_raw if isinstance(_agent_ctx_raw, dict) else {}
    last_summary = str(agent_ctx.get("last_episode_summary", "")).strip()
    if last_summary:
        parts.append(f"[YOUR LAST EPISODE]\n{last_summary}")
    return "\n\n".join(parts) + "\n\n---\n\n"


def _fetch_bonfire_episodes(bonfire_id: str, limit: int) -> list[dict[str, object]]:
    url = f"{config.DELVE_BASE_URL}/bonfires/{bonfire_id}/episodes?limit={limit}"
    status, payload = http_client._json_request("GET", url)
    if status != 200:
        return []
    episodes = payload.get("episodes")
    if not isinstance(episodes, list):
        return []
    return [item for item in episodes if isinstance(item, dict)]


def _fetch_bonfire_pricing(bonfire_id: str) -> tuple[int, dict[str, object]]:
    url = f"{config.DELVE_BASE_URL}/bonfires/{bonfire_id}/pricing"
    return http_client._json_request("GET", url)


def _fetch_provision_records_for_wallet(wallet_address: str) -> list[dict[str, object]]:
    url = f"{config.DELVE_BASE_URL}/provision?wallet_address={urllib.parse.quote(wallet_address)}"
    status, payload = http_client._json_request("GET", url)
    if status != 200:
        return []
    records_obj = payload.get("records")
    if isinstance(records_obj, list):
        return [r for r in records_obj if isinstance(r, dict)]
    return []


def _fetch_owned_bonfires_for_wallet(
    resolve_owner_wallet: Callable[[int], str], wallet_address: str
) -> list[dict[str, object]]:
    records = _fetch_provision_records_for_wallet(wallet_address)
    owned: list[dict[str, object]] = []
    for rec in records:
        if not isinstance(rec, dict):
            continue
        bonfire_id = rec.get("bonfire_id")
        token_id_obj = rec.get("erc8004_bonfire_id")
        if not isinstance(bonfire_id, str) or not bonfire_id:
            continue
        if not isinstance(token_id_obj, int):
            continue
        try:
            owner = resolve_owner_wallet(token_id_obj).lower()
        except Exception:
            continue
        if owner != wallet_address.lower():
            continue
        owned.append(
            {
                "bonfire_id": bonfire_id,
                "erc8004_bonfire_id": token_id_obj,
                "agent_id": rec.get("agent_id"),
                "agent_name": rec.get("agent_name"),
                "owner_wallet": owner,
            }
        )
    return owned


def _fetch_agent_configs_for_bonfire(bonfire_id: str) -> list[dict[str, object]]:
    status, payload = http_client._json_request(
        "GET", f"{config.DELVE_BASE_URL}/agents?bonfire_id={urllib.parse.quote(bonfire_id)}"
    )
    if status != 200 or not isinstance(payload, dict):
        return []
    agents_obj = payload.get("agents")
    if not isinstance(agents_obj, list):
        return []
    return [obj for obj in agents_obj if isinstance(obj, dict)]


def _extract_purchase_tx_hash_from_agent_payload(payload: dict[str, object]) -> str | None:
    direct_candidates = (
        payload.get("purchase_tx_hash"),
        payload.get("purchaseTxHash"),
        payload.get("tx_hash"),
        payload.get("txHash"),
    )
    for value in direct_candidates:
        if isinstance(value, str) and value:
            return value
    deployment_obj = payload.get("deploymentConfiguration")
    if isinstance(deployment_obj, dict):
        for key in ("purchase_tx_hash", "purchaseTxHash", "tx_hash", "txHash"):
            value = deployment_obj.get(key)
            if isinstance(value, str) and value:
                return value
    return None


def _fetch_wallet_purchased_agents(
    wallet_address: str, bonfire_id: str
) -> list[dict[str, object]]:
    purchased_by_agent_id: dict[str, dict[str, object]] = {}

    purchased_url = (
        f"{config.DELVE_BASE_URL}/purchased-agents?"
        f"wallet_address={urllib.parse.quote(wallet_address)}&bonfire_id={urllib.parse.quote(bonfire_id)}"
    )
    purchased_status, purchased_payload = http_client._json_request("GET", purchased_url)
    if purchased_status == 200 and isinstance(purchased_payload, dict):
        records_obj = purchased_payload.get("records")
        if isinstance(records_obj, list):
            for rec in records_obj:
                if not isinstance(rec, dict):
                    continue
                rec_agent_obj = rec.get("agent_id")
                if not isinstance(rec_agent_obj, str) or not rec_agent_obj:
                    continue
                rec_bonfire_obj = rec.get("bonfire_id")
                if not isinstance(rec_bonfire_obj, str) or rec_bonfire_obj != bonfire_id:
                    continue
                purchased_item: dict[str, object] = {
                    "wallet_address": wallet_address,
                    "bonfire_id": rec_bonfire_obj,
                    "agent_id": rec_agent_obj,
                    "agent_name": rec.get("agent_name"),
                    "source": "purchased_agents",
                }
                purchase_id_obj = rec.get("purchase_id")
                if isinstance(purchase_id_obj, str) and purchase_id_obj:
                    purchased_item["purchase_id"] = purchase_id_obj
                tx_hash_obj = (
                    rec.get("purchase_tx_hash")
                    or rec.get("purchaseTxHash")
                    or rec.get("tx_hash")
                    or rec.get("txHash")
                )
                if isinstance(tx_hash_obj, str) and tx_hash_obj:
                    purchased_item["purchase_tx_hash"] = tx_hash_obj
                purchased_by_agent_id[rec_agent_obj] = purchased_item

    records = _fetch_provision_records_for_wallet(wallet_address)
    for rec in records:
        if not isinstance(rec, dict):
            continue
        rec_bonfire_id = rec.get("bonfire_id")
        if not isinstance(rec_bonfire_id, str) or rec_bonfire_id != bonfire_id:
            continue
        agent_id_obj = rec.get("agent_id")
        if not isinstance(agent_id_obj, str) or not agent_id_obj:
            continue
        provision_item: dict[str, object] = {
            "wallet_address": wallet_address,
            "bonfire_id": rec_bonfire_id,
            "agent_id": agent_id_obj,
            "agent_name": rec.get("agent_name"),
            "erc8004_bonfire_id": rec.get("erc8004_bonfire_id"),
            "source": "provision_records",
        }
        purchase_id_obj = rec.get("purchase_id")
        if isinstance(purchase_id_obj, str) and purchase_id_obj:
            provision_item["purchase_id"] = purchase_id_obj
        purchase_tx_hash_obj = rec.get("purchase_tx_hash")
        if isinstance(purchase_tx_hash_obj, str) and purchase_tx_hash_obj:
            provision_item["purchase_tx_hash"] = purchase_tx_hash_obj
        purchased_by_agent_id[agent_id_obj] = provision_item

    bonfire_agents_url = f"{config.DELVE_BASE_URL}/bonfires/{bonfire_id}/agents"
    status, payload = http_client._json_request("GET", bonfire_agents_url)
    if status == 200:
        agents_obj = payload.get("agents")
        if isinstance(agents_obj, list):
            for item_obj in agents_obj:
                if not isinstance(item_obj, dict):
                    continue
                agent_id_raw = item_obj.get("id") or item_obj.get("agent_id")
                if not isinstance(agent_id_raw, str) or not agent_id_raw:
                    continue
                if agent_id_raw in purchased_by_agent_id:
                    continue
                merged: dict[str, object] = {
                    "wallet_address": wallet_address,
                    "bonfire_id": bonfire_id,
                    "agent_id": agent_id_raw,
                    "agent_name": item_obj.get("name") or item_obj.get("username"),
                    "source": "bonfire_agents",
                }
                purchase_id_obj = item_obj.get("purchase_id") or item_obj.get("purchaseId")
                if isinstance(purchase_id_obj, str) and purchase_id_obj:
                    merged["purchase_id"] = purchase_id_obj
                tx_hash_obj = (
                    item_obj.get("purchase_tx_hash")
                    or item_obj.get("purchaseTxHash")
                    or item_obj.get("tx_hash")
                    or item_obj.get("txHash")
                )
                if isinstance(tx_hash_obj, str) and tx_hash_obj:
                    merged["purchase_tx_hash"] = tx_hash_obj
                purchased_by_agent_id[agent_id_raw] = merged

    purchased = list(purchased_by_agent_id.values())
    purchased.sort(key=lambda x: str(x.get("agent_id", "")))
    return purchased


def _resolve_purchase_id_for_selected_agent(
    store: GameStore, wallet_address: str, bonfire_id: str, agent_id: str
) -> str | None:
    wallet_lower = wallet_address.lower()
    player = store.get_player(agent_id)
    if player and player.wallet == wallet_lower and player.bonfire_id == bonfire_id and player.purchase_id:
        return player.purchase_id

    purchased_agents = _fetch_wallet_purchased_agents(wallet_lower, bonfire_id)
    for item in purchased_agents:
        if not isinstance(item, dict):
            continue
        if item.get("agent_id") != agent_id:
            continue
        purchase_id_obj = item.get("purchase_id")
        if isinstance(purchase_id_obj, str) and purchase_id_obj:
            return purchase_id_obj

    status, payload = http_client._json_request("GET", f"{config.DELVE_BASE_URL}/agents/{agent_id}")
    if status == 200 and isinstance(payload, dict):
        for key in ("purchase_id", "purchaseId"):
            candidate_obj = payload.get(key)
            if isinstance(candidate_obj, str) and candidate_obj:
                return candidate_obj

    probe_url = f"{config.DELVE_BASE_URL}/purchased-agents/{agent_id}/reveal_nonce"
    probe_status, _probe_payload = http_client._json_request("GET", probe_url)
    if probe_status == 200:
        return agent_id
    return None


def _resolve_purchase_tx_hash_for_selected_agent(
    store: GameStore, wallet_address: str, bonfire_id: str, agent_id: str
) -> str | None:
    wallet_lower = wallet_address.lower()
    player = store.get_player(agent_id)
    if (
        player
        and player.wallet == wallet_lower
        and player.bonfire_id == bonfire_id
        and player.purchase_tx_hash
    ):
        return player.purchase_tx_hash

    purchased_agents = _fetch_wallet_purchased_agents(wallet_lower, bonfire_id)
    for item in purchased_agents:
        if not isinstance(item, dict) or item.get("agent_id") != agent_id:
            continue
        tx_obj = (
            item.get("purchase_tx_hash")
            or item.get("purchaseTxHash")
            or item.get("tx_hash")
            or item.get("txHash")
        )
        if isinstance(tx_obj, str) and tx_obj:
            return tx_obj

    for agent_payload in _fetch_agent_configs_for_bonfire(bonfire_id):
        payload_agent_id_obj = (
            agent_payload.get("id") or agent_payload.get("_id") or agent_payload.get("agent_id")
        )
        if not isinstance(payload_agent_id_obj, str) or payload_agent_id_obj != agent_id:
            continue
        tx_obj = _extract_purchase_tx_hash_from_agent_payload(agent_payload)
        if isinstance(tx_obj, str) and tx_obj:
            return tx_obj

    status, payload = http_client._json_request("GET", f"{config.DELVE_BASE_URL}/agents/{agent_id}")
    if status == 200 and isinstance(payload, dict):
        tx_obj = _extract_purchase_tx_hash_from_agent_payload(payload)
        if isinstance(tx_obj, str) and tx_obj:
            return tx_obj
    return None


def _seed_game_from_prompt(
    store: GameStore,
    bonfire_id: str,
    owner_wallet: str,
    game_prompt: str,
    gm_agent_id: str | None,
    quest_count: int,
) -> dict[str, object]:
    episode_summary = f"The game begins: {game_prompt.strip()}"
    seeded_quests: list[dict[str, object]] = []

    if gm_agent_id and config.DELVE_API_KEY:
        gm_url = f"{config.DELVE_BASE_URL}/agents/{gm_agent_id}/chat"
        gm_status, gm_payload = http_client._agent_json_request(
            "POST",
            gm_url,
            config.DELVE_API_KEY,
            body={
                "message": (
                    "You are a game master. Return strict JSON with keys "
                    '{"episode_summary": string, "quests": [{"prompt": string, "keyword": string, "reward": int}]}. '
                    f"Create {quest_count} quests based on this game prompt: {game_prompt}"
                ),
                "chat_history": [],
                "graph_mode": "adaptive",
                "context": {},
            },
        )
        if gm_status == 200:
            reply = gm_payload.get("reply")
            if isinstance(reply, str):
                parsed = _safe_json_object(reply)
                if parsed:
                    parsed_episode = parsed.get("episode_summary")
                    if isinstance(parsed_episode, str) and parsed_episode.strip():
                        episode_summary = parsed_episode.strip()
                    parsed_quests = parsed.get("quests")
                    if isinstance(parsed_quests, list):
                        for q in parsed_quests:
                            if not isinstance(q, dict):
                                continue
                            seeded_quests.append(
                                {
                                    "prompt": str(q.get("prompt", "")).strip(),
                                    "keyword": str(q.get("keyword", "")).strip().lower(),
                                    "reward": int(q.get("reward", 1)),
                                }
                            )

    if not seeded_quests:
        words = [w.strip(".,!?;:()[]{}\"'").lower() for w in game_prompt.split()]
        keywords = [w for w in words if len(w) >= 4 and w.isalpha()]
        if not keywords:
            keywords = ["quest", "signal", "artifact"]
        for i in range(max(1, quest_count)):
            keyword = keywords[i % len(keywords)]
            seeded_quests.append(
                {
                    "prompt": f"Quest {i + 1}: Produce a meaningful update about '{keyword}' tied to the game prompt.",
                    "keyword": keyword,
                    "reward": 1 + (i % 2),
                }
            )

    store._append_event(
        bonfire_id,
        "game_seed_episode",
        {"episode_summary": episode_summary, "owner_wallet": owner_wallet.lower()},
    )

    created_quests: list[dict[str, object]] = []
    for i, seeded in enumerate(seeded_quests[: max(1, quest_count)]):
        prompt = str(seeded.get("prompt", "")).strip() or f"Quest seed {i + 1}"
        keyword = str(seeded.get("keyword", "")).strip().lower() or _derive_keyword_from_text(prompt)
        reward_val = seeded.get("reward", 1)
        reward = reward_val if isinstance(reward_val, int) and reward_val >= 1 else 1
        quest = store.create_quest(
            bonfire_id=bonfire_id,
            creator_wallet=owner_wallet.lower(),
            quest_type="game_seed",
            prompt=prompt,
            keyword=keyword,
            reward=reward,
            cooldown_seconds=config.DEFAULT_CLAIM_COOLDOWN_SECONDS,
            expires_in_seconds=None,
        )
        created_quests.append(
            {
                "quest_id": quest.quest_id,
                "prompt": quest.prompt,
                "keyword": quest.keyword,
                "reward": quest.reward,
            }
        )

    return {"episode_summary": episode_summary, "quests": created_quests}


def _get_agent_episode_uuids(agent_id: str) -> list[str]:
    status, payload = http_client._json_request("GET", f"{config.DELVE_BASE_URL}/agents/{agent_id}")
    if status != 200 or not isinstance(payload, dict):
        print(f"  [poll] GET /agents/{agent_id} returned {status}")
        return []
    uuids = payload.get("episode_uuids") or payload.get("episodeUuids") or []
    return [str(u) for u in uuids] if isinstance(uuids, list) else []


def _poll_for_new_episode(
    agent_id: str, pre_uuids: list[str], max_wait: float = 45.0, interval: float = 3.0
) -> str:
    pre_set = set(pre_uuids)
    elapsed = 0.0
    print(
        f"  [poll] Waiting for new episode on agent {agent_id} (pre={len(pre_uuids)} uuids, max_wait={max_wait}s)"
    )
    while elapsed < max_wait:
        time.sleep(interval)
        elapsed += interval
        current = _get_agent_episode_uuids(agent_id)
        new_uuids = [u for u in current if u not in pre_set]
        if new_uuids:
            print(f"  [poll] Found new episode after {elapsed:.0f}s: {new_uuids[-1]}")
            return new_uuids[-1]
        print(f"  [poll] {elapsed:.0f}s elapsed, {len(current)} total uuids, no new yet")
    print(f"  [poll] Timed out after {max_wait}s for agent {agent_id}")
    return stack_processing._resolve_latest_episode_from_agent(agent_id) or ""


def _trigger_gm_reaction_for_agent(
    store: GameStore, agent_id: str, episode_id: str | None = None
) -> tuple[int, dict[str, object]]:
    player = store.get_player(agent_id)
    if not player:
        return 404, {"error": "agent is not registered in game"}

    chosen_episode_id = episode_id or ""
    ctx = store.get_agent_context(agent_id)
    if not chosen_episode_id:
        episode_id_obj = ctx.get("last_episode_id")
        if isinstance(episode_id_obj, str) and episode_id_obj.strip():
            chosen_episode_id = episode_id_obj.strip()

    episode_summary = ""
    episode_payload: dict[str, object] | None = None
    if chosen_episode_id:
        episode_payload = stack_processing._fetch_episode_payload(player.bonfire_id, chosen_episode_id)
        if episode_payload is not None:
            episode_summary = _extract_episode_summary(episode_payload)

    if not episode_summary:
        summary_obj = ctx.get("last_episode_summary")
        if isinstance(summary_obj, str) and summary_obj.strip():
            episode_summary = summary_obj.strip()

    if not chosen_episode_id:
        chosen_episode_id = f"manual-{agent_id}-{int(time.time())}"
    if not episode_summary:
        return 400, {"error": "No episode context found. Process stack first."}

    gm_decision = gm_engine._make_gm_decision(
        store, agent_id, episode_summary, chosen_episode_id, episode_payload
    )
    reaction = str(gm_decision.get("reaction", "")).strip()
    world_update = str(gm_decision.get("world_state_update", "")).strip()
    updated_world = store.update_game_world_state(
        bonfire_id=player.bonfire_id,
        episode_id=chosen_episode_id,
        world_state_summary=world_update,
        gm_reaction=reaction,
    )
    gm_agent_ctx = store.update_agent_context_with_gm_response(
        agent_id=agent_id,
        episode_id=chosen_episode_id,
        gm_reaction=reaction,
        world_state_update=world_update,
    )
    extension_obj = gm_decision.get("extension_awarded", 0)
    extension = extension_obj if isinstance(extension_obj, int) else 0
    extension_payload: dict[str, object] | None = None
    if extension > 0:
        extension_payload = store.recharge_agent(
            bonfire_id=player.bonfire_id,
            agent_id=agent_id,
            amount=extension,
            reason="gm_episode_extension",
        )
    response: dict[str, object] = {
        "agent_id": agent_id,
        "episode_id": chosen_episode_id,
        "gm_decision": gm_decision,
        "world_state": updated_world,
        "agent_gm_context": gm_agent_ctx,
        "trigger": "manual",
    }
    if extension_payload is not None:
        response["episode_extension"] = {
            "extension_awarded": extension,
            "recharge": extension_payload,
        }
    return 200, response


# ---------------------------------------------------------------------------
# GET routes
# ---------------------------------------------------------------------------


@router.get("/healthz")
def route_healthz() -> JSONResponse:
    return JSONResponse({"status": "ok"})


@router.get("/game/state")
def route_game_state(
    bonfire_id: str = Query(...),
    store: GameStore = Depends(get_store),
) -> JSONResponse:
    return JSONResponse(store.get_state(bonfire_id))


@router.get("/game/feed")
def route_game_feed(
    bonfire_id: str = Query(...),
    limit: int = Query(default=20, ge=1, le=200),
    store: GameStore = Depends(get_store),
) -> JSONResponse:
    events = store.get_events(bonfire_id, limit)
    episodes = _fetch_bonfire_episodes(bonfire_id, limit)
    return JSONResponse({"bonfire_id": bonfire_id, "events": events, "episodes": episodes})


@router.get("/game/list-active")
def route_list_active(store: GameStore = Depends(get_store)) -> JSONResponse:
    return JSONResponse({"games": store.list_active_games()})


@router.get("/game/details")
def route_game_details(
    bonfire_id: str = Query(...),
    store: GameStore = Depends(get_store),
) -> JSONResponse:
    game = store.get_game(bonfire_id)
    if not game or game.status != "active":
        return JSONResponse(status_code=404, content={"error": "active game not found"})
    state = store.get_state(bonfire_id)
    events = store.get_events(bonfire_id, 50)
    return JSONResponse(
        {
            "game": {
                "game_id": game.game_id,
                "bonfire_id": game.bonfire_id,
                "owner_wallet": game.owner_wallet,
                "game_prompt": game.game_prompt,
                "gm_agent_id": game.gm_agent_id,
                "initial_episode_summary": game.initial_episode_summary,
                "world_state_summary": game.world_state_summary,
                "last_gm_reaction": game.last_gm_reaction,
                "last_episode_id": game.last_episode_id,
                "created_at": game.created_at,
                "updated_at": game.updated_at,
                "status": game.status,
            },
            "state": state,
            "events": events,
        }
    )


@router.get("/game/bonfire/pricing")
def route_bonfire_pricing(bonfire_id: str = Query(...)) -> JSONResponse:
    status, payload = _fetch_bonfire_pricing(bonfire_id)
    return JSONResponse(status_code=status, content=payload)


@router.get("/game/config")
def route_game_config() -> JSONResponse:
    return JSONResponse(
        {
            "erc8004_registry_address": config.ERC8004_REGISTRY_ADDRESS,
            "payment": {
                "network": config.PAYMENT_NETWORK,
                "source_network": config.PAYMENT_SOURCE_NETWORK,
                "destination_network": config.PAYMENT_DESTINATION_NETWORK,
                "token_address": config.PAYMENT_TOKEN_ADDRESS,
                "chain_id": config.PAYMENT_CHAIN_ID,
                "default_amount": config.PAYMENT_DEFAULT_AMOUNT,
                "intermediary_address": config.ONCHAINFI_INTERMEDIARY_ADDRESS,
            },
        }
    )


@router.get("/game/wallet/provision-records")
def route_provision_records(wallet_address: str = Query(...)) -> JSONResponse:
    wa = wallet_address.strip().lower()
    records = _fetch_provision_records_for_wallet(wa)
    return JSONResponse({"wallet_address": wa, "records": records})


@router.get("/game/wallet/bonfires")
def route_wallet_bonfires(
    wallet_address: str = Query(...),
    resolve_owner_wallet: Callable[[int], str] = Depends(get_resolve_owner_wallet),
) -> JSONResponse:
    wa = wallet_address.strip().lower()
    bonfires = _fetch_owned_bonfires_for_wallet(resolve_owner_wallet, wa)
    return JSONResponse({"wallet_address": wa, "bonfires": bonfires})


@router.get("/game/wallet/purchased-agents")
def route_wallet_purchased_agents(
    wallet_address: str = Query(...),
    bonfire_id: str = Query(...),
) -> JSONResponse:
    wa = wallet_address.strip().lower()
    agents = _fetch_wallet_purchased_agents(wa, bonfire_id)
    return JSONResponse({"wallet_address": wa, "bonfire_id": bonfire_id, "agents": agents})


@router.get("/game/stack/timer/status")
def route_timer_status(
    stack_timer: StackTimerRunner | None = Depends(get_stack_timer),
    gm_timer: GmBatchTimerRunner | None = Depends(get_gm_timer),
) -> JSONResponse:
    return JSONResponse(
        {
            "enabled": stack_timer is not None,
            "is_running": stack_timer.is_running if stack_timer else False,
            "interval_seconds": config.STACK_PROCESS_INTERVAL_SECONDS,
            "last_run_at": stack_timer.last_run_at if stack_timer else None,
            "last_result": stack_timer.last_result if stack_timer else None,
            "gm_timer": {
                "enabled": gm_timer is not None,
                "is_running": gm_timer.is_running if gm_timer else False,
                "interval_seconds": config.GM_BATCH_INTERVAL_SECONDS,
                "last_run_at": gm_timer.last_run_at if gm_timer else None,
                "last_result": gm_timer.last_result if gm_timer else None,
            },
        }
    )


@router.get("/game/room/chat")
def route_room_chat(
    room_id: str = Query(...),
    limit: int = Query(default=50, ge=1, le=200),
    store: GameStore = Depends(get_store),
) -> JSONResponse:
    messages = store.get_room_messages(room_id, limit=limit)
    return JSONResponse({"room_id": room_id, "messages": messages})


@router.get("/game/room/npcs")
def route_room_npcs(
    bonfire_id: str = Query(...),
    room_id: str = Query(...),
    store: GameStore = Depends(get_store),
) -> JSONResponse:
    npcs = store.get_npcs_in_room(bonfire_id, room_id)
    return JSONResponse({"npcs": [asdict(n) for n in npcs]})


@router.get("/game/inventory")
def route_inventory(
    agent_id: str = Query(...),
    bonfire_id: str = Query(default=""),
    store: GameStore = Depends(get_store),
) -> JSONResponse:
    effective_bonfire_id = bonfire_id
    if not effective_bonfire_id:
        player = store.get_player(agent_id)
        effective_bonfire_id = player.bonfire_id if player else ""
    if not effective_bonfire_id:
        return JSONResponse(status_code=404, content={"error": "player_not_found"})
    items = store.get_player_inventory(effective_bonfire_id, agent_id)
    return JSONResponse({"agent_id": agent_id, "items": items})


@router.get("/game/map")
def route_map(
    bonfire_id: str = Query(...),
    store: GameStore = Depends(get_store),
) -> JSONResponse:
    return JSONResponse(store.get_room_map(bonfire_id))


@router.get("/game/graph")
def route_graph(
    bonfire_id: str = Query(...),
    agent_id: str = Query(default=""),
    store: GameStore = Depends(get_store),
) -> JSONResponse:
    episode_uuids: list[str] = []
    if agent_id:
        episode_uuids = _get_agent_episode_uuids(agent_id)
    if not episode_uuids:
        for aid in store.get_all_agent_ids():
            p = store.get_player(aid)
            if p and p.bonfire_id == bonfire_id:
                episode_uuids = _get_agent_episode_uuids(aid)
                if episode_uuids:
                    break
    if not episode_uuids:
        return JSONResponse({"nodes": [], "edges": [], "episodes": []})

    uuids_batch = episode_uuids[-20:]
    url = f"{config.DELVE_BASE_URL}/knowledge_graph/episodes/expand"
    body: dict[str, object] = {"episode_uuids": uuids_batch, "bonfire_id": bonfire_id, "limit": 200}
    status, payload = http_client._json_request("POST", url, body)
    if status != 200:
        return JSONResponse(status_code=status, content=payload)

    nodes = _normalize_graph_nodes(payload.get("nodes") or payload.get("entities") or [])
    edges = _normalize_graph_edges(payload.get("edges") or [])
    episodes = payload.get("episodes") or []
    return JSONResponse({"nodes": nodes, "edges": edges, "episodes": episodes})


# ---------------------------------------------------------------------------
# POST routes
# ---------------------------------------------------------------------------


@router.post("/game/purchase-agent/{bonfire_id}")
def route_purchase_agent(
    bonfire_id: str, body: dict[str, object] = Body(default={})
) -> JSONResponse:
    url = f"{config.DELVE_BASE_URL}/bonfires/{bonfire_id}/purchase-agent"
    status, payload = http_client._json_request("POST", url, body)
    return JSONResponse(status_code=status, content=payload)


@router.post("/game/purchased-agents/reveal-nonce")
def route_reveal_nonce_proxy(body: dict[str, object] = Body(default={})) -> JSONResponse:
    purchase_id = _required_string(body, "purchase_id")
    url = f"{config.DELVE_BASE_URL}/purchased-agents/{purchase_id}/reveal_nonce"
    status, payload = http_client._json_request("GET", url)
    return JSONResponse(status_code=status, content=payload)


@router.post("/game/purchased-agents/reveal-api-key")
def route_reveal_api_key_proxy(body: dict[str, object] = Body(default={})) -> JSONResponse:
    purchase_id = _required_string(body, "purchase_id")
    nonce = _required_string(body, "nonce")
    signature = _required_string(body, "signature")
    url = f"{config.DELVE_BASE_URL}/purchased-agents/{purchase_id}/reveal_api_key"
    status, payload = http_client._json_request(
        "POST", url, {"nonce": nonce, "signature": signature}
    )
    return JSONResponse(status_code=status, content=payload)


@router.post("/game/agents/reveal-nonce-selected")
def route_reveal_nonce_selected(
    body: dict[str, object] = Body(default={}),
    store: GameStore = Depends(get_store),
) -> JSONResponse:
    wallet = _required_string(body, "wallet_address").lower()
    bonfire_id = _required_string(body, "bonfire_id")
    agent_id = _required_string(body, "agent_id")

    purchase_id = _resolve_purchase_id_for_selected_agent(store, wallet, bonfire_id, agent_id)
    if purchase_id:
        url = f"{config.DELVE_BASE_URL}/purchased-agents/{purchase_id}/reveal_nonce"
        status, payload = http_client._json_request("GET", url)
        if isinstance(payload, dict):
            response_payload = dict(payload)
            response_payload["purchase_id"] = purchase_id
            response_payload["resolution"] = "purchase_id"
            return JSONResponse(status_code=status, content=response_payload)
        return JSONResponse(
            status_code=status, content={"purchase_id": purchase_id, "upstream_payload": payload}
        )

    purchase_tx_hash = _resolve_purchase_tx_hash_for_selected_agent(
        store, wallet, bonfire_id, agent_id
    )
    if purchase_tx_hash:
        url = f"{config.DELVE_BASE_URL}/provision/reveal_nonce?tx_hash={urllib.parse.quote(purchase_tx_hash)}"
        status, payload = http_client._json_request("GET", url)
        if isinstance(payload, dict):
            response_payload = dict(payload)
            response_payload["purchase_tx_hash"] = purchase_tx_hash
            response_payload["resolution"] = "purchase_tx_hash"
            return JSONResponse(status_code=status, content=response_payload)
        return JSONResponse(
            status_code=status,
            content={"purchase_tx_hash": purchase_tx_hash, "upstream_payload": payload},
        )

    return JSONResponse(
        status_code=404,
        content={
            "error": "purchase_id_not_found_for_selected_agent",
            "detail": "Selected agent has no purchase_id or purchase_tx_hash in available purchase records.",
            "wallet_address": wallet,
            "bonfire_id": bonfire_id,
            "agent_id": agent_id,
        },
    )


@router.post("/game/agents/reveal-api-key-selected")
def route_reveal_api_key_selected(
    body: dict[str, object] = Body(default={}),
    store: GameStore = Depends(get_store),
) -> JSONResponse:
    wallet = _required_string(body, "wallet_address").lower()
    bonfire_id = _required_string(body, "bonfire_id")
    agent_id = _required_string(body, "agent_id")
    nonce = _required_string(body, "nonce")
    signature = _required_string(body, "signature")

    purchase_id_obj = body.get("purchase_id")
    purchase_id = str(purchase_id_obj).strip() if isinstance(purchase_id_obj, str) else ""
    if not purchase_id:
        purchase_id = _resolve_purchase_id_for_selected_agent(store, wallet, bonfire_id, agent_id)

    if purchase_id:
        url = f"{config.DELVE_BASE_URL}/purchased-agents/{purchase_id}/reveal_api_key"
        status, payload = http_client._json_request(
            "POST", url, {"nonce": nonce, "signature": signature}
        )
        if isinstance(payload, dict):
            response_payload = dict(payload)
            response_payload["purchase_id"] = purchase_id
            response_payload["resolution"] = "purchase_id"
            return JSONResponse(status_code=status, content=response_payload)
        return JSONResponse(
            status_code=status, content={"purchase_id": purchase_id, "upstream_payload": payload}
        )

    purchase_tx_hash_obj = body.get("purchase_tx_hash")
    purchase_tx_hash = str(purchase_tx_hash_obj).strip() if isinstance(purchase_tx_hash_obj, str) else ""
    if not purchase_tx_hash:
        purchase_tx_hash = _resolve_purchase_tx_hash_for_selected_agent(
            store, wallet, bonfire_id, agent_id
        )
    if purchase_tx_hash:
        url = f"{config.DELVE_BASE_URL}/provision/reveal_api_key"
        status, payload = http_client._json_request(
            "POST", url, {"tx_hash": purchase_tx_hash, "nonce": nonce, "signature": signature}
        )
        if isinstance(payload, dict):
            response_payload = dict(payload)
            response_payload["purchase_tx_hash"] = purchase_tx_hash
            response_payload["resolution"] = "purchase_tx_hash"
            return JSONResponse(status_code=status, content=response_payload)
        return JSONResponse(
            status_code=status,
            content={"purchase_tx_hash": purchase_tx_hash, "upstream_payload": payload},
        )

    return JSONResponse(
        status_code=404,
        content={
            "error": "purchase_id_not_found_for_selected_agent",
            "detail": "Selected agent has no purchase_id or purchase_tx_hash in available purchase records.",
            "wallet_address": wallet,
            "bonfire_id": bonfire_id,
            "agent_id": agent_id,
        },
    )


@router.post("/game/bonfire/link")
def route_bonfire_link(
    body: dict[str, object] = Body(default={}),
    store: GameStore = Depends(get_store),
    resolve_owner_wallet: Callable[[int], str] = Depends(get_resolve_owner_wallet),
) -> JSONResponse:
    bonfire_id = _required_string(body, "bonfire_id")
    erc8004_bonfire_id = _required_int(body, "erc8004_bonfire_id")
    wallet_address = _required_string(body, "wallet_address").lower()
    owner_wallet = resolve_owner_wallet(erc8004_bonfire_id).lower()
    if owner_wallet != wallet_address:
        return JSONResponse(
            status_code=403,
            content={"error": "wallet does not own bonfire NFT", "owner_wallet": owner_wallet},
        )
    linked = store.link_bonfire(
        bonfire_id=bonfire_id,
        erc8004_bonfire_id=erc8004_bonfire_id,
        owner_wallet=owner_wallet,
    )
    return JSONResponse(dict(linked))


@router.post("/game/agents/register-purchase")
def route_register_purchase(
    body: dict[str, object] = Body(default={}),
    store: GameStore = Depends(get_store),
) -> JSONResponse:
    wallet = _required_string(body, "wallet_address").lower()
    agent_id = _required_string(body, "agent_id")
    bonfire_id = _required_string(body, "bonfire_id")
    purchase_id = _required_string(body, "purchase_id")
    purchase_tx_hash = _required_string(body, "purchase_tx_hash")
    erc8004_bonfire_id = _required_int(body, "erc8004_bonfire_id")
    episodes_purchased = _required_int(body, "episodes_purchased")

    reveal_nonce_url = f"{config.DELVE_BASE_URL}/purchased-agents/{purchase_id}/reveal_nonce"
    status, payload = http_client._json_request("GET", reveal_nonce_url)
    if status != 200:
        return JSONResponse(
            status_code=400,
            content={
                "error": "invalid_purchase_id",
                "detail": "purchase_id could not be validated against purchased-agent endpoints",
                "upstream_status": status,
                "upstream_payload": payload,
            },
        )

    owner_wallet_existing = store.get_owner_wallet(bonfire_id)
    owner_wallet = owner_wallet_existing.lower() if owner_wallet_existing else wallet
    if not owner_wallet_existing:
        store.link_bonfire(
            bonfire_id=bonfire_id,
            erc8004_bonfire_id=erc8004_bonfire_id,
            owner_wallet=owner_wallet,
        )

    player = store.register_purchase(
        wallet=wallet,
        agent_id=agent_id,
        bonfire_id=bonfire_id,
        erc8004_bonfire_id=erc8004_bonfire_id,
        purchase_id=purchase_id,
        purchase_tx_hash=purchase_tx_hash,
        episodes_purchased=episodes_purchased,
    )
    store.place_player_in_starting_room(agent_id)
    return JSONResponse(
        {
            "agent_id": player.agent_id,
            "purchase_id": player.purchase_id,
            "owner_wallet": owner_wallet,
            "remaining_episodes": player.remaining_episodes,
            "total_quota": player.total_quota,
        }
    )


@router.post("/game/agents/register-selected")
def route_register_selected(
    body: dict[str, object] = Body(default={}),
    store: GameStore = Depends(get_store),
) -> JSONResponse:
    wallet = _required_string(body, "wallet_address").lower()
    agent_id = _required_string(body, "agent_id")
    bonfire_id = _required_string(body, "bonfire_id")
    erc8004_bonfire_id = _required_int(body, "erc8004_bonfire_id")
    episodes_obj = body.get("episodes_purchased", 2)
    if not isinstance(episodes_obj, int):
        raise ValueError("episodes_purchased must be an integer")
    episodes_purchased = max(1, episodes_obj)

    owner_wallet_existing = store.get_owner_wallet(bonfire_id)
    owner_wallet = owner_wallet_existing.lower() if owner_wallet_existing else wallet
    if not owner_wallet_existing:
        store.link_bonfire(
            bonfire_id=bonfire_id,
            erc8004_bonfire_id=erc8004_bonfire_id,
            owner_wallet=owner_wallet,
        )

    player = store.register_agent(
        wallet=wallet,
        agent_id=agent_id,
        bonfire_id=bonfire_id,
        erc8004_bonfire_id=erc8004_bonfire_id,
        episodes_purchased=episodes_purchased,
    )
    store.place_player_in_starting_room(agent_id)
    return JSONResponse(
        {
            "agent_id": player.agent_id,
            "owner_wallet": owner_wallet,
            "remaining_episodes": player.remaining_episodes,
            "total_quota": player.total_quota,
            "note": "Selected agent registered using local game config only.",
        }
    )


@router.post("/game/create")
def route_create_game(
    body: dict[str, object] = Body(default={}),
    store: GameStore = Depends(get_store),
) -> JSONResponse:
    bonfire_id = _required_string(body, "bonfire_id")
    wallet = _required_string(body, "wallet_address").lower()
    game_prompt = _required_string(body, "game_prompt")
    erc8004_bonfire_id = _required_int(body, "erc8004_bonfire_id")
    gm_agent_id_raw = body.get("gm_agent_id")
    gm_agent_id = (
        str(gm_agent_id_raw).strip()
        if isinstance(gm_agent_id_raw, str) and gm_agent_id_raw.strip()
        else None
    )
    quest_count_obj = body.get("initial_quest_count", 2)
    if not isinstance(quest_count_obj, int):
        raise ValueError("initial_quest_count must be an integer")
    quest_count = max(1, min(quest_count_obj, 5))

    owner_wallet_existing = store.get_owner_wallet(bonfire_id)
    if owner_wallet_existing and owner_wallet_existing.lower() != wallet:
        raise PermissionError("wallet is not the bonfire game owner")
    if not owner_wallet_existing:
        store.link_bonfire(
            bonfire_id=bonfire_id,
            erc8004_bonfire_id=erc8004_bonfire_id,
            owner_wallet=wallet,
        )

    seed = _seed_game_from_prompt(store, bonfire_id, wallet, game_prompt, gm_agent_id, quest_count)
    game = store.create_or_replace_game(
        bonfire_id=bonfire_id,
        owner_wallet=wallet,
        game_prompt=game_prompt,
        gm_agent_id=gm_agent_id,
        initial_episode_summary=str(seed.get("episode_summary", "")),
    )
    store.ensure_starting_room(bonfire_id)
    response: dict[str, object] = {
        "game_id": game.game_id,
        "bonfire_id": game.bonfire_id,
        "owner_wallet": game.owner_wallet,
        "game_prompt": game.game_prompt,
        "initial_episode_summary": game.initial_episode_summary,
        "initial_quests": seed.get("quests", []),
        "status": game.status,
    }
    if not gm_agent_id:
        response["warning"] = (
            "No dedicated gm_agent_id provided. The GM will use the bonfire owner's "
            "first non-player agent. For best results, create a separate agent for the GM."
        )
    return JSONResponse(response)


@router.post("/game/player/restore")
def route_restore_players(
    body: dict[str, object] = Body(default={}),
    store: GameStore = Depends(get_store),
) -> JSONResponse:
    wallet = _required_string(body, "wallet_address").lower()
    tx_hash_obj = body.get("purchase_tx_hash")
    tx_hash = _required_string(body, "purchase_tx_hash") if tx_hash_obj else None
    restored = store.restore_players(wallet=wallet, purchase_tx_hash=tx_hash)
    return JSONResponse({"wallet_address": wallet, "purchase_tx_hash": tx_hash, "players": restored})


@router.post("/game/agents/complete")
def route_agent_complete(
    body: dict[str, object] = Body(default={}),
    store: GameStore = Depends(get_store),
    agent_api_key_info: tuple[str, str] = Depends(_get_agent_api_key),
) -> JSONResponse:
    agent_id = _required_string(body, "agent_id")
    message = _required_string(body, "message")
    chat_id = _required_string(body, "chat_id") if body.get("chat_id") else f"game-{agent_id}"
    user_id = _required_string(body, "user_id") if body.get("user_id") else "game-user"
    as_game_master = bool(body.get("as_game_master", False))
    graph_mode = _resolve_graph_mode(body, default="regenerate")

    player = store.get_player(agent_id)
    if not player:
        return JSONResponse(status_code=404, content={"error": "agent is not registered in game"})

    agent_api_key, api_key_source = agent_api_key_info
    if not agent_api_key:
        return JSONResponse(
            status_code=503,
            content={"error": "Provide X-Agent-Api-Key or set config.DELVE_API_KEY on server"},
        )

    preamble = _build_game_context_preamble(store, agent_id)
    augmented_message = f"{preamble}{message}" if preamble else message

    chat_url = f"{config.DELVE_BASE_URL}/agents/{agent_id}/chat"
    chat_status, chat_payload = http_client._agent_json_request(
        "POST",
        chat_url,
        agent_api_key,
        body={
            "message": augmented_message,
            "chat_history": [],
            "graph_mode": graph_mode,
            "context": _build_agent_chat_context(store, agent_id),
        },
    )
    if chat_status != 200:
        return JSONResponse(
            status_code=chat_status, content={"error": "agent chat failed", "upstream": chat_payload}
        )

    assistant_reply_obj = chat_payload.get("reply")
    assistant_reply = (
        assistant_reply_obj if isinstance(assistant_reply_obj, str) else json.dumps(chat_payload)
    )

    now_iso = datetime.now(UTC).isoformat()
    room_prefix = ""
    if player.current_room:
        room_data = store.get_room_by_id(player.bonfire_id, player.current_room)
        if room_data:
            room_prefix = f"[Room: {room_data.get('name', 'Unknown')}] "

    stack_text_user = f"{room_prefix}{message}" if room_prefix else message
    stack_text_agent = f"{room_prefix}{assistant_reply}" if room_prefix else assistant_reply

    stack_url = f"{config.DELVE_BASE_URL}/agents/{agent_id}/stack/add"
    stack_status, stack_payload = http_client._agent_json_request(
        "POST",
        stack_url,
        agent_api_key,
        body={
            "messages": [
                {"text": stack_text_user, "userId": user_id, "chatId": chat_id, "timestamp": now_iso},
                {
                    "text": stack_text_agent,
                    "userId": f"agent:{agent_id}",
                    "chatId": chat_id,
                    "timestamp": now_iso,
                },
            ],
            "is_paired": True,
        },
    )
    if stack_status != 200:
        return JSONResponse(
            status_code=stack_status,
            content={"error": "stack add failed", "chat": chat_payload, "stack": stack_payload},
        )

    if player.current_room:
        store.append_room_message(
            room_id=player.current_room,
            sender_agent_id=agent_id,
            sender_wallet=player.wallet,
            role="user",
            text=message,
        )
        store.append_room_message(
            room_id=player.current_room,
            sender_agent_id=agent_id,
            sender_wallet=player.wallet,
            role="agent",
            text=assistant_reply,
        )

    response_body: dict[str, object] = {
        "agent_id": agent_id,
        "chat": chat_payload,
        "stack": stack_payload,
        "api_key_source": api_key_source,
        "graph_mode": graph_mode,
        "room_id": player.current_room,
        "note": "Message pair appended to stack; Game Master context updates only after episode creation via stack processing.",
    }

    if as_game_master:
        owner_wallet = store.get_owner_wallet(player.bonfire_id)
        if not owner_wallet or owner_wallet.lower() != player.wallet.lower():
            return JSONResponse(
                status_code=403,
                content={"error": "Only bonfire NFT owner agent can generate quests via completions"},
            )
        reward = body.get("reward", 1)
        if not isinstance(reward, int):
            raise ValueError("reward must be an integer")
        cooldown = body.get("cooldown_seconds", config.DEFAULT_CLAIM_COOLDOWN_SECONDS)
        if not isinstance(cooldown, int):
            raise ValueError("cooldown_seconds must be an integer")
        quest_type = str(body.get("quest_type", "gm_generated"))
        keyword_raw = body.get("keyword")
        keyword = (
            keyword_raw.strip().lower()
            if isinstance(keyword_raw, str) and keyword_raw.strip()
            else _derive_keyword_from_text(assistant_reply)
        )
        quest = store.create_quest(
            bonfire_id=player.bonfire_id,
            creator_wallet=player.wallet,
            quest_type=quest_type,
            prompt=assistant_reply,
            keyword=keyword,
            reward=reward,
            cooldown_seconds=cooldown,
            expires_in_seconds=None,
        )
        response_body["auto_quest"] = {
            "quest_id": quest.quest_id,
            "quest_type": quest.quest_type,
            "keyword": quest.keyword,
            "reward": quest.reward,
        }
        response_body["note"] = "Game Master completion auto-generated a quest."

    return JSONResponse(response_body)


@router.post("/game/agents/end-turn")
def route_end_turn(
    body: dict[str, object] = Body(default={}),
    store: GameStore = Depends(get_store),
    agent_api_key_info: tuple[str, str] = Depends(_get_agent_api_key),
) -> JSONResponse:
    agent_id = _required_string(body, "agent_id")
    agent_api_key, api_key_source = agent_api_key_info
    if not agent_api_key:
        return JSONResponse(
            status_code=503,
            content={"error": "Provide X-Agent-Api-Key or set config.DELVE_API_KEY on server"},
        )
    player = store.get_player(agent_id)
    if not player:
        return JSONResponse(status_code=404, content={"error": "agent is not registered in game"})

    bonfire_id = player.bonfire_id
    gm_agent_id = store.get_owner_agent_id(bonfire_id)

    url = f"{config.DELVE_BASE_URL}/agents/{agent_id}/stack/process"
    pre_uuids = _get_agent_episode_uuids(agent_id)
    proc_status, proc_payload = http_client._agent_json_request("POST", url, agent_api_key, body={})
    episode_id = _extract_episode_id_from_payload(proc_payload) if proc_status == 200 else ""
    if proc_status == 200 and not episode_id:
        episode_id = _poll_for_new_episode(agent_id, pre_uuids)

    if proc_status != 200:
        return JSONResponse(
            status_code=proc_status,
            content={"error": "stack processing failed", "upstream": proc_payload},
        )

    response: dict[str, object] = {
        "agent_id": agent_id,
        "episode_id": episode_id,
        "api_key_source": api_key_source,
    }

    if not episode_id:
        response["episode_pending"] = True
        response["note"] = "Stack processed but no episode yet. Try again shortly."
        return JSONResponse(response)

    episode_payload = stack_processing._fetch_episode_payload(bonfire_id, episode_id)
    if episode_payload is None:
        ep_inline = proc_payload.get("episode")
        if isinstance(ep_inline, dict):
            episode_payload = ep_inline
    episode_summary = (
        _extract_episode_summary(episode_payload)
        if episode_payload is not None
        else str(proc_payload.get("message") or proc_payload.get("detail") or f"Episode {episode_id}")
    )
    store.update_agent_context_from_episode(agent_id, episode_id, episode_summary)

    if player.current_room:
        _try_pin_room_graph_entity(store, bonfire_id, player.current_room)

    gm_decision: dict[str, object] = {}
    if gm_agent_id and config.DELVE_API_KEY and gm_agent_id != agent_id:
        game = store.get_game(bonfire_id)
        room_map = store.get_room_map(bonfire_id)
        room_summary = gm_engine._build_room_structured_summary(store, bonfire_id)
        game_context: dict[str, object] = {
            "bonfire_id": bonfire_id,
            "game_prompt": game.game_prompt if game else "",
            "world_state_summary": game.world_state_summary if game else "",
            "last_gm_reaction": game.last_gm_reaction if game else "",
            "rooms": room_map.get("rooms", []),
            "player_positions": room_map.get("players", []),
        }
        gm_url = f"{config.DELVE_BASE_URL}/agents/{gm_agent_id}/chat"
        gm_status, gm_payload = http_client._agent_json_request(
            "POST",
            gm_url,
            config.DELVE_API_KEY,
            body={
                "message": (
                    "You are the Game Master for a shared world. Read the episode and return strict JSON "
                    '{"extension_awarded": int, "reaction": string, "world_state_update": string, '
                    '"room_movements": [{"agent_id": string, "to_room": string}], '
                    '"new_rooms": [{"name": string, "description": string, "connections": [string]}], '
                    '"room_updates": [{"room_id": string, "description": string}], '
                    '"new_npcs": [{"name": string, "room_id": string, "personality": string, "description": string}], '
                    '"npc_updates": [{"npc_id": string, "room_id": string}], '
                    '"new_objects": [{"name": string, "description": string, "obj_type": string, '
                    '"location_type": "room"|"npc"|"player", "location_id": string, "properties": {}}], '
                    '"object_grants": [{"object_id": string, "to_agent_id": string}]}. '
                    "extension_awarded must be between 0 and 3. "
                    "room_movements moves players between known rooms when narratively appropriate. "
                    "new_rooms creates new areas for exploration (only when the story demands it). "
                    "room_updates changes descriptions of existing rooms as the world evolves. "
                    "new_npcs spawns NPCs in rooms. npc_updates moves NPCs. "
                    "new_objects creates items. object_grants gives items to players. "
                    "Use room names from the room list for movements. "
                    f"Episode id: {episode_id}. Episode summary: {episode_summary}.\n"
                    f"Room activity:\n{room_summary}\n"
                    f"Rooms: {json.dumps(room_map.get('rooms', []))}. "
                    f"Player positions: {json.dumps(room_map.get('players', []))}"
                ),
                "chat_history": [],
                "graph_mode": "adaptive",
                "context": {
                    "role": "game_master",
                    "bonfire_id": bonfire_id,
                    "episode_id": episode_id,
                    "episode": episode_payload or {"summary": episode_summary},
                    "game": game_context,
                },
            },
        )
        if gm_status == 200:
            reply = gm_payload.get("reply")
            if isinstance(reply, str):
                parsed = _safe_json_object(reply)
                if parsed:
                    ext_obj = parsed.get("extension_awarded", 0)
                    extension = ext_obj if isinstance(ext_obj, int) else 0
                    extension = max(0, min(extension, 3))
                    gm_decision = {
                        "extension_awarded": extension,
                        "reaction": str(parsed.get("reaction", "GM reviewed the episode.")).strip(),
                        "world_state_update": str(parsed.get("world_state_update", "")).strip(),
                        "room_movements": parsed.get("room_movements", []),
                        "new_rooms": parsed.get("new_rooms", []),
                        "room_updates": parsed.get("room_updates", []),
                        "new_npcs": parsed.get("new_npcs", []),
                        "npc_updates": parsed.get("npc_updates", []),
                        "new_objects": parsed.get("new_objects", []),
                        "object_grants": parsed.get("object_grants", []),
                        "source": "gm_llm",
                    }

        gm_reaction_text = str(gm_decision.get("reaction", "")).strip()
        gm_world_update = str(gm_decision.get("world_state_update", "")).strip()
        if gm_reaction_text or gm_world_update:
            now_iso = datetime.now(UTC).isoformat()
            stack_add_url = f"{config.DELVE_BASE_URL}/agents/{gm_agent_id}/stack/add"
            http_client._agent_json_request(
                "POST",
                stack_add_url,
                config.DELVE_API_KEY,
                body={
                    "messages": [
                        {
                            "text": f"[Player {agent_id} episode] {episode_summary}",
                            "userId": f"player:{agent_id}",
                            "chatId": f"gm-{bonfire_id}",
                            "timestamp": now_iso,
                        },
                        {
                            "text": f"GM reaction: {gm_reaction_text}\nWorld update: {gm_world_update}",
                            "userId": f"gm:{gm_agent_id}",
                            "chatId": f"gm-{bonfire_id}",
                            "timestamp": now_iso,
                        },
                    ],
                    "is_paired": True,
                },
            )
    else:
        gm_decision = gm_engine._make_gm_decision(
            store, agent_id, episode_summary, episode_id, episode_payload
        )

    reaction = str(gm_decision.get("reaction", "")).strip()
    world_update = str(gm_decision.get("world_state_update", "")).strip()

    store.update_game_world_state(
        bonfire_id=bonfire_id, episode_id=episode_id, world_state_summary=world_update, gm_reaction=reaction
    )
    store.update_agent_context_with_gm_response(
        agent_id=agent_id,
        episode_id=episode_id,
        gm_reaction=reaction,
        world_state_update=world_update,
    )

    ext = gm_decision.get("extension_awarded", 0)
    extension_awarded = ext if isinstance(ext, int) else 0
    if extension_awarded > 0:
        recharge = store.recharge_agent(bonfire_id, agent_id, extension_awarded, "gm_episode_extension")
        response["episode_extension"] = {"extension_awarded": extension_awarded, "recharge": recharge}

    room_changes = gm_engine._apply_gm_room_changes(store, bonfire_id, gm_decision)
    npc_obj_changes = gm_engine._apply_gm_npc_and_object_changes(store, bonfire_id, gm_decision)

    response["gm_decision"] = gm_decision
    response["room_changes"] = room_changes
    response["npc_object_changes"] = npc_obj_changes
    response["room_map"] = store.get_room_map(bonfire_id)

    game_obj = store.get_game(bonfire_id)
    if game_obj:
        response["world_state"] = {
            "world_state_summary": game_obj.world_state_summary,
            "last_gm_reaction": game_obj.last_gm_reaction,
            "last_episode_id": game_obj.last_episode_id,
        }

    return JSONResponse(response)


@router.post("/game/npc/interact")
def route_npc_interact(
    body: dict[str, object] = Body(default={}),
    store: GameStore = Depends(get_store),
) -> JSONResponse:
    agent_id = _required_string(body, "agent_id")
    npc_id = _required_string(body, "npc_id")
    message = _required_string(body, "message")

    player = store.get_player(agent_id)
    if not player:
        return JSONResponse(status_code=404, content={"error": "agent is not registered in game"})

    bonfire_id = player.bonfire_id
    npc = store.get_npc(bonfire_id, npc_id)
    if not npc:
        return JSONResponse(status_code=404, content={"error": "npc_not_found"})

    game = store.games_by_bonfire.get(bonfire_id)
    gm_agent_id = game.gm_agent_id if game else None
    if not gm_agent_id:
        return JSONResponse(status_code=503, content={"error": "no_gm_agent"})

    npc_inventory_text = ""
    if npc.inventory:
        npc_items = []
        for oid in npc.inventory:
            obj = store.get_object(bonfire_id, oid)
            if obj and not obj.is_consumed:
                npc_items.append(f"- {obj.name}: {obj.description}")
        if npc_items:
            npc_inventory_text = "\nItems you carry:\n" + "\n".join(npc_items)

    player_inv_text = ""
    player_items = store.get_player_inventory(bonfire_id, agent_id)
    if player_items:
        player_inv_text = "\nThe adventurer carries:\n" + "\n".join(
            f"- {it['name']}: {it['description']}" for it in player_items
        )

    room = store.get_room_by_id(bonfire_id, npc.room_id)
    room_name = room.get("name", "Unknown") if room else "Unknown"

    graph_context = ""
    if npc.graph_entity_uuid:
        graph_context = _fetch_room_graph_context(bonfire_id, npc.graph_entity_uuid)
        if graph_context:
            graph_context = f"\n[YOUR KNOWLEDGE]\n{graph_context}"

    npc_prompt = (
        f"You are {npc.name}. {npc.personality}\n"
        f"Dialogue style: {npc.dialogue_style or 'natural'}\n"
        f"You are in: {room_name}\n"
        f"Description: {npc.description}{npc_inventory_text}{player_inv_text}{graph_context}\n\n"
        f"Stay in character. Respond as {npc.name} would. "
        f"Do NOT break character or reveal you are an AI."
    )

    chat_url = f"{config.DELVE_BASE_URL}/agents/{gm_agent_id}/chat"
    chat_status, chat_payload = http_client._agent_json_request(
        "POST",
        chat_url,
        config.DELVE_API_KEY,
        body={"message": message, "chat_history": [], "graph_mode": "disabled", "context": npc_prompt},
    )
    if chat_status != 200:
        return JSONResponse(status_code=chat_status, content=chat_payload)

    reply = ""
    if isinstance(chat_payload, dict):
        reply = str(chat_payload.get("reply") or chat_payload.get("message") or "")

    if player.current_room:
        store.append_room_message(player.current_room, npc_id, "", "npc", f"[{npc.name}] {reply}")

    return JSONResponse(
        {"npc_id": npc_id, "npc_name": npc.name, "reply": reply, "room_id": npc.room_id}
    )


@router.post("/game/inventory/use")
def route_inventory_use(
    body: dict[str, object] = Body(default={}),
    store: GameStore = Depends(get_store),
) -> JSONResponse:
    agent_id = _required_string(body, "agent_id")
    object_id = _required_string(body, "object_id")

    player = store.get_player(agent_id)
    if not player:
        return JSONResponse(status_code=404, content={"error": "agent is not registered in game"})

    result = store.use_object(player.bonfire_id, agent_id, object_id)
    if not result.get("success"):
        return JSONResponse(status_code=400, content=result)

    effects_raw = result.get("effects", [])
    effects = effects_raw if isinstance(effects_raw, list) else []
    if effects and player.current_room:
        store.append_room_message(
            player.current_room,
            agent_id,
            player.wallet,
            "system",
            f"Used item: {', '.join(str(e) for e in effects)}",
        )
    return JSONResponse(result)


@router.post("/game/agents/process-stack")
def route_process_stack(
    body: dict[str, object] = Body(default={}),
    store: GameStore = Depends(get_store),
    agent_api_key_info: tuple[str, str] = Depends(_get_agent_api_key),
) -> JSONResponse:
    agent_id = _required_string(body, "agent_id")
    agent_api_key, api_key_source = agent_api_key_info
    if not agent_api_key:
        return JSONResponse(
            status_code=503,
            content={"error": "Provide X-Agent-Api-Key or set config.DELVE_API_KEY on server"},
        )

    url = f"{config.DELVE_BASE_URL}/agents/{agent_id}/stack/process"
    pre_uuids = _get_agent_episode_uuids(agent_id)
    status, payload = http_client._agent_json_request("POST", url, agent_api_key, body={})
    episode_id = _extract_episode_id_from_payload(payload) if status == 200 else ""
    if status == 200 and not episode_id:
        episode_id = _poll_for_new_episode(agent_id, pre_uuids)

    response_payload: dict[str, object] = dict(payload)
    response_payload["api_key_source"] = api_key_source

    if status == 200:
        if episode_id:
            summary_obj = payload.get("message") or payload.get("detail") or ""
            player = store.get_player(agent_id)
            bonfire_id = player.bonfire_id if player else ""
            episode_payload = (
                stack_processing._fetch_episode_payload(bonfire_id, episode_id) if bonfire_id else None
            )
            if episode_payload is None:
                payload_episode_obj = payload.get("episode")
                if isinstance(payload_episode_obj, dict):
                    episode_payload = payload_episode_obj
            episode_summary = (
                _extract_episode_summary(episode_payload)
                if episode_payload is not None
                else str(summary_obj) or f"Episode {episode_id} processed."
            )
            gm_context = store.update_agent_context_from_episode(agent_id, episode_id, episode_summary)
            response_payload["game_master_context"] = gm_context
            gm_decision = gm_engine._make_gm_decision(
                store, agent_id, episode_summary, episode_id, episode_payload
            )
            reaction = str(gm_decision.get("reaction", "")).strip()
            world_update = str(gm_decision.get("world_state_update", "")).strip()
            if player:
                updated_world = store.update_game_world_state(
                    bonfire_id=player.bonfire_id,
                    episode_id=episode_id,
                    world_state_summary=world_update,
                    gm_reaction=reaction,
                )
                if updated_world:
                    response_payload["world_state"] = updated_world
            gm_agent_ctx = store.update_agent_context_with_gm_response(
                agent_id=agent_id,
                episode_id=episode_id,
                gm_reaction=reaction,
                world_state_update=world_update,
            )
            response_payload["agent_gm_context"] = gm_agent_ctx
            extension_obj = gm_decision.get("extension_awarded", 0)
            extension = extension_obj if isinstance(extension_obj, int) else 0
            if extension > 0 and player:
                recharge_result = store.recharge_agent(
                    bonfire_id=player.bonfire_id,
                    agent_id=agent_id,
                    amount=extension,
                    reason="gm_episode_extension",
                )
                response_payload["episode_extension"] = {
                    "extension_awarded": extension,
                    "recharge": recharge_result,
                }
            response_payload["gm_decision"] = gm_decision
        else:
            response_payload["episode_pending"] = True
            response_payload["note"] = (
                "Stack processed but no episode id returned yet. "
                "Retry process-stack in a moment to finalize world-state update."
            )

    return JSONResponse(status_code=status, content=response_payload)


@router.post("/game/agents/gm-react")
def route_gm_react(
    body: dict[str, object] = Body(default={}),
    store: GameStore = Depends(get_store),
) -> JSONResponse:
    agent_id = _required_string(body, "agent_id")
    episode_id_obj = body.get("episode_id")
    episode_id = (
        str(episode_id_obj).strip()
        if isinstance(episode_id_obj, str) and episode_id_obj.strip()
        else None
    )
    status, payload = _trigger_gm_reaction_for_agent(store, agent_id, episode_id=episode_id)
    return JSONResponse(status_code=status, content=payload)


@router.post("/game/world/generate-episode")
def route_generate_world_episode(
    body: dict[str, object] = Body(default={}),
    store: GameStore = Depends(get_store),
) -> JSONResponse:
    bonfire_id = _required_string(body, "bonfire_id")
    game = store.get_game(bonfire_id)
    if not game:
        return JSONResponse(status_code=404, content={"error": "game not found for bonfire"})
    owner_agent_id = store.get_owner_agent_id(bonfire_id)
    if not owner_agent_id:
        return JSONResponse(
            status_code=400,
            content={"error": "no owner agent available to publish world episode"},
        )
    if not config.DELVE_API_KEY:
        return JSONResponse(
            status_code=503,
            content={"error": "config.DELVE_API_KEY is required for GM world episode generation"},
        )

    world_summary = game.world_state_summary.strip()
    gm_reaction = game.last_gm_reaction.strip()
    if not world_summary and not gm_reaction:
        return JSONResponse(
            status_code=400,
            content={"error": "no GM world update available; trigger GM reaction first"},
        )

    gm_prompt = (
        "You are the Game Master. Publish an in-world update as a short episode entry.\n"
        f"World state update: {world_summary or 'n/a'}\n"
        f"GM reaction: {gm_reaction or 'n/a'}\n"
        "Return a concise narrative update."
    )
    chat_url = f"{config.DELVE_BASE_URL}/agents/{owner_agent_id}/chat"
    chat_status, chat_payload = http_client._agent_json_request(
        "POST",
        chat_url,
        config.DELVE_API_KEY,
        body={
            "message": gm_prompt,
            "chat_history": [],
            "graph_mode": "append",
            "context": {"role": "game_master", "bonfire_id": bonfire_id},
        },
    )
    if chat_status != 200:
        return JSONResponse(
            status_code=chat_status,
            content={"error": "gm world chat failed", "upstream": chat_payload},
        )

    reply_obj = chat_payload.get("reply")
    reply = str(reply_obj) if reply_obj is not None else ""
    now_iso = datetime.now(UTC).isoformat()

    add_url = f"{config.DELVE_BASE_URL}/agents/{owner_agent_id}/stack/add"
    add_status, add_payload = http_client._agent_json_request(
        "POST",
        add_url,
        config.DELVE_API_KEY,
        body={
            "messages": [
                {
                    "text": gm_prompt,
                    "userId": "game:gm",
                    "chatId": f"world-{bonfire_id}",
                    "timestamp": now_iso,
                },
                {
                    "text": reply,
                    "userId": f"agent:{owner_agent_id}",
                    "chatId": f"world-{bonfire_id}",
                    "timestamp": now_iso,
                },
            ],
            "is_paired": True,
        },
    )
    if add_status != 200:
        return JSONResponse(
            status_code=add_status,
            content={"error": "gm stack add failed", "chat": chat_payload, "stack": add_payload},
        )

    process_url = f"{config.DELVE_BASE_URL}/agents/{owner_agent_id}/stack/process"
    process_status, process_payload = http_client._agent_json_request(
        "POST", process_url, config.DELVE_API_KEY, body={}
    )
    episode_id = _extract_episode_id_from_payload(process_payload) if process_status == 200 else ""
    if process_status == 200 and episode_id:
        store.update_game_world_state(
            bonfire_id=bonfire_id,
            episode_id=episode_id,
            world_state_summary=world_summary or reply,
            gm_reaction=gm_reaction or "World update published.",
        )
    return JSONResponse(
        status_code=process_status,
        content={
            "bonfire_id": bonfire_id,
            "owner_agent_id": owner_agent_id,
            "episode_id": episode_id,
            "chat": chat_payload,
            "stack_add": add_payload,
            "stack_process": process_payload,
        },
    )


@router.post("/game/stack/process-all")
def route_process_all_stacks(store: GameStore = Depends(get_store)) -> JSONResponse:
    if not config.DELVE_API_KEY:
        return JSONResponse(
            status_code=503,
            content={"error": "config.DELVE_API_KEY is required for stack processing"},
        )
    result = stack_processing._process_all_agent_stacks(store)
    return JSONResponse(result)


@router.post("/game/admin/backfill-world-state")
def route_backfill_world_state(
    body: dict[str, object] = Body(default={}),
    store: GameStore = Depends(get_store),
) -> JSONResponse:
    bonfire_id = _required_string(body, "bonfire_id")
    requested_episode_id = str(body.get("episode_id") or "").strip()

    game = store.get_game(bonfire_id)
    if not game:
        return JSONResponse(
            status_code=404, content={"error": "game_not_found", "bonfire_id": bonfire_id}
        )

    episode_payload: dict[str, object] | None = None
    episode_id = ""

    if requested_episode_id:
        episode_payload = stack_processing._fetch_episode_payload(bonfire_id, requested_episode_id)
        episode_id = requested_episode_id
    else:
        agent_ids = store.get_all_agent_ids()
        for aid in agent_ids:
            player = store.get_player(aid)
            if player and player.bonfire_id == bonfire_id:
                uuids = _get_agent_episode_uuids(aid)
                for uuid in reversed(uuids):
                    candidate = stack_processing._fetch_episode_payload(bonfire_id, uuid)
                    if candidate:
                        episode_id = uuid
                        episode_payload = candidate
                        break
                if episode_payload:
                    break

        if not episode_payload:
            episodes = _fetch_bonfire_episodes(bonfire_id, 10)
            for ep in reversed(episodes):
                candidate_id = _extract_episode_id_from_payload(ep)
                if candidate_id:
                    episode_id = candidate_id
                    episode_payload = ep
                    break

    if not episode_id or episode_payload is None:
        return JSONResponse(
            status_code=404,
            content={
                "error": "no_episodes_found",
                "bonfire_id": bonfire_id,
                "detail": "No episodes available to backfill from.",
            },
        )

    episode_summary = _extract_episode_summary(episode_payload)
    agent_ids = store.get_all_agent_ids()
    target_agent_id = ""
    for aid in agent_ids:
        player = store.get_player(aid)
        if player and player.bonfire_id == bonfire_id:
            target_agent_id = aid
            break

    if target_agent_id:
        store.update_agent_context_from_episode(target_agent_id, episode_id, episode_summary)

    gm_decision = gm_engine._make_gm_decision(
        store,
        target_agent_id or (agent_ids[0] if agent_ids else ""),
        episode_summary,
        episode_id,
        episode_payload,
    )
    reaction = str(gm_decision.get("reaction", "")).strip()
    world_update = str(gm_decision.get("world_state_update", "")).strip()

    world_state = store.update_game_world_state(
        bonfire_id=bonfire_id,
        episode_id=episode_id,
        world_state_summary=world_update,
        gm_reaction=reaction,
    )

    if target_agent_id:
        store.update_agent_context_with_gm_response(
            agent_id=target_agent_id,
            episode_id=episode_id,
            gm_reaction=reaction,
            world_state_update=world_update,
        )

    return JSONResponse(
        {
            "backfilled": True,
            "episode_id": episode_id,
            "episode_summary": episode_summary,
            "gm_decision": gm_decision,
            "world_state": world_state,
            "agent_id": target_agent_id or None,
        }
    )


@router.post("/game/turn")
def route_turn(
    body: dict[str, object] = Body(default={}),
    store: GameStore = Depends(get_store),
) -> JSONResponse:
    agent_id = _required_string(body, "agent_id")
    action = _required_string(body, "action")
    out = store.run_turn(agent_id=agent_id, action=action)
    return JSONResponse(out)


@router.post("/game/quests/create")
def route_create_quest(
    body: dict[str, object] = Body(default={}),
    store: GameStore = Depends(get_store),
) -> JSONResponse:
    bonfire_id = _required_string(body, "bonfire_id")
    creator_wallet = _required_string(body, "wallet_address")
    _assert_owner(store, bonfire_id, creator_wallet)
    reward = _required_int(body, "reward")
    cooldown_raw = body.get("cooldown_seconds", config.DEFAULT_CLAIM_COOLDOWN_SECONDS)
    if not isinstance(cooldown_raw, int):
        raise ValueError("cooldown_seconds must be an integer")
    cooldown = cooldown_raw
    expires_in_seconds = body.get("expires_in_seconds")
    expires_int: int | None = expires_in_seconds if isinstance(expires_in_seconds, int) else None
    quest = store.create_quest(
        bonfire_id=bonfire_id,
        creator_wallet=creator_wallet,
        quest_type=_required_string(body, "quest_type"),
        prompt=_required_string(body, "prompt"),
        keyword=_required_string(body, "keyword"),
        reward=reward,
        cooldown_seconds=cooldown,
        expires_in_seconds=expires_int,
    )
    return JSONResponse(
        {
            "quest_id": quest.quest_id,
            "quest_type": quest.quest_type,
            "reward": quest.reward,
            "status": quest.status,
        }
    )


@router.post("/game/quests/claim")
def route_claim_quest(
    body: dict[str, object] = Body(default={}),
    store: GameStore = Depends(get_store),
) -> JSONResponse:
    quest_id = _required_string(body, "quest_id")
    agent_id = _required_string(body, "agent_id")
    submission = _required_string(body, "submission")
    out = store.claim_quest(quest_id=quest_id, agent_id=agent_id, submission=submission)
    return JSONResponse(out)


@router.post("/game/agents/recharge")
def route_recharge_agent(
    body: dict[str, object] = Body(default={}),
    store: GameStore = Depends(get_store),
) -> JSONResponse:
    bonfire_id = _required_string(body, "bonfire_id")
    wallet = _required_string(body, "wallet_address")
    _assert_owner(store, bonfire_id, wallet)
    out = store.recharge_agent(
        bonfire_id=bonfire_id,
        agent_id=_required_string(body, "agent_id"),
        amount=_required_int(body, "amount"),
        reason=_required_string(body, "reason"),
    )
    return JSONResponse(out)


@router.post("/game/map/init")
def route_map_init(
    body: dict[str, object] = Body(default={}),
    store: GameStore = Depends(get_store),
) -> JSONResponse:
    bonfire_id = _required_string(body, "bonfire_id")
    room_id = store.ensure_starting_room(bonfire_id)
    if not room_id:
        return JSONResponse(status_code=404, content={"error": "game not found for bonfire"})
    return JSONResponse(store.get_room_map(bonfire_id))


@router.post("/game/entity/expand")
def route_entity_expand(body: dict[str, object] = Body(default={})) -> JSONResponse:
    entity_uuid = _required_string(body, "entity_uuid")
    bonfire_id = _required_string(body, "bonfire_id")
    limit = body.get("limit", 50)
    if not isinstance(limit, int):
        limit = 50

    url = f"{config.DELVE_BASE_URL}/knowledge_graph/expand/entity"
    req_body: dict[str, object] = {"entity_uuid": entity_uuid, "bonfire_id": bonfire_id, "limit": limit}
    status, payload = http_client._json_request("POST", url, req_body)
    if status != 200:
        return JSONResponse(status_code=status, content=payload)

    nodes = _normalize_graph_nodes(payload.get("nodes") or payload.get("entities") or [])
    edges = _normalize_graph_edges(payload.get("edges") or [])
    episodes = payload.get("episodes") or []
    return JSONResponse({"nodes": nodes, "edges": edges, "episodes": episodes})


@router.post("/game/quests/generate")
def route_generate_quests(
    body: dict[str, object] = Body(default={}),
    store: GameStore = Depends(get_store),
) -> JSONResponse:
    bonfire_id = _required_string(body, "bonfire_id")
    game = store.get_game(bonfire_id)
    if not game:
        return JSONResponse(status_code=404, content={"error": "game_not_found"})

    world_state = game.world_state_summary or game.game_prompt or ""
    query_text = world_state[:300] if world_state else "explore the world"

    delve_url = f"{config.DELVE_BASE_URL}/delve"
    delve_body: dict[str, object] = {"query": query_text, "bonfire_id": bonfire_id, "num_results": 15}
    status, delve_payload = http_client._json_request("POST", delve_url, delve_body)
    entities: list[dict[str, object]] = []
    if status == 200 and isinstance(delve_payload, dict):
        raw = delve_payload.get("entities") or delve_payload.get("nodes") or []
        if isinstance(raw, list):
            entities = [e for e in raw if isinstance(e, dict)]

    if not entities:
        return JSONResponse(
            {"quests": [], "note": "No graph entities available for quest generation"}
        )

    existing_keywords: set[str] = set()
    for q_dict in (store.quests_by_bonfire.get(bonfire_id) or {}).values():
        if q_dict.status == "active":
            existing_keywords.add(q_dict.keyword.lower())

    candidates: list[dict[str, object]] = []
    for ent in entities:
        name = str(ent.get("name") or "").strip()
        if not name or name.lower() in existing_keywords or len(name) < 2:
            continue
        candidates.append(ent)
        if len(candidates) >= 3:
            break

    if not candidates:
        return JSONResponse(
            {"quests": [], "note": "All interesting entities already have active quests"}
        )

    owner_agent_id = game.gm_agent_id or ""
    created_quests: list[dict[str, object]] = []

    for ent in candidates:
        ent_name = str(ent.get("name") or "entity")
        ent_summary = str(ent.get("summary") or ent.get("description") or "")
        keyword = ent_name.lower().split()[0] if ent_name else "explore"

        if owner_agent_id and config.DELVE_API_KEY:
            gm_prompt = (
                f"You are the Game Master. Generate a short quest (1-2 sentences) about investigating "
                f"'{ent_name}' in the game world. Context: {ent_summary[:200]}. "
                f"World state: {world_state[:200]}. "
                f"Reply with ONLY a JSON object: "
                f'{{"prompt": "quest text", "keyword": "single_word", "reward": 1}}'
            )
            gm_url = f"{config.DELVE_BASE_URL}/agents/{owner_agent_id}/chat"
            gm_status, gm_payload = http_client._agent_json_request(
                "POST", gm_url, config.DELVE_API_KEY, body={"message": gm_prompt, "graph_mode": "static"}
            )
            if gm_status == 200 and isinstance(gm_payload, dict):
                reply = str(gm_payload.get("reply") or gm_payload.get("message") or "")
                try:
                    parsed = json.loads(reply)
                    if isinstance(parsed, dict):
                        keyword = str(parsed.get("keyword") or keyword).strip().lower()
                        ent_name = str(parsed.get("prompt") or ent_name)
                        reward_raw = parsed.get("reward", 1)
                        reward = reward_raw if isinstance(reward_raw, int) and 1 <= reward_raw <= 5 else 1
                    else:
                        reward = 1
                except json.JSONDecodeError:
                    reward = 1
                    ent_name = (
                        f"Investigate {ent_name}: {reply[:100]}"
                        if reply
                        else f"Investigate {ent_name}"
                    )
            else:
                reward = 1
                ent_name = f"Investigate the entity known as '{ent_name}' and discover its role in the world."
        else:
            reward = 1
            ent_name = f"Investigate the entity known as '{ent_name}' and discover its role in the world."

        if keyword.lower() in existing_keywords:
            continue
        existing_keywords.add(keyword.lower())

        quest = store.create_quest(
            bonfire_id=bonfire_id,
            creator_wallet=game.owner_wallet,
            quest_type="graph_discovery",
            prompt=ent_name,
            keyword=keyword,
            reward=reward,
            cooldown_seconds=config.DEFAULT_CLAIM_COOLDOWN_SECONDS,
            expires_in_seconds=None,
        )
        created_quests.append(
            {
                "quest_id": quest.quest_id,
                "quest_type": quest.quest_type,
                "prompt": quest.prompt,
                "keyword": quest.keyword,
                "reward": quest.reward,
                "entity_uuid": str(ent.get("uuid") or ""),
                "entity_name": str(ent.get("name") or ""),
            }
        )

    return JSONResponse({"quests": created_quests, "count": len(created_quests)})
