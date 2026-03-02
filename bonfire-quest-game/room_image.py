"""Room ↔ DataRoom bridge: creates DataRooms, generates HyperBlogs, pushes summaries to GM stack."""

from __future__ import annotations

import time
from datetime import UTC, datetime

import game_config as config
import http_client
from game_store import GameStore

_POLL_INTERVAL = 5.0
_POLL_MAX_WAIT = 180.0


def create_room_dataroom(store: GameStore, bonfire_id: str, room_id: str) -> str:
    """Create a Delve DataRoom for a game room and persist the mapping.

    Returns the dataroom_id or empty string on failure.
    """
    room = store.get_room_by_id(bonfire_id, room_id)
    if not room:
        print(f"[room_image] room {room_id} not found in bonfire {bonfire_id}")
        return ""

    game = store.get_game(bonfire_id)
    game_prompt = game.game_prompt if game else ""

    body: dict[str, object] = {
        "bonfire_id": bonfire_id,
        "description": f"{room.get('name', 'Room')}: {room.get('description', '')}".strip()[:1000],
        "system_prompt": (
            f"You are a narrator for a dark fantasy RPG adventure. "
            f"The game premise is: {game_prompt[:500]}. "
            f"Describe this location and what happens here in vivid, atmospheric prose."
        )[:2000],
        "price_usd": 0.01,
    }
    if config.ROOM_HTN_TEMPLATE_ID:
        body["htn_template_id"] = config.ROOM_HTN_TEMPLATE_ID

    url = f"{config.DELVE_BASE_URL}/datarooms"
    status, payload = http_client._json_request("POST", url, body)
    if status not in (200, 201) or not isinstance(payload, dict):
        print(f"[room_image] dataroom creation failed ({status}): {payload}")
        return ""

    dataroom_id = str(payload.get("id") or payload.get("_id") or "")
    if dataroom_id:
        store.update_room_dataroom(bonfire_id, room_id, dataroom_id)
        print(f"[room_image] DataRoom {dataroom_id} created for room {room_id}")
    return dataroom_id


def generate_room_hyperblog(
    store: GameStore,
    bonfire_id: str,
    room_id: str,
    user_query: str = "Describe the current state of this location.",
) -> str:
    """Trigger a HyperBlog generation for a room's DataRoom.

    Returns the hyperblog_id or empty string on failure.
    """
    room = store.get_room_by_id(bonfire_id, room_id)
    if not room:
        return ""
    dataroom_id = str(room.get("dataroom_id", ""))
    if not dataroom_id:
        dataroom_id = create_room_dataroom(store, bonfire_id, room_id)
        if not dataroom_id:
            return ""

    url = f"{config.DELVE_BASE_URL}/datarooms/{dataroom_id}/hyperblogs/generate"
    body: dict[str, object] = {
        "bonfire_id": bonfire_id,
        "user_query": user_query,
        "blog_length": "short",
        "generation_mode": "card",
        "author_wallet": "game-server",
    }
    status, payload = http_client._json_request("POST", url, body)
    if status not in (200, 201) or not isinstance(payload, dict):
        print(f"[room_image] hyperblog generation failed ({status}): {payload}")
        return ""

    hb_id = str(payload.get("hyperblog_id") or "")
    print(f"[room_image] HyperBlog {hb_id} queued for room {room_id}")
    return hb_id


def poll_and_update_room_image(
    store: GameStore, bonfire_id: str, room_id: str, hyperblog_id: str
) -> bool:
    """Poll until HyperBlog completes, then update room image + push to GM stack.

    Returns True if image was updated successfully.
    """
    if not hyperblog_id:
        return False

    elapsed = 0.0
    poll_url = f"{config.DELVE_BASE_URL}/datarooms/hyperblogs/{hyperblog_id}"

    while elapsed < _POLL_MAX_WAIT:
        time.sleep(_POLL_INTERVAL)
        elapsed += _POLL_INTERVAL

        status, payload = http_client._json_request("GET", poll_url)
        if status != 200 or not isinstance(payload, dict):
            print(f"[room_image] poll error ({status}) for {hyperblog_id}")
            continue

        hb_data = payload.get("hyperblog") if isinstance(payload.get("hyperblog"), dict) else payload
        gen_status = str(hb_data.get("generation_status", ""))

        if gen_status == "completed":
            banner_url = str(hb_data.get("banner_url") or "")
            summary = str(hb_data.get("summary") or hb_data.get("preview") or "")

            if not banner_url:
                banner_url = _trigger_banner_generation(hyperblog_id)

            store.update_room_image(bonfire_id, room_id, banner_url, summary, hyperblog_id)
            print(f"[room_image] room {room_id} updated: image={banner_url[:60]}...")

            room = store.get_room_by_id(bonfire_id, room_id)
            room_name = str(room.get("name", "")) if room else ""
            if summary:
                push_hyperblog_to_stack(store, bonfire_id, room_id, room_name, summary)
            return True

        if gen_status == "failed":
            print(f"[room_image] HyperBlog {hyperblog_id} generation failed")
            return False

        print(f"[room_image] poll {elapsed:.0f}s — status={gen_status}")

    print(f"[room_image] poll timed out for {hyperblog_id}")
    return False


def _trigger_banner_generation(hyperblog_id: str) -> str:
    """Call the banner generation endpoint and return the URL."""
    url = f"{config.DELVE_BASE_URL}/datarooms/hyperblogs/{hyperblog_id}/banner"
    status, payload = http_client._json_request("POST", url, {"bonfire_id": ""})
    if status == 200 and isinstance(payload, dict) and payload.get("success"):
        return str(payload.get("banner_url") or "")
    print(f"[room_image] banner generation failed ({status}): {payload}")
    return ""


def push_hyperblog_to_stack(
    store: GameStore,
    bonfire_id: str,
    room_id: str,
    room_name: str,
    summary: str,
) -> bool:
    """Push HyperBlog summary to the GM agent's stack so it enters the KG."""
    gm_agent_id = store.get_owner_agent_id(bonfire_id)
    if not gm_agent_id or not config.DELVE_API_KEY:
        print(f"[room_image] no GM agent or API key for bonfire {bonfire_id}")
        return False

    url = f"{config.DELVE_BASE_URL}/agents/{gm_agent_id}/stack/add"
    body: dict[str, object] = {
        "message": {
            "text": f"[Room: {room_name}] {summary}",
            "chatId": f"room-{room_id}",
            "userId": "room-narrator",
            "timestamp": datetime.now(UTC).isoformat(),
        },
    }
    status, payload = http_client._agent_json_request("POST", url, config.DELVE_API_KEY, body)
    if status == 200:
        print(f"[room_image] pushed summary to GM stack for room {room_name}")
        return True
    print(f"[room_image] stack push failed ({status}): {payload}")
    return False


def setup_room(store: GameStore, bonfire_id: str, room_id: str) -> None:
    """Full orchestration: create DataRoom → generate HyperBlog → poll → update."""
    room = store.get_room_by_id(bonfire_id, room_id)
    if not room:
        return

    dataroom_id = str(room.get("dataroom_id", ""))
    if not dataroom_id:
        dataroom_id = create_room_dataroom(store, bonfire_id, room_id)
        if not dataroom_id:
            return

    hb_id = generate_room_hyperblog(store, bonfire_id, room_id)
    if hb_id:
        poll_and_update_room_image(store, bonfire_id, room_id, hb_id)
