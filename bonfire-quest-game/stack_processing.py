"""Episode, stack, and GM-stack processing utilities."""

from __future__ import annotations

import json
import time
from datetime import UTC, datetime

import game_config as config
import http_client
import gm_engine
from game_store import GameStore


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


def _resolve_latest_episode_from_agent(agent_id: str) -> str:
    """Fetch the agent object and return the last entry in episode_uuids."""
    url = f"{config.DELVE_BASE_URL}/agents/{agent_id}"
    status, payload = http_client._json_request("GET", url)
    if status != 200 or not isinstance(payload, dict):
        return ""
    uuids = payload.get("episode_uuids") or payload.get("episodeUuids") or payload.get("episode_ids") or []
    if not isinstance(uuids, list) or len(uuids) == 0:
        return ""
    last = uuids[-1]
    if isinstance(last, str) and last.strip():
        return last.strip()
    if isinstance(last, dict):
        oid = last.get("$oid")
        if isinstance(oid, str) and oid.strip():
            return oid.strip()
    return ""


def _fetch_episode_payload(bonfire_id: str, episode_id: str) -> dict[str, object] | None:
    """Fetch full episode payload by ID (UUID or ObjectId), trying MongoDB UUID lookup first."""
    uuid_url = f"{config.DELVE_BASE_URL}/episodes/by-uuid/{episode_id}"
    uuid_status, uuid_payload = http_client._json_request("GET", uuid_url)
    if uuid_status == 200 and isinstance(uuid_payload, dict):
        return uuid_payload

    for url in (
        f"{config.DELVE_BASE_URL}/episodes/{episode_id}",
        f"{config.DELVE_BASE_URL}/bonfires/{bonfire_id}/episodes/{episode_id}",
    ):
        status, payload = http_client._json_request("GET", url)
        if status != 200:
            continue
        episode_obj = payload.get("episode")
        if isinstance(episode_obj, dict):
            return episode_obj
        data_obj = payload.get("data")
        if isinstance(data_obj, dict):
            return data_obj
        if isinstance(payload, dict):
            return payload
    list_url = f"{config.DELVE_BASE_URL}/bonfires/{bonfire_id}/episodes?limit=50"
    list_status, list_payload = http_client._json_request("GET", list_url)
    if list_status == 200:
        episodes = list_payload.get("episodes")
        if isinstance(episodes, list):
            for item in episodes:
                if isinstance(item, dict):
                    eid = _extract_episode_id_from_payload(item)
                    if eid == episode_id:
                        return item
    return None


def _get_agent_episode_uuids_standalone(agent_id: str) -> list[str]:
    """Snapshot current episode_uuids for an agent (module-level helper)."""
    status, payload = http_client._json_request("GET", f"{config.DELVE_BASE_URL}/agents/{agent_id}")
    if status != 200 or not isinstance(payload, dict):
        print(f"  [poll] GET /agents/{agent_id} returned {status}")
        return []
    uuids = payload.get("episode_uuids") or payload.get("episodeUuids") or []
    return [str(u) for u in uuids] if isinstance(uuids, list) else []


def _poll_for_new_episode_standalone(
    agent_id: str, pre_uuids: list[str], max_wait: float = 45.0, interval: float = 3.0
) -> str:
    """Poll agent's episode_uuids until a new entry appears or timeout (module-level helper)."""
    pre_set = set(pre_uuids)
    elapsed = 0.0
    print(f"  [poll] Waiting for new episode on agent {agent_id} (pre={len(pre_uuids)} uuids, max_wait={max_wait}s)")
    while elapsed < max_wait:
        time.sleep(interval)
        elapsed += interval
        current = _get_agent_episode_uuids_standalone(agent_id)
        new_uuids = [u for u in current if u not in pre_set]
        if new_uuids:
            print(f"  [poll] Found new episode after {elapsed:.0f}s: {new_uuids[-1]}")
            return new_uuids[-1]
        print(f"  [poll] {elapsed:.0f}s elapsed, {len(current)} total uuids, no new yet")
    print(f"  [poll] Timed out after {max_wait}s for agent {agent_id}")
    return _resolve_latest_episode_from_agent(agent_id) or ""


def _process_all_agent_stacks(store: GameStore) -> dict[str, object]:
    """Process stack for every registered agent using server API key."""
    agent_ids = store.get_all_agent_ids()
    processed: list[dict[str, object]] = []
    for agent_id in agent_ids:
        url = f"{config.DELVE_BASE_URL}/agents/{agent_id}/stack/process"
        pre_uuids = _get_agent_episode_uuids_standalone(agent_id)
        status, payload = http_client._agent_json_request("POST", url, config.DELVE_API_KEY, body={})
        player = store.get_player(agent_id)
        result_entry: dict[str, object] = {"agent_id": agent_id, "status": status, "payload": payload}

        if status == 200 and isinstance(payload, dict):
            episode_id = _extract_episode_id_from_payload(payload)
            if not episode_id:
                episode_id = _poll_for_new_episode_standalone(agent_id, pre_uuids)

            if episode_id and player:
                bonfire_id = player.bonfire_id
                episode_payload = _fetch_episode_payload(bonfire_id, episode_id)
                if episode_payload is None:
                    ep_inline = payload.get("episode")
                    if isinstance(ep_inline, dict):
                        episode_payload = ep_inline

                episode_summary = (
                    _extract_episode_summary(episode_payload)
                    if episode_payload is not None
                    else str(payload.get("message") or payload.get("detail") or f"Episode {episode_id} processed.")
                )

                store.update_agent_context_from_episode(agent_id, episode_id, episode_summary)

                gm_decision = gm_engine._make_gm_decision(store, agent_id, episode_summary, episode_id, episode_payload)
                reaction = str(gm_decision.get("reaction", "")).strip()
                world_update = str(gm_decision.get("world_state_update", "")).strip()

                store.update_game_world_state(
                    bonfire_id=bonfire_id,
                    episode_id=episode_id,
                    world_state_summary=world_update,
                    gm_reaction=reaction,
                )
                store.update_agent_context_with_gm_response(
                    agent_id=agent_id,
                    episode_id=episode_id,
                    gm_reaction=reaction,
                    world_state_update=world_update,
                )

                result_entry["gm_decision"] = gm_decision
                result_entry["episode_id"] = episode_id

                ext_obj = gm_decision.get("extension_awarded", 0)
                extension = ext_obj if isinstance(ext_obj, int) else 0
                if extension > 0:
                    recharge = store.recharge_agent(bonfire_id, agent_id, extension, "gm_episode_extension")
                    result_entry["episode_extension"] = {"extension_awarded": extension, "recharge": recharge}

        if player:
            store._append_event(
                player.bonfire_id,
                "stack_processed",
                {
                    "agent_id": agent_id,
                    "status": status,
                    "success": status == 200,
                    "episode_id": result_entry.get("episode_id", ""),
                },
            )
        processed.append(result_entry)
    return {
        "processed_count": len(processed),
        "results": processed,
        "at": datetime.now(UTC).isoformat(),
    }


def _process_gm_stacks(store: GameStore) -> dict[str, object]:
    """Process the stack of every distinct GM agent across all active games."""
    processed: list[dict[str, object]] = []
    seen_gm_ids: set[str] = set()
    for game in store.list_active_games():
        bonfire_id = str(game.get("bonfire_id", ""))
        gm_agent_id = store.get_owner_agent_id(bonfire_id)
        if not gm_agent_id or gm_agent_id in seen_gm_ids:
            continue
        seen_gm_ids.add(gm_agent_id)
        if not config.DELVE_API_KEY:
            continue

        room_summary = gm_engine._build_room_structured_summary(store, bonfire_id)
        if room_summary:
            now_iso = datetime.now(UTC).isoformat()
            game_obj = store.get_game(bonfire_id)
            world_state = game_obj.world_state_summary if game_obj else ""
            summary_msg = (
                "You are the Game Master. Here is the current room-by-room activity summary "
                "for your world. Use this to inform your next narrative episode.\n"
                f"World state: {world_state}\n\n{room_summary}"
            )
            http_client._agent_json_request(
                "POST",
                f"{config.DELVE_BASE_URL}/agents/{gm_agent_id}/stack/add",
                config.DELVE_API_KEY,
                body={
                    "messages": [
                        {
                            "text": summary_msg,
                            "userId": "system:gm-batch",
                            "chatId": f"gm-{bonfire_id}",
                            "timestamp": now_iso,
                        },
                    ],
                },
            )

        url = f"{config.DELVE_BASE_URL}/agents/{gm_agent_id}/stack/process"
        pre_uuids = _get_agent_episode_uuids_standalone(gm_agent_id)
        status, payload = http_client._agent_json_request("POST", url, config.DELVE_API_KEY, body={})
        episode_id = _extract_episode_id_from_payload(payload) if status == 200 else ""
        if status == 200 and not episode_id:
            episode_id = _poll_for_new_episode_standalone(gm_agent_id, pre_uuids)
        entry: dict[str, object] = {"gm_agent_id": gm_agent_id, "bonfire_id": bonfire_id, "status": status}
        if episode_id:
            entry["episode_id"] = episode_id
            episode_payload = _fetch_episode_payload(bonfire_id, episode_id)
            episode_summary = (
                _extract_episode_summary(episode_payload)
                if episode_payload is not None
                else f"GM episode {episode_id}"
            )
            game_obj = store.get_game(bonfire_id)
            store.update_game_world_state(
                bonfire_id=bonfire_id,
                episode_id=episode_id,
                world_state_summary=episode_summary,
                gm_reaction=game_obj.last_gm_reaction if game_obj else "",
            )
        processed.append(entry)
    return {"processed_count": len(processed), "results": processed, "at": datetime.now(UTC).isoformat()}
