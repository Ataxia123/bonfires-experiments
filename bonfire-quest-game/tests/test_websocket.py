"""WebSocket tests for RoomHub and real-time event delivery."""

from __future__ import annotations

import sys
import tempfile
import time
from pathlib import Path
from typing import Any

import pytest
from starlette.testclient import TestClient

GAME_DIR = Path(__file__).parent.parent
sys.path.insert(0, str(GAME_DIR))

import game_config as config
import http_client
from app import create_app
from game_store import GameStore
from room_hub import RoomHub


# ---------------------------------------------------------------------------
# RoomHub unit tests (async)
# ---------------------------------------------------------------------------


class _FakeWebSocket:
    """Minimal stand-in for starlette WebSocket."""

    def __init__(self) -> None:
        from starlette.websockets import WebSocketState

        self.sent: list[dict[str, Any]] = []
        self.client_state = WebSocketState.CONNECTED
        self.closed = False

    async def send_json(self, data: dict[str, Any]) -> None:
        self.sent.append(data)

    async def close(self) -> None:
        from starlette.websockets import WebSocketState

        self.client_state = WebSocketState.DISCONNECTED
        self.closed = True


@pytest.mark.asyncio
async def test_hub_connect_subscribe_broadcast() -> None:
    hub = RoomHub()
    ws1 = _FakeWebSocket()
    ws2 = _FakeWebSocket()

    await hub.connect("agent-a", ws1)  # type: ignore[arg-type]
    await hub.connect("agent-b", ws2)  # type: ignore[arg-type]
    await hub.subscribe("agent-a", "room-1")
    await hub.subscribe("agent-b", "room-1")

    await hub.broadcast_to_room("room-1", {"type": "test", "msg": "hello"})

    assert len(ws1.sent) == 1
    assert ws1.sent[0]["type"] == "test"
    assert len(ws2.sent) == 1


@pytest.mark.asyncio
async def test_hub_disconnect_removes_subscription() -> None:
    hub = RoomHub()
    ws = _FakeWebSocket()

    await hub.connect("agent-x", ws)  # type: ignore[arg-type]
    await hub.subscribe("agent-x", "room-2")
    await hub.disconnect("agent-x")

    await hub.broadcast_to_room("room-2", {"type": "ping"})
    assert len(ws.sent) == 0


@pytest.mark.asyncio
async def test_hub_room_switch() -> None:
    hub = RoomHub()
    ws = _FakeWebSocket()

    await hub.connect("agent-s", ws)  # type: ignore[arg-type]
    await hub.subscribe("agent-s", "room-old")
    await hub.subscribe("agent-s", "room-new")

    await hub.broadcast_to_room("room-old", {"type": "old"})
    await hub.broadcast_to_room("room-new", {"type": "new"})

    assert len(ws.sent) == 1
    assert ws.sent[0]["type"] == "new"


@pytest.mark.asyncio
async def test_hub_stale_connection_cleanup() -> None:
    hub = RoomHub()
    ws = _FakeWebSocket()
    await ws.close()

    await hub.connect("agent-stale", ws)  # type: ignore[arg-type]
    await hub.subscribe("agent-stale", "room-3")
    await hub.broadcast_to_room("room-3", {"type": "ping"})

    assert len(ws.sent) == 0


@pytest.mark.asyncio
async def test_hub_send_to_player() -> None:
    hub = RoomHub()
    ws = _FakeWebSocket()

    await hub.connect("agent-dm", ws)  # type: ignore[arg-type]
    await hub.send_to_player("agent-dm", {"type": "direct", "msg": "hi"})

    assert len(ws.sent) == 1
    assert ws.sent[0]["type"] == "direct"


@pytest.mark.asyncio
async def test_hub_broadcast_all() -> None:
    hub = RoomHub()
    ws1 = _FakeWebSocket()
    ws2 = _FakeWebSocket()

    await hub.connect("a1", ws1)  # type: ignore[arg-type]
    await hub.connect("a2", ws2)  # type: ignore[arg-type]
    await hub.subscribe("a1", "room-x")
    await hub.subscribe("a2", "room-y")

    await hub.broadcast_all({"type": "global"})

    assert len(ws1.sent) == 1
    assert len(ws2.sent) == 1


# ---------------------------------------------------------------------------
# GameStore event callback tests
# ---------------------------------------------------------------------------


def test_store_emits_room_chat_event() -> None:
    events: list[tuple[str, dict[str, Any]]] = []

    def capture(room_id: str, event: dict[str, Any]) -> None:
        events.append((room_id, event))

    store_path = Path(tempfile.gettempdir()) / f"ws-test-{time.time_ns()}.json"
    store = GameStore(storage_path=store_path, on_room_event=capture)

    store.append_room_message("r1", "agent-1", "0xwallet", "user", "hello")

    assert len(events) == 1
    room_id, ev = events[0]
    assert room_id == "r1"
    assert ev["type"] == "room_chat"
    assert ev["text"] == "hello"


def test_store_emits_player_moved_events() -> None:
    events: list[tuple[str, dict[str, Any]]] = []

    def capture(room_id: str, event: dict[str, Any]) -> None:
        events.append((room_id, event))

    store_path = Path(tempfile.gettempdir()) / f"ws-test-{time.time_ns()}.json"
    store = GameStore(storage_path=store_path, on_room_event=capture)

    store.create_or_replace_game("bf1", "0xowner", "test", None, "summary")
    game = store.get_game("bf1")
    assert game is not None
    game.rooms = [
        {"room_id": "r1", "name": "Room 1", "description": "", "connections": []},
        {"room_id": "r2", "name": "Room 2", "description": "", "connections": []},
    ]

    store.register_agent("0xwallet", "agent-1", "bf1", 7, 5)
    store.move_player("agent-1", "r1")
    events.clear()

    store.move_player("agent-1", "r2")

    assert len(events) == 2
    assert events[0][0] == "r1"
    assert events[0][1]["type"] == "player_left"
    assert events[1][0] == "r2"
    assert events[1][1]["type"] == "player_joined"


def test_store_emits_room_image_updated() -> None:
    events: list[tuple[str, dict[str, Any]]] = []

    def capture(room_id: str, event: dict[str, Any]) -> None:
        events.append((room_id, event))

    store_path = Path(tempfile.gettempdir()) / f"ws-test-{time.time_ns()}.json"
    store = GameStore(storage_path=store_path, on_room_event=capture)

    store.create_or_replace_game("bf1", "0xowner", "test", None, "summary")
    game = store.get_game("bf1")
    assert game is not None
    game.rooms = [{"room_id": "r1", "name": "Room 1", "description": "", "connections": []}]

    events.clear()
    store.update_room_image("bf1", "r1", "http://img.png", "A dark cave", "hb-1")

    assert len(events) == 1
    assert events[0][1]["type"] == "room_image_updated"
    assert events[0][1]["image_url"] == "http://img.png"


def test_store_emits_world_state_events() -> None:
    events: list[tuple[str, dict[str, Any]]] = []

    def capture(room_id: str, event: dict[str, Any]) -> None:
        events.append((room_id, event))

    store_path = Path(tempfile.gettempdir()) / f"ws-test-{time.time_ns()}.json"
    store = GameStore(storage_path=store_path, on_room_event=capture)

    store.create_or_replace_game("bf1", "0xowner", "test", None, "summary")
    game = store.get_game("bf1")
    assert game is not None
    game.rooms = [
        {"room_id": "r1", "name": "Room 1", "description": "", "connections": []},
        {"room_id": "r2", "name": "Room 2", "description": "", "connections": []},
    ]
    events.clear()

    store.update_game_world_state("bf1", "ep-1", "The world evolves.", "Great progress!")

    assert len(events) == 2
    assert all(e[1]["type"] == "world_state" for e in events)
    assert {e[0] for e in events} == {"r1", "r2"}


def test_store_emits_room_created() -> None:
    events: list[tuple[str, dict[str, Any]]] = []

    def capture(room_id: str, event: dict[str, Any]) -> None:
        events.append((room_id, event))

    store_path = Path(tempfile.gettempdir()) / f"ws-test-{time.time_ns()}.json"
    store = GameStore(storage_path=store_path, on_room_event=capture)

    store.create_or_replace_game("bf1", "0xowner", "test", None, "summary")
    events.clear()

    store.create_room("bf1", "New Chamber", "A newly discovered room")

    room_created_events = [(rid, ev) for rid, ev in events if ev.get("type") == "room_created"]
    assert len(room_created_events) == 1
    assert room_created_events[0][1]["room"]["name"] == "New Chamber"


# ---------------------------------------------------------------------------
# Integration: WebSocket endpoint via TestClient
# ---------------------------------------------------------------------------


def _make_test_app(monkeypatch: pytest.MonkeyPatch) -> tuple[TestClient, GameStore, RoomHub]:
    """Create a TestClient with mocked HTTP dependencies."""

    def fake_json_request(method: str, url: str, body: dict[str, object] | None = None):
        return 200, {"status": "ok"}

    def fake_agent_json_request(method: str, url: str, api_key: str, body=None):
        if url.endswith("/chat"):
            return 200, {"reply": "narrator says hello"}
        if url.endswith("/stack/add"):
            return 200, {"success": True, "message_count": 2}
        return 200, {"status": "ok"}

    monkeypatch.setattr(http_client, "_json_request", fake_json_request)
    monkeypatch.setattr(http_client, "_agent_json_request", fake_agent_json_request)
    monkeypatch.setattr(config, "DELVE_API_KEY", "test-key")

    store_path = Path(tempfile.gettempdir()) / f"ws-int-test-{time.time_ns()}.json"
    store = GameStore(storage_path=store_path)
    hub = RoomHub()
    app = create_app(store=store, resolve_owner_wallet=lambda _: "0xowner", room_hub=hub)
    client = TestClient(app, raise_server_exceptions=False)
    return client, store, hub


def test_ws_rejects_without_params(monkeypatch: pytest.MonkeyPatch) -> None:
    client, _, _ = _make_test_app(monkeypatch)
    with pytest.raises(Exception):
        with client.websocket_connect("/ws/game"):
            pass


def test_ws_rejects_unregistered_agent(monkeypatch: pytest.MonkeyPatch) -> None:
    client, _, _ = _make_test_app(monkeypatch)
    with pytest.raises(Exception):
        with client.websocket_connect("/ws/game?agent_id=unknown&api_key=key"):
            pass


def test_ws_accepts_registered_agent(monkeypatch: pytest.MonkeyPatch) -> None:
    client, store, _ = _make_test_app(monkeypatch)

    client.post(
        "/game/bonfire/link",
        json={"bonfire_id": "bf1", "erc8004_bonfire_id": 7, "wallet_address": "0xowner"},
    )
    store.register_agent("0xplayer", "agent-ws", "bf1", 7, 5)

    with client.websocket_connect("/ws/game?agent_id=agent-ws&api_key=test-key") as ws:
        ws.send_text("ping")
        resp = ws.receive_text()
        assert resp == "pong"


def test_ws_receives_chat_event_after_room_message(monkeypatch: pytest.MonkeyPatch) -> None:
    client, store, _ = _make_test_app(monkeypatch)

    client.post(
        "/game/bonfire/link",
        json={"bonfire_id": "bf1", "erc8004_bonfire_id": 7, "wallet_address": "0xowner"},
    )
    store.register_agent("0xplayer", "agent-ws2", "bf1", 7, 5)
    store.create_or_replace_game("bf1", "0xowner", "test game", None, "summary")
    room_id = store.ensure_starting_room("bf1")
    store.move_player("agent-ws2", room_id)

    with client.websocket_connect(f"/ws/game?agent_id=agent-ws2&api_key=test-key") as ws:
        ws.send_text("ping")
        pong = ws.receive_text()
        assert pong == "pong"

        time.sleep(0.1)
        store.append_room_message(room_id, "agent-other", "0xother", "user", "hello from another player")
        time.sleep(0.2)

        event = ws.receive_json(mode="text")
        assert event["type"] == "room_chat"
        assert event["text"] == "hello from another player"
