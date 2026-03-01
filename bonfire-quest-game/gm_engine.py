"""Game Master decision logic and world-change application."""

from __future__ import annotations

import json

import game_config as config
import http_client
from game_store import GameStore


def _safe_json_object(text: str) -> dict[str, object] | None:
    """Attempt to parse a JSON object from LLM text, tolerating markdown fences."""
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


def _build_room_structured_summary(store: GameStore, bonfire_id: str) -> str:
    """Build a room-by-room summary of recent activity for GM context."""
    room_map = store.get_room_map(bonfire_id)
    rooms_raw = room_map.get("rooms", [])
    players_raw = room_map.get("players", [])
    rooms = rooms_raw if isinstance(rooms_raw, list) else []
    players = players_raw if isinstance(players_raw, list) else []
    if not rooms:
        return ""
    player_rooms: dict[str, list[str]] = {}
    for p in players:
        if not isinstance(p, dict):
            continue
        pr = str(p.get("current_room", ""))
        if pr:
            player_rooms.setdefault(pr, []).append(str(p.get("agent_id", "")))

    lines: list[str] = []
    for room in rooms:
        if not isinstance(room, dict):
            continue
        rid = str(room.get("room_id", ""))
        rname = str(room.get("name", "Unknown"))
        occupants = player_rooms.get(rid, [])
        msgs = store.get_room_messages(rid, limit=10)
        activity_parts: list[str] = []
        for msg in msgs:
            if not isinstance(msg, dict):
                continue
            role = str(msg.get("role", ""))
            sender = str(msg.get("sender_agent_id", ""))[:8]
            text = str(msg.get("text", ""))[:120]
            activity_parts.append(f"  [{role}:{sender}] {text}")
        occupant_str = ", ".join(occupants) if occupants else "empty"
        room_line = f'Room "{rname}" (players: {occupant_str})'
        if activity_parts:
            room_line += ":\n" + "\n".join(activity_parts[-5:])
        else:
            room_line += ": no recent activity"
        lines.append(room_line)
    return "\n".join(lines)


def _make_gm_decision(
    store: GameStore,
    agent_id: str,
    episode_summary: str,
    episode_id: str,
    episode_payload: dict[str, object] | None,
) -> dict[str, object]:
    """Run GM decision for a processed episode (usable outside request context)."""
    player = store.get_player(agent_id)
    if not player:
        return {
            "extension_awarded": 0,
            "reaction": "No registered player found.",
            "world_state_update": "",
            "source": "fallback",
        }

    owner_agent_id = store.get_owner_agent_id(player.bonfire_id)
    if owner_agent_id and config.DELVE_API_KEY:
        game = store.get_game(player.bonfire_id)
        room_map = store.get_room_map(player.bonfire_id)
        room_summary = _build_room_structured_summary(store, player.bonfire_id)
        game_context: dict[str, object] = {
            "bonfire_id": player.bonfire_id,
            "game_prompt": game.game_prompt if game else "",
            "world_state_summary": game.world_state_summary if game else "",
            "last_gm_reaction": game.last_gm_reaction if game else "",
            "rooms": room_map.get("rooms", []),
            "player_positions": room_map.get("players", []),
        }
        gm_url = f"{config.DELVE_BASE_URL}/agents/{owner_agent_id}/chat"
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
                    "room_movements moves players between rooms when narratively appropriate. "
                    "new_rooms creates new areas for exploration (only when the story demands it). "
                    "room_updates changes descriptions of existing rooms as the world evolves. "
                    "new_npcs spawns new NPCs in rooms. npc_updates moves NPCs between rooms. "
                    "new_objects creates items (key|tool|artifact|consumable). obj_type 'key' with "
                    'properties {"unlocks_room": "<room_id>"} unlocks passages. '
                    "object_grants gives existing objects to players. "
                    f"Episode id: {episode_id}. Episode summary: {episode_summary}.\n"
                    f"Room activity:\n{room_summary}\n"
                    f"Rooms: {json.dumps(room_map.get('rooms', []))}. "
                    f"Player positions: {json.dumps(room_map.get('players', []))}"
                ),
                "chat_history": [],
                "graph_mode": "adaptive",
                "context": {
                    "role": "game_master",
                    "bonfire_id": player.bonfire_id,
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
                    reaction_obj = parsed.get("reaction", "GM reviewed the episode.")
                    reaction = str(reaction_obj).strip() or "GM reviewed the episode."
                    world_update_obj = parsed.get("world_state_update", "")
                    world_update = str(world_update_obj).strip()
                    return {
                        "extension_awarded": extension,
                        "reaction": reaction,
                        "world_state_update": world_update,
                        "room_movements": parsed.get("room_movements", []),
                        "new_rooms": parsed.get("new_rooms", []),
                        "room_updates": parsed.get("room_updates", []),
                        "new_npcs": parsed.get("new_npcs", []),
                        "npc_updates": parsed.get("npc_updates", []),
                        "new_objects": parsed.get("new_objects", []),
                        "object_grants": parsed.get("object_grants", []),
                        "source": "gm_llm",
                    }

    lowered = episode_summary.lower()
    extension = 0
    if any(token in lowered for token in ["quest", "artifact", "discovery", "completed"]):
        extension = 1
    if "major" in lowered or "milestone" in lowered:
        extension = 2
    return {
        "extension_awarded": extension,
        "reaction": "GM auto-reviewed the episode and applied fallback rules.",
        "world_state_update": episode_summary,
        "room_movements": [],
        "new_rooms": [],
        "room_updates": [],
        "new_npcs": [],
        "npc_updates": [],
        "new_objects": [],
        "object_grants": [],
        "source": "fallback",
    }


def _apply_gm_room_changes(store: GameStore, bonfire_id: str, gm_decision: dict[str, object]) -> dict[str, object]:
    """Parse and apply new_rooms, room_updates, and room_movements from GM decision."""
    result: dict[str, object] = {"new_rooms_created": [], "rooms_updated": [], "movements_applied": []}

    new_rooms_raw = gm_decision.get("new_rooms", [])
    if isinstance(new_rooms_raw, list):
        created: list[dict[str, str]] = []
        for nr in new_rooms_raw:
            if not isinstance(nr, dict):
                continue
            name = str(nr.get("name", "")).strip()
            if not name:
                continue
            desc = str(nr.get("description", "")).strip()
            conns = nr.get("connections", [])
            conn_list = [str(c) for c in conns] if isinstance(conns, list) else []
            try:
                room = store.create_room(bonfire_id, name, desc, conn_list)
                created.append({"room_id": room.room_id, "name": room.name})
            except ValueError:
                pass
        result["new_rooms_created"] = created

    room_updates_raw = gm_decision.get("room_updates", [])
    if isinstance(room_updates_raw, list):
        updated: list[dict[str, str]] = []
        for ru in room_updates_raw:
            if not isinstance(ru, dict):
                continue
            rid = str(ru.get("room_id", "")).strip()
            if not rid:
                continue
            desc = ru.get("description")
            desc_str = str(desc).strip() if isinstance(desc, str) else None
            conns = ru.get("connections")
            conn_list = [str(c) for c in conns] if isinstance(conns, list) else None
            if store.update_room(bonfire_id, rid, description=desc_str, connections=conn_list):
                updated.append({"room_id": rid})
        result["rooms_updated"] = updated

    movements_raw = gm_decision.get("room_movements", [])
    if isinstance(movements_raw, list):
        game = store.get_game(bonfire_id)
        room_name_to_id: dict[str, str] = {}
        if game:
            for r in game.rooms:
                if isinstance(r, dict):
                    room_name_to_id[str(r.get("name", "")).lower()] = str(r.get("room_id", ""))
                    room_name_to_id[str(r.get("room_id", ""))] = str(r.get("room_id", ""))
        applied: list[dict[str, str]] = []
        for mv in movements_raw:
            if not isinstance(mv, dict):
                continue
            mv_agent = str(mv.get("agent_id", "")).strip()
            mv_room = str(mv.get("to_room", "")).strip()
            if not mv_agent or not mv_room:
                continue
            resolved_room_id = room_name_to_id.get(mv_room.lower(), mv_room)
            if store.move_player(mv_agent, resolved_room_id):
                applied.append({"agent_id": mv_agent, "to_room": resolved_room_id})
        result["movements_applied"] = applied

    return result


def _apply_gm_npc_and_object_changes(
    store: GameStore, bonfire_id: str, gm_decision: dict[str, object],
) -> dict[str, object]:
    """Parse and apply new_npcs, npc_updates, new_objects, and object_grants from GM decision."""
    result: dict[str, object] = {
        "npcs_created": [], "npcs_moved": [], "objects_created": [], "objects_granted": [],
    }

    new_npcs_raw = gm_decision.get("new_npcs", [])
    if isinstance(new_npcs_raw, list):
        created_npcs: list[dict[str, str]] = []
        for entry in new_npcs_raw:
            if not isinstance(entry, dict):
                continue
            name = str(entry.get("name", "")).strip()
            room_id = str(entry.get("room_id", "")).strip()
            personality = str(entry.get("personality", "")).strip()
            if not name or not room_id:
                continue
            description = str(entry.get("description", "")).strip()
            dialogue_style = str(entry.get("dialogue_style", "")).strip()
            try:
                npc = store.create_npc(
                    bonfire_id, name, room_id, personality,
                    description=description, dialogue_style=dialogue_style,
                )
                created_npcs.append({"npc_id": npc.npc_id, "name": npc.name, "room_id": room_id})
            except ValueError:
                pass
        result["npcs_created"] = created_npcs

    npc_updates_raw = gm_decision.get("npc_updates", [])
    if isinstance(npc_updates_raw, list):
        moved_npcs: list[dict[str, str]] = []
        for entry in npc_updates_raw:
            if not isinstance(entry, dict):
                continue
            npc_id = str(entry.get("npc_id", "")).strip()
            room_id = str(entry.get("room_id", "")).strip()
            if not npc_id:
                continue
            if store.update_npc(bonfire_id, npc_id, room_id=room_id or None):
                moved_npcs.append({"npc_id": npc_id, "room_id": room_id})
        result["npcs_moved"] = moved_npcs

    new_objects_raw = gm_decision.get("new_objects", [])
    if isinstance(new_objects_raw, list):
        created_objs: list[dict[str, str]] = []
        for entry in new_objects_raw:
            if not isinstance(entry, dict):
                continue
            name = str(entry.get("name", "")).strip()
            description = str(entry.get("description", "")).strip()
            if not name:
                continue
            obj_type = str(entry.get("obj_type", "artifact")).strip()
            props_raw = entry.get("properties", {})
            props = {str(k): str(v) for k, v in props_raw.items()} if isinstance(props_raw, dict) else {}
            loc_type = str(entry.get("location_type", "room")).strip()
            loc_id = str(entry.get("location_id", "")).strip()
            if loc_type and loc_id:
                props["location_type"] = loc_type
                props["location_id"] = loc_id
            try:
                obj = store.create_object(bonfire_id, name, description, obj_type, props)
                created_objs.append({
                    "object_id": obj.object_id, "name": obj.name, "location_type": loc_type, "location_id": loc_id,
                })
                if loc_type == "npc" and loc_id:
                    store.grant_object_to_npc(bonfire_id, loc_id, obj.object_id)
                elif loc_type == "player" and loc_id:
                    store.grant_object_to_player(bonfire_id, loc_id, obj.object_id)
            except ValueError:
                pass
        result["objects_created"] = created_objs

    grants_raw = gm_decision.get("object_grants", [])
    if isinstance(grants_raw, list):
        granted: list[dict[str, str]] = []
        for entry in grants_raw:
            if not isinstance(entry, dict):
                continue
            object_id = str(entry.get("object_id", "")).strip()
            to_agent = str(entry.get("to_agent_id", "")).strip()
            if not object_id or not to_agent:
                continue
            if store.grant_object_to_player(bonfire_id, to_agent, object_id):
                granted.append({"object_id": object_id, "to_agent_id": to_agent})
        result["objects_granted"] = granted

    return result
