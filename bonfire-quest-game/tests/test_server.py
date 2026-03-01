"""API tests for bonfire quest game demo."""

from __future__ import annotations

import json
import sys
import tempfile
import time
from collections.abc import Callable
from pathlib import Path

import pytest
from starlette.testclient import TestClient

GAME_DIR = Path(__file__).parent.parent
sys.path.insert(0, str(GAME_DIR))

import game_config as config
import http_client
import stack_processing
import gm_engine
import timers
import models
from app import create_app
from game_store import GameStore



def _post(
    client: TestClient,
    path: str,
    body: dict[str, object],
    headers: dict[str, str] | None = None,
) -> tuple[int, dict[str, object]]:
    response = client.post(path, json=body, headers=headers or {})
    return response.status_code, response.json()


def _get(client: TestClient, path: str) -> tuple[int, dict[str, object]]:
    response = client.get(path)
    return response.status_code, response.json()


def _start_server(
    resolver: Callable[[int], str],
) -> tuple[TestClient, TestClient, GameStore]:
    store_path = Path(tempfile.gettempdir()) / f"bonfire-quest-game-test-store-{time.time_ns()}.json"
    store = GameStore(storage_path=store_path)
    app = create_app(store=store, resolve_owner_wallet=resolver)
    client = TestClient(app, raise_server_exceptions=False)
    return client, client, store


@pytest.fixture()
def live_server(monkeypatch: pytest.MonkeyPatch):
    def fake_json_request(method: str, url: str, body: dict[str, object] | None = None):
        if "/reveal_nonce" in url:
            return 200, {"nonce": "abc", "message": "sign me"}
        if "/reveal_api_key" in url:
            return 200, {"api_key": "agent-key-123"}
        if "/purchase-agent" in url:
            return 200, {"agent_id": "agent-upstream", "purchase_id": "purchase-upstream"}
        if "/bonfires/" in url and url.endswith("/pricing"):
            return 200, {"price_per_episode": "0.01", "max_episodes_per_agent": 20, "max_agents": 10}
        if "/bonfires/" in url and url.endswith("/agents"):
            return 200, {
                "bonfire_id": "bf1",
                "agents": [
                    {"id": "agent-1", "name": "Owner Agent", "username": "owner"},
                    {"id": "agent-3", "name": "Second Agent", "username": "second"},
                ],
                "total_agents": 2,
                "active_agents": 2,
            }
        if "/provision?wallet_address=" in url:
            return 200, {
                "records": [
                    {
                        "bonfire_id": "bf1",
                        "erc8004_bonfire_id": 7,
                        "agent_id": "agent-1",
                        "agent_name": "Owner Agent",
                    },
                    {
                        "bonfire_id": "bf2",
                        "erc8004_bonfire_id": 8,
                        "agent_id": "agent-2",
                        "agent_name": "Other Agent",
                    },
                ]
            }
        return 404, {"error": "not found"}

    monkeypatch.setattr(http_client, "_json_request", fake_json_request)
    monkeypatch.setattr(config, "DELVE_API_KEY", "server-key")
    def fake_agent_json_request(method: str, url: str, api_key: str, body=None):
        if url.endswith("/chat"):
            return 200, {"reply": f"assistant says hi via {api_key}"}
        if url.endswith("/stack/add"):
            return 200, {"success": True, "message_count": 2}
        if url.endswith("/stack/process"):
            return 200, {
                "success": True,
                "message_count": 2,
                "episode_id": "ep-123",
                "message": "major quest milestone completed",
            }
        return 404, {"error": "not found"}

    monkeypatch.setattr(http_client, "_agent_json_request", fake_agent_json_request)
    client, _, store = _start_server(lambda token_id: "0xowner")
    yield client, store


def _link_bonfire(client: TestClient, wallet: str = "0xowner") -> None:
    status, _ = _post(
        client,
        "/game/bonfire/link",
        {"bonfire_id": "bf1", "erc8004_bonfire_id": 7, "wallet_address": wallet},
    )
    assert status == 200


def _register_purchase(
    client: TestClient,
    agent_id: str = "agent-1",
    episodes: int = 2,
    wallet_address: str = "0xowner",
) -> None:
    status, _ = _post(
        client,
        "/game/agents/register-purchase",
        {
            "wallet_address": wallet_address,
            "agent_id": agent_id,
            "bonfire_id": "bf1",
            "erc8004_bonfire_id": 7,
            "purchase_id": f"purchase-{agent_id}",
            "purchase_tx_hash": f"0xtx-{agent_id}",
            "episodes_purchased": episodes,
        },
    )
    assert status == 200


class TestBonfireLink:
    def test_rejects_when_wallet_not_owner(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(http_client, "_json_request", lambda method, url, body=None: (200, {"nonce": "n", "message": "m"}))
        client, _, _ = _start_server(lambda token_id: "0xactualowner")
        status, data = _post(client,
            "/game/bonfire/link",
            {"bonfire_id": "bf1", "erc8004_bonfire_id": 9, "wallet_address": "0xother"},
        )
        assert status == 403
        assert "owner_wallet" in data


class TestPurchaseIntegration:
    def test_register_purchase_requires_valid_purchase_id(self, live_server) -> None:
        client, _ = live_server
        status, data = _post(client,
            "/game/agents/register-purchase",
            {
                "wallet_address": "0xowner",
                "agent_id": "agent-a",
                "bonfire_id": "bf1",
                "erc8004_bonfire_id": 7,
                "purchase_id": "purchase-a",
                "purchase_tx_hash": "0xtx",
                "episodes_purchased": 3,
            },
        )
        assert status == 200
        assert data["remaining_episodes"] == 3

    def test_register_purchase_rejects_when_upstream_missing(
        self,
        live_server,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        client, _ = live_server

        monkeypatch.setattr(
            http_client,
            "_json_request",
            lambda method, url, body=None: (404, {"detail": "Purchase record not found"}),
        )

        status, data = _post(client,
            "/game/agents/register-purchase",
            {
                "wallet_address": "0xowner",
                "agent_id": "agent-x",
                "bonfire_id": "bf1",
                "erc8004_bonfire_id": 7,
                "purchase_id": "purchase-x",
                "purchase_tx_hash": "0xtx-x",
                "episodes_purchased": 2,
            },
        )
        assert status == 400
        assert data.get("error") == "invalid_purchase_id"

    def test_register_selected_agent_works_without_purchase_lookup(
        self,
        live_server,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        client, _ = live_server

        monkeypatch.setattr(
            http_client,
            "_json_request",
            lambda method, url, body=None: (404, {"detail": "Purchase record not found"}),
        )

        status, data = _post(client,
            "/game/agents/register-selected",
            {
                "wallet_address": "0xowner",
                "bonfire_id": "bf1",
                "erc8004_bonfire_id": 7,
                "agent_id": "agent-y",
                "episodes_purchased": 2,
            },
        )
        assert status == 200
        assert data.get("agent_id") == "agent-y"

    def test_purchase_proxy_returns_upstream_payload(self, live_server) -> None:
        client, _ = live_server
        status, data = _post(client,
            "/game/purchase-agent/bf1",
            {
                "payment_header": "x402",
                "platform": "web",
                "episodes_requested": 2,
                "agent_name": "A",
                "agent_context": "ctx",
            },
        )
        assert status == 200
        assert data["purchase_id"] == "purchase-upstream"

    def test_reveal_api_key_proxy_roundtrip(self, live_server) -> None:
        client, _ = live_server
        status_nonce, data_nonce = _post(client,
            "/game/purchased-agents/reveal-nonce",
            {"purchase_id": "purchase-upstream"},
        )
        assert status_nonce == 200
        assert data_nonce.get("nonce") == "abc"

        status_reveal, data_reveal = _post(client,
            "/game/purchased-agents/reveal-api-key",
            {"purchase_id": "purchase-upstream", "nonce": "abc", "signature": "0xsig"},
        )
        assert status_reveal == 200
        assert data_reveal.get("api_key") == "agent-key-123"

    def test_reveal_api_key_selected_agent_roundtrip(self, live_server) -> None:
        client, _ = live_server
        _link_bonfire(client)
        _register_purchase(client, agent_id="agent-1", wallet_address="0xowner")

        status_nonce, data_nonce = _post(client,
            "/game/agents/reveal-nonce-selected",
            {
                "wallet_address": "0xowner",
                "bonfire_id": "bf1",
                "agent_id": "agent-1",
            },
        )
        assert status_nonce == 200
        assert data_nonce.get("nonce") == "abc"
        assert data_nonce.get("purchase_id") == "purchase-agent-1"

        status_reveal, data_reveal = _post(client,
            "/game/agents/reveal-api-key-selected",
            {
                "wallet_address": "0xowner",
                "bonfire_id": "bf1",
                "agent_id": "agent-1",
                "nonce": "abc",
                "signature": "0xsig",
            },
        )
        assert status_reveal == 200
        assert data_reveal.get("api_key") == "agent-key-123"
        assert data_reveal.get("purchase_id") == "purchase-agent-1"

    def test_reveal_selected_agent_fails_when_purchase_id_unavailable(
        self,
        live_server,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        client, _ = live_server
        monkeypatch.setattr(http_client, "_json_request", lambda method, url, body=None: (404, {"error": "not found"}))
        status_register, _ = _post(client,
            "/game/agents/register-selected",
            {
                "wallet_address": "0xowner",
                "bonfire_id": "bf1",
                "erc8004_bonfire_id": 7,
                "agent_id": "agent-3",
                "episodes_purchased": 2,
            },
        )
        assert status_register == 200

        status_nonce, data_nonce = _post(client,
            "/game/agents/reveal-nonce-selected",
            {
                "wallet_address": "0xowner",
                "bonfire_id": "bf1",
                "agent_id": "agent-3",
            },
        )
        assert status_nonce == 404
        assert data_nonce.get("error") == "purchase_id_not_found_for_selected_agent"

    def test_reveal_selected_agent_falls_back_to_purchase_tx_hash(
        self,
        live_server,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        client, _ = live_server

        def fake_json_request(method: str, url: str, body: dict[str, object] | None = None):
            if url.endswith("/agents/agent-3"):
                return 200, {
                    "id": "agent-3",
                    "bonfire_id": "bf1",
                    "purchaseTxHash": "0xtx-agent-3",
                }
            if "/purchased-agents/" in url and "/reveal_nonce" in url:
                return 404, {"detail": "Purchase record not found"}
            if "/provision/reveal_nonce" in url:
                return 200, {"nonce": "abc", "message": "sign me"}
            if url.endswith("/provision/reveal_api_key"):
                tx_hash = (body or {}).get("tx_hash")
                if tx_hash == "0xtx-agent-3":
                    return 200, {"api_key": "agent-key-123"}
                return 404, {"detail": "Provision record not found"}
            if "/provision?wallet_address=" in url:
                return 200, {"records": []}
            if "/bonfires/" in url and url.endswith("/agents"):
                return 200, {"bonfire_id": "bf1", "agents": [{"id": "agent-3", "name": "Second Agent"}]}
            return 404, {"error": "not found"}

        monkeypatch.setattr(http_client, "_json_request", fake_json_request)

        status_register, _ = _post(client,
            "/game/agents/register-selected",
            {
                "wallet_address": "0xowner",
                "bonfire_id": "bf1",
                "erc8004_bonfire_id": 7,
                "agent_id": "agent-3",
                "episodes_purchased": 2,
            },
        )
        assert status_register == 200

        status_nonce, data_nonce = _post(client,
            "/game/agents/reveal-nonce-selected",
            {
                "wallet_address": "0xowner",
                "bonfire_id": "bf1",
                "agent_id": "agent-3",
            },
        )
        assert status_nonce == 200
        assert data_nonce.get("purchase_tx_hash") == "0xtx-agent-3"

        status_reveal, data_reveal = _post(client,
            "/game/agents/reveal-api-key-selected",
            {
                "wallet_address": "0xowner",
                "bonfire_id": "bf1",
                "agent_id": "agent-3",
                "purchase_tx_hash": "0xtx-agent-3",
                "nonce": "abc",
                "signature": "0xsig",
            },
        )
        assert status_reveal == 200
        assert data_reveal.get("api_key") == "agent-key-123"
        assert data_reveal.get("purchase_tx_hash") == "0xtx-agent-3"

    def test_reveal_selected_agent_uses_public_agent_list_when_agent_route_forbidden(
        self,
        live_server,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        client, _ = live_server

        def fake_json_request(method: str, url: str, body: dict[str, object] | None = None):
            if "/agents?bonfire_id=bf1" in url:
                return 200, {
                    "agents": [
                        {
                            "id": "agent-3",
                            "name": "Second Agent",
                            "purchaseTxHash": "0xtx-agent-3",
                        }
                    ]
                }
            if url.endswith("/agents/agent-3"):
                return 403, {"detail": "forbidden"}
            if "/purchased-agents/" in url and "/reveal_nonce" in url:
                return 404, {"detail": "Purchase record not found"}
            if "/provision/reveal_nonce" in url:
                return 200, {"nonce": "abc", "message": "sign me"}
            if url.endswith("/provision/reveal_api_key"):
                tx_hash = (body or {}).get("tx_hash")
                if tx_hash == "0xtx-agent-3":
                    return 200, {"api_key": "agent-key-123"}
                return 404, {"detail": "Provision record not found"}
            if "/provision?wallet_address=" in url:
                return 200, {"records": []}
            if "/bonfires/" in url and url.endswith("/agents"):
                return 200, {"bonfire_id": "bf1", "agents": [{"id": "agent-3", "name": "Second Agent"}]}
            return 404, {"error": "not found"}

        monkeypatch.setattr(http_client, "_json_request", fake_json_request)

        status_register, _ = _post(client,
            "/game/agents/register-selected",
            {
                "wallet_address": "0xowner",
                "bonfire_id": "bf1",
                "erc8004_bonfire_id": 7,
                "agent_id": "agent-3",
                "episodes_purchased": 2,
            },
        )
        assert status_register == 200

        status_nonce, data_nonce = _post(client,
            "/game/agents/reveal-nonce-selected",
            {
                "wallet_address": "0xowner",
                "bonfire_id": "bf1",
                "agent_id": "agent-3",
            },
        )
        assert status_nonce == 200
        assert data_nonce.get("purchase_tx_hash") == "0xtx-agent-3"


class TestStorePersistence:
    def test_store_persists_and_loads_from_json(self, tmp_path: Path) -> None:
        store_path = tmp_path / "game-store.json"
        store_cls = GameStore
        store = store_cls(storage_path=store_path)
        store.link_bonfire("bf1", 7, "0xowner")
        store.register_agent(
            wallet="0xowner",
            agent_id="agent-1",
            bonfire_id="bf1",
            erc8004_bonfire_id=7,
            episodes_purchased=3,
        )
        store.create_or_replace_game(
            bonfire_id="bf1",
            owner_wallet="0xowner",
            game_prompt="test campaign",
            gm_agent_id="agent-1",
            initial_episode_summary="opening scene",
        )
        store.run_turn("agent-1", "Explore the cave.")

        reloaded = store_cls(storage_path=store_path)
        player = reloaded.get_player("agent-1")
        assert player is not None
        assert player.turns_used == 1
        assert player.remaining_episodes == 2

        game = reloaded.get_game("bf1")
        assert game is not None
        assert game.game_prompt == "test campaign"

        state = reloaded.get_state("bf1")
        players = state.get("players")
        assert isinstance(players, list)
        assert len(players) == 1

    def test_register_selected_agent_without_manual_purchase_fields(self, live_server) -> None:
        client, _ = live_server
        status, data = _post(client,
            "/game/agents/register-selected",
            {
                "wallet_address": "0xowner",
                "bonfire_id": "bf1",
                "erc8004_bonfire_id": 7,
                "agent_id": "agent-3",
                "episodes_purchased": 2,
            },
        )
        assert status == 200
        assert data.get("agent_id") == "agent-3"
        assert "purchase_id" not in data
        assert "purchase_tx_hash" not in data


class TestQuotaAndTurns:
    def test_turn_denied_when_quota_exhausted(self, live_server) -> None:
        client, _ = live_server
        _link_bonfire(client)
        _register_purchase(client, episodes=1)

        status1, _ = _post(client, "/game/turn", {"agent_id": "agent-1", "action": "first"})
        assert status1 == 200
        status2, data2 = _post(client, "/game/turn", {"agent_id": "agent-1", "action": "second"})
        assert status2 == 429
        assert data2["error"] == "episode_quota_exhausted"


class TestQuests:
    def test_owner_actions_work_without_explicit_link_call(self, live_server) -> None:
        client, _ = live_server
        _register_purchase(client, agent_id="owner-agent", episodes=2)
        status, _ = _post(client,
            "/game/quests/create",
            {
                "bonfire_id": "bf1",
                "wallet_address": "0xowner",
                "quest_type": "collect",
                "prompt": "Bring artifact",
                "keyword": "artifact",
                "reward": 2,
            },
        )
        assert status == 200

    def test_quest_create_owner_only(self, live_server) -> None:
        client, _ = live_server
        _link_bonfire(client)
        status, _ = _post(client,
            "/game/quests/create",
            {
                "bonfire_id": "bf1",
                "wallet_address": "0xowner",
                "quest_type": "collect",
                "prompt": "Bring artifact",
                "keyword": "artifact",
                "reward": 2,
            },
        )
        assert status == 200

        status_bad, _ = _post(client,
            "/game/quests/create",
            {
                "bonfire_id": "bf1",
                "wallet_address": "0xnotowner",
                "quest_type": "collect",
                "prompt": "Bring artifact",
                "keyword": "artifact",
                "reward": 2,
            },
        )
        assert status_bad == 403

    def test_claim_is_idempotent_for_same_agent(self, live_server) -> None:
        client, _ = live_server
        _link_bonfire(client)
        _register_purchase(client, episodes=1)
        status_q, data_q = _post(client,
            "/game/quests/create",
            {
                "bonfire_id": "bf1",
                "wallet_address": "0xowner",
                "quest_type": "collect",
                "prompt": "Bring artifact",
                "keyword": "artifact",
                "reward": 2,
            },
        )
        assert status_q == 200
        quest_id = str(data_q["quest_id"])

        status1, data1 = _post(client,
            "/game/quests/claim",
            {"quest_id": quest_id, "agent_id": "agent-1", "submission": "I found an artifact in the feed"},
        )
        assert status1 == 200
        assert data1["reward_granted"] == 2

        status2, _ = _post(client,
            "/game/quests/claim",
            {"quest_id": quest_id, "agent_id": "agent-1", "submission": "artifact again"},
        )
        assert status2 == 403

    def test_recharge_reactivates_exhausted_agent(self, live_server) -> None:
        client, _ = live_server
        _link_bonfire(client)
        _register_purchase(client, episodes=1)

        status1, _ = _post(client, "/game/turn", {"agent_id": "agent-1", "action": "burn"})
        assert status1 == 200
        status2, _ = _post(client, "/game/turn", {"agent_id": "agent-1", "action": "burn2"})
        assert status2 == 429

        status3, data3 = _post(client,
            "/game/agents/recharge",
            {
                "bonfire_id": "bf1",
                "wallet_address": "0xowner",
                "agent_id": "agent-1",
                "amount": 3,
                "reason": "quest_reward",
            },
        )
        assert status3 == 200
        is_active = data3.get("is_active")
        remaining = data3.get("remaining_episodes")
        assert is_active is True
        assert isinstance(remaining, int)
        assert remaining >= 3


class TestFeedAndState:
    def test_state_and_feed_return_registered_agent(self, live_server) -> None:
        client, _ = live_server
        _link_bonfire(client)
        _register_purchase(client, episodes=2)

        status_state, data_state = _get(client, "/game/state?bonfire_id=bf1")
        assert status_state == 200
        players = data_state.get("players")
        assert isinstance(players, list)
        assert len(players) == 1
        first_player = players[0]
        assert isinstance(first_player, dict)
        assert first_player.get("agent_id") == "agent-1"

        status_feed, data_feed = _get(client, "/game/feed?bonfire_id=bf1&limit=10")
        assert status_feed == 200
        assert isinstance(data_feed["events"], list)


class TestGameCreationFlow:
    def test_create_game_and_list_active(self, live_server) -> None:
        client, _ = live_server
        status_create, data_create = _post(client,
            "/game/create",
            {
                "bonfire_id": "bf1",
                "erc8004_bonfire_id": 7,
                "wallet_address": "0xowner",
                "game_prompt": "Explore the artifact maze and report discoveries",
                "initial_quest_count": 2,
            },
        )
        assert status_create == 200
        assert data_create.get("bonfire_id") == "bf1"
        quests = data_create.get("initial_quests")
        assert isinstance(quests, list)
        assert len(quests) >= 1

        status_list, data_list = _get(client, "/game/list-active")
        assert status_list == 200
        games = data_list.get("games")
        assert isinstance(games, list)
        assert len(games) == 1
        assert games[0].get("bonfire_id") == "bf1"

    def test_create_game_replaces_existing_active_game(self, live_server) -> None:
        client, _ = live_server
        status_first, data_first = _post(client,
            "/game/create",
            {
                "bonfire_id": "bf1",
                "erc8004_bonfire_id": 7,
                "wallet_address": "0xowner",
                "game_prompt": "First world",
                "initial_quest_count": 1,
            },
        )
        assert status_first == 200
        first_id = data_first.get("game_id")

        status_second, data_second = _post(client,
            "/game/create",
            {
                "bonfire_id": "bf1",
                "erc8004_bonfire_id": 7,
                "wallet_address": "0xowner",
                "game_prompt": "Second world",
                "initial_quest_count": 1,
            },
        )
        assert status_second == 200
        second_id = data_second.get("game_id")
        assert first_id != second_id

        status_list, data_list = _get(client, "/game/list-active")
        assert status_list == 200
        games = data_list.get("games")
        assert isinstance(games, list)
        assert len(games) == 1
        assert games[0].get("game_id") == second_id

    def test_game_details_returns_state_and_events(self, live_server) -> None:
        client, _ = live_server
        _post(client,
            "/game/create",
            {
                "bonfire_id": "bf1",
                "erc8004_bonfire_id": 7,
                "wallet_address": "0xowner",
                "game_prompt": "Details world",
                "initial_quest_count": 1,
            },
        )
        _register_purchase(client, agent_id="agent-details", episodes=2, wallet_address="0xowner")
        status, data = _get(client, "/game/details?bonfire_id=bf1")
        assert status == 200
        game = data.get("game")
        assert isinstance(game, dict)
        assert game.get("bonfire_id") == "bf1"
        state = data.get("state")
        assert isinstance(state, dict)
        players = state.get("players")
        assert isinstance(players, list)
        assert len(players) >= 1
        events = data.get("events")
        assert isinstance(events, list)


class TestWalletBonfireDiscovery:
    def test_wallet_bonfire_lookup_returns_owned_records(self, live_server) -> None:
        client, _ = live_server
        status, data = _get(client, "/game/wallet/bonfires?wallet_address=0xowner")
        assert status == 200
        bonfires = data.get("bonfires")
        assert isinstance(bonfires, list)
        assert len(bonfires) == 2
        assert bonfires[0]["owner_wallet"] == "0xowner"

    def test_wallet_bonfire_lookup_requires_wallet(self, live_server) -> None:
        client, _ = live_server
        status, _ = _get(client, "/game/wallet/bonfires")
        assert status == 422

    def test_wallet_provision_records_endpoint(self, live_server) -> None:
        client, _ = live_server
        status, data = _get(client, "/game/wallet/provision-records?wallet_address=0xowner")
        assert status == 200
        records = data.get("records")
        assert isinstance(records, list)
        assert len(records) == 2

    def test_wallet_purchased_agents_by_bonfire(self, live_server) -> None:
        client, _ = live_server
        status, data = _get(client,
            "/game/wallet/purchased-agents?wallet_address=0xowner&bonfire_id=bf1",
        )
        assert status == 200
        agents = data.get("agents")
        assert isinstance(agents, list)
        assert len(agents) >= 2
        ids = {str(a.get("agent_id")) for a in agents if isinstance(a, dict)}
        assert "agent-1" in ids
        assert "agent-3" in ids

    def test_wallet_purchased_agents_requires_bonfire(self, live_server) -> None:
        client, _ = live_server
        status, _ = _get(client,
            "/game/wallet/purchased-agents?wallet_address=0xowner",
        )
        assert status == 422

    def test_game_config_endpoint_returns_registry(self, live_server) -> None:
        client, _ = live_server
        status, data = _get(client, "/game/config")
        assert status == 200
        assert "erc8004_registry_address" in data


class TestAgentCompletionFlow:
    def test_completion_adds_to_stack_without_updating_context(self, live_server) -> None:
        client, _ = live_server
        _link_bonfire(client)
        _register_purchase(client, agent_id="agent-x", episodes=3)

        status, data = _post(client,
            "/game/agents/complete",
            {
                "agent_id": "agent-x",
                "message": "I need help with this quest artifact",
                "chat_id": "chat-1",
                "user_id": "player-1",
            },
        )
        assert status == 200
        assert "game_master_context" not in data
        assert data.get("api_key_source") == "server"

        state_status, state = _get(client, "/game/state?bonfire_id=bf1")
        assert state_status == 200
        contexts = state.get("agent_context")
        assert isinstance(contexts, list)
        assert len(contexts) == 0

    def test_process_stack_endpoint_updates_context_with_episode(self, live_server, monkeypatch: pytest.MonkeyPatch) -> None:
        client, _ = live_server
        monkeypatch.setattr(config, "DELVE_API_KEY", "server-key")
        _link_bonfire(client)
        _register_purchase(client, agent_id="agent-y", episodes=1)
        status, data = _post(client,
            "/game/agents/process-stack",
            {"agent_id": "agent-y"},
        )
        assert status == 200
        assert data.get("success") is True
        gm_context = data.get("game_master_context")
        assert isinstance(gm_context, dict)
        assert gm_context.get("last_episode_id") == "ep-123"
        gm_decision = data.get("gm_decision")
        assert isinstance(gm_decision, dict)
        extension = gm_decision.get("extension_awarded")
        assert isinstance(extension, int)
        assert extension >= 1
        extension_payload = data.get("episode_extension")
        assert isinstance(extension_payload, dict)

    def test_completion_uses_agent_key_from_header_when_provided(self, live_server) -> None:
        client, _ = live_server
        _link_bonfire(client)
        _register_purchase(client, agent_id="agent-h", episodes=2)
        status, data = _post(client,
            "/game/agents/complete",
            {
                "agent_id": "agent-h",
                "message": "help",
                "chat_id": "chat-1",
                "user_id": "player-1",
            },
            headers={"X-Agent-Api-Key": "header-key-999"},
        )
        assert status == 200
        assert data.get("api_key_source") == "header"
        chat = data.get("chat")
        assert isinstance(chat, dict)
        assert "header-key-999" in str(chat.get("reply"))

    def test_completion_sends_runtime_game_context(self, live_server, monkeypatch: pytest.MonkeyPatch) -> None:
        client, _ = live_server
        _register_purchase(client, agent_id="agent-cx", episodes=2, wallet_address="0xowner")
        _post(client,
            "/game/create",
            {
                "bonfire_id": "bf1",
                "erc8004_bonfire_id": 7,
                "wallet_address": "0xowner",
                "game_prompt": "Explore ruins and complete quests",
                "initial_quest_count": 2,
            },
        )
        captured_context: dict[str, object] = {}
        captured_graph_mode = ""

        def fake_agent_json_request(method: str, url: str, api_key: str, body=None):
            nonlocal captured_context, captured_graph_mode
            if url.endswith("/chat"):
                if isinstance(body, dict):
                    context_obj = body.get("context")
                    if isinstance(context_obj, dict):
                        captured_context = context_obj
                    graph_mode_obj = body.get("graph_mode")
                    if isinstance(graph_mode_obj, str):
                        captured_graph_mode = graph_mode_obj
                return 200, {"reply": "ok"}
            if url.endswith("/stack/add"):
                return 200, {"success": True}
            if url.endswith("/stack/process"):
                return 200, {"success": True, "episode_id": "ep-ctx", "message": "episode"}
            return 404, {"error": "not found"}

        monkeypatch.setattr(http_client, "_agent_json_request", fake_agent_json_request)
        status, _ = _post(client,
            "/game/agents/complete",
            {"agent_id": "agent-cx", "message": "what should I do next?"},
        )
        assert status == 200
        assert isinstance(captured_context.get("game"), dict)
        assert isinstance(captured_context.get("agent"), dict)
        assert isinstance(captured_context.get("active_quests"), list)
        assert isinstance(captured_context.get("recent_events"), list)
        assert captured_graph_mode == "regenerate"

    def test_game_master_completion_auto_generates_quest(self, live_server) -> None:
        client, _ = live_server
        _link_bonfire(client)
        _register_purchase(client, agent_id="owner-agent", episodes=2)

        status, data = _post(client,
            "/game/agents/complete",
            {
                "agent_id": "owner-agent",
                "message": "Create a challenge for artifact hunters",
                "as_game_master": True,
                "reward": 3,
            },
        )
        assert status == 200
        auto_quest = data.get("auto_quest")
        assert isinstance(auto_quest, dict)
        assert auto_quest.get("reward") == 3

        state_status, state = _get(client, "/game/state?bonfire_id=bf1")
        assert state_status == 200
        quests = state.get("quests")
        assert isinstance(quests, list)
        assert len(quests) >= 1

    def test_process_stack_persists_gm_response_into_next_chat_context(
        self, live_server, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        client, _ = live_server
        _register_purchase(client, agent_id="owner-agent", episodes=3, wallet_address="0xowner")
        _register_purchase(client, agent_id="player-agent", episodes=3, wallet_address="0xplayer")
        _post(client,
            "/game/create",
            {
                "bonfire_id": "bf1",
                "erc8004_bonfire_id": 7,
                "wallet_address": "0xowner",
                "game_prompt": "Find signal ruins and report changes",
                "initial_quest_count": 1,
                "gm_agent_id": "owner-agent",
            },
        )
        captured_player_context: dict[str, object] = {}

        def fake_agent_json_request(method: str, url: str, api_key: str, body=None):
            nonlocal captured_player_context
            if url.endswith("/agents/player-agent/chat"):
                if isinstance(body, dict):
                    ctx_obj = body.get("context")
                    if isinstance(ctx_obj, dict):
                        captured_player_context = ctx_obj
                return 200, {"reply": "player reply"}
            if url.endswith("/agents/owner-agent/chat"):
                return 200, {
                    "reply": json.dumps(
                        {
                            "extension_awarded": 2,
                            "reaction": "The expedition discovered a stable route.",
                            "world_state_update": "A stable route to the signal ruins is now known.",
                        }
                    )
                }
            if url.endswith("/agents/player-agent/stack/add"):
                return 200, {"success": True}
            if url.endswith("/agents/player-agent/stack/process"):
                return 200, {"success": True, "episode_id": "ep-gm-1", "message": "new route discovered"}
            return 404, {"error": "not found"}

        monkeypatch.setattr(http_client, "_agent_json_request", fake_agent_json_request)

        status_process, data_process = _post(client,
            "/game/agents/process-stack",
            {"agent_id": "player-agent"},
        )
        assert status_process == 200
        gm_decision = data_process.get("gm_decision")
        assert isinstance(gm_decision, dict)
        assert gm_decision.get("world_state_update") == "A stable route to the signal ruins is now known."

        status_complete, _ = _post(client,
            "/game/agents/complete",
            {"agent_id": "player-agent", "message": "what changed in the world?"},
        )
        assert status_complete == 200
        game_obj = captured_player_context.get("game")
        assert isinstance(game_obj, dict)
        assert game_obj.get("world_state_summary") == "A stable route to the signal ruins is now known."
        assert game_obj.get("last_gm_reaction") == "The expedition discovered a stable route."

    def test_non_owner_cannot_generate_quest_via_completion(self, live_server) -> None:
        client, _ = live_server
        _link_bonfire(client)
        _register_purchase(client, agent_id="owner-agent", episodes=2)
        _register_purchase(client, agent_id="other-agent", episodes=2, wallet_address="0xother")

        status, data = _post(client,
            "/game/agents/complete",
            {
                "agent_id": "other-agent",
                "message": "Create a challenge",
                "as_game_master": True,
            },
        )
        assert status == 403
        assert "owner" in str(data.get("error", "")).lower()

    def test_manual_gm_reaction_endpoint(self, live_server) -> None:
        client, _ = live_server
        _register_purchase(client, agent_id="owner-agent", episodes=3, wallet_address="0xowner")
        _register_purchase(client, agent_id="player-agent", episodes=3, wallet_address="0xplayer")
        _post(client,
            "/game/create",
            {
                "bonfire_id": "bf1",
                "erc8004_bonfire_id": 7,
                "wallet_address": "0xowner",
                "game_prompt": "GM reaction flow",
                "initial_quest_count": 1,
            },
        )
        _post(client, "/game/agents/process-stack", {"agent_id": "player-agent"})
        status, data = _post(client,
            "/game/agents/gm-react",
            {"agent_id": "player-agent"},
        )
        assert status == 200
        decision = data.get("gm_decision")
        assert isinstance(decision, dict)
        world = data.get("world_state")
        assert isinstance(world, dict)

    def test_process_stack_accepts_nested_mongo_episode_id(
        self,
        live_server,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        client, _ = live_server
        _register_purchase(client, agent_id="nested-agent", episodes=3, wallet_address="0xowner")

        nested_episode_id = "69a1cbd33ec59f0d19c471e8"

        def fake_agent_json_request(method: str, url: str, api_key: str, body=None):
            if url.endswith("/stack/process"):
                return 200, {
                    "episode": {
                        "_id": {"$oid": nested_episode_id},
                        "summary": "Nested episode summary",
                    }
                }
            return 404, {"error": "not found"}

        monkeypatch.setattr(http_client, "_agent_json_request", fake_agent_json_request)

        status_process, data_process = _post(client,
            "/game/agents/process-stack",
            {"agent_id": "nested-agent"},
        )
        assert status_process == 200
        gm_context = data_process.get("game_master_context")
        assert isinstance(gm_context, dict)
        assert gm_context.get("last_episode_id") == nested_episode_id
        assert gm_context.get("last_episode_summary") == "Nested episode summary"

        status_react, data_react = _post(client,
            "/game/agents/gm-react",
            {"agent_id": "nested-agent"},
        )
        assert status_react == 200
        assert data_react.get("episode_id") == nested_episode_id

    def test_generate_world_episode_endpoint(self, live_server) -> None:
        client, _ = live_server
        _register_purchase(client, agent_id="owner-agent", episodes=3, wallet_address="0xowner")
        _post(client,
            "/game/create",
            {
                "bonfire_id": "bf1",
                "erc8004_bonfire_id": 7,
                "wallet_address": "0xowner",
                "game_prompt": "World episode generation",
                "initial_quest_count": 1,
            },
        )
        _post(client,
            "/game/agents/process-stack",
            {"agent_id": "owner-agent"},
        )
        _post(client,
            "/game/agents/gm-react",
            {"agent_id": "owner-agent"},
        )
        status, data = _post(client,
            "/game/world/generate-episode",
            {"bonfire_id": "bf1"},
        )
        assert status == 200
        assert data.get("bonfire_id") == "bf1"
        assert data.get("owner_agent_id") == "owner-agent"


class TestStackTimerControls:
    def test_process_all_and_timer_status(self, live_server, monkeypatch: pytest.MonkeyPatch) -> None:
        client, _ = live_server
        monkeypatch.setattr(config, "DELVE_API_KEY", "server-key")
        _link_bonfire(client)
        _register_purchase(client, agent_id="agent-z", episodes=2)

        status_all, data_all = _post(client, "/game/stack/process-all", {})
        assert status_all == 200
        assert data_all.get("processed_count") == 1

        status_timer, data_timer = _get(client, "/game/stack/timer/status")
        assert status_timer == 200
        assert data_timer.get("enabled") is False

    def test_process_all_picks_up_mongo_episode_ids_and_updates_world(
        self, live_server, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        client, store = live_server
        _link_bonfire(client)
        _register_purchase(client, agent_id="batch-agent", episodes=3, wallet_address="0xowner")
        _post(client,
            "/game/create",
            {
                "bonfire_id": "bf1",
                "erc8004_bonfire_id": 7,
                "wallet_address": "0xowner",
                "game_prompt": "Batch world",
                "initial_quest_count": 1,
            },
        )

        nested_eid = "69a2abc0000000000000beef"

        def fake_agent_json_request(method: str, url: str, api_key: str, body=None):
            if url.endswith("/stack/process"):
                return 200, {
                    "episode": {
                        "_id": {"$oid": nested_eid},
                        "summary": "major quest milestone completed in batch",
                    }
                }
            if url.endswith("/chat"):
                return 200, {
                    "reply": json.dumps({
                        "extension_awarded": 1,
                        "reaction": "Batch GM reacted.",
                        "world_state_update": "The batch world has changed.",
                    })
                }
            return 404, {"error": "not found"}

        def fake_pre_uuids(agent_id: str) -> list[str]:
            return []

        monkeypatch.setattr(http_client, "_agent_json_request", fake_agent_json_request)
        monkeypatch.setattr(stack_processing, "_get_agent_episode_uuids_standalone", fake_pre_uuids)

        status_all, data_all = _post(client, "/game/stack/process-all", {})
        assert status_all == 200
        results = data_all.get("results")
        assert isinstance(results, list)
        batch_result = next((r for r in results if r.get("agent_id") == "batch-agent"), None)
        assert batch_result is not None
        assert batch_result.get("episode_id") == nested_eid
        gm = batch_result.get("gm_decision")
        assert isinstance(gm, dict)
        assert gm.get("world_state_update") == "The batch world has changed."

        game = store.get_game("bf1")
        assert game is not None
        assert game.world_state_summary == "The batch world has changed."
        assert game.last_gm_reaction == "Batch GM reacted."
        assert game.last_episode_id == nested_eid


class TestPlayerRestore:
    def test_restore_players_by_wallet_and_tx(self, live_server) -> None:
        client, _ = live_server
        _register_purchase(client, agent_id="agent-restore", episodes=3, wallet_address="0xowner")
        status, data = _post(client,
            "/game/player/restore",
            {"wallet_address": "0xowner", "purchase_tx_hash": "0xtx-agent-restore"},
        )
        assert status == 200
        players = data.get("players")
        assert isinstance(players, list)
        assert len(players) == 1
        first = players[0]
        assert first.get("agent_id") == "agent-restore"


class TestChatContextPreamble:
    def test_completion_prepends_game_world_state_to_message(
        self, live_server, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        client, _ = live_server
        _register_purchase(client, agent_id="ctx-agent", episodes=3, wallet_address="0xowner")
        _post(client,
            "/game/create",
            {
                "bonfire_id": "bf1",
                "erc8004_bonfire_id": 7,
                "wallet_address": "0xowner",
                "game_prompt": "A dark forest full of mysteries",
                "initial_quest_count": 1,
            },
        )

        captured_message = ""

        def fake_agent_json_request(method: str, url: str, api_key: str, body=None):
            nonlocal captured_message
            if url.endswith("/chat"):
                if isinstance(body, dict):
                    msg = body.get("message")
                    if isinstance(msg, str):
                        captured_message = msg
                return 200, {"reply": "I see the dark forest ahead."}
            if url.endswith("/stack/add"):
                return 200, {"success": True}
            return 404, {"error": "not found"}

        monkeypatch.setattr(http_client, "_agent_json_request", fake_agent_json_request)

        status, _ = _post(client,
            "/game/agents/complete",
            {"agent_id": "ctx-agent", "message": "Where am I?"},
        )
        assert status == 200
        assert "[GAME WORLD]" in captured_message
        assert "A dark forest full of mysteries" in captured_message
        assert "Where am I?" in captured_message

    def test_preamble_includes_world_state_after_processing(
        self, live_server, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        client, _ = live_server
        _register_purchase(client, agent_id="ws-agent", episodes=3, wallet_address="0xowner")
        _post(client,
            "/game/create",
            {
                "bonfire_id": "bf1",
                "erc8004_bonfire_id": 7,
                "wallet_address": "0xowner",
                "game_prompt": "Ice realm adventure",
                "initial_quest_count": 1,
                "gm_agent_id": "ws-agent",
            },
        )

        call_count = 0
        captured_message = ""

        def fake_agent_json_request(method: str, url: str, api_key: str, body=None):
            nonlocal call_count, captured_message
            call_count += 1
            if url.endswith("/stack/process"):
                return 200, {"success": True, "episode_id": "ep-ws", "message": "glacier explored"}
            if url.endswith("/chat"):
                if isinstance(body, dict):
                    msg = body.get("message")
                    if isinstance(msg, str):
                        captured_message = msg
                    reply_obj = body.get("context", {})
                    if isinstance(reply_obj, dict) and reply_obj.get("role") == "game_master":
                        return 200, {
                            "reply": json.dumps({
                                "extension_awarded": 0,
                                "reaction": "The glacier trembles.",
                                "world_state_update": "Cracks appeared in the glacier.",
                            })
                        }
                return 200, {"reply": "adventure continues"}
            if url.endswith("/stack/add"):
                return 200, {"success": True}
            return 404, {"error": "not found"}

        monkeypatch.setattr(http_client, "_agent_json_request", fake_agent_json_request)

        _post(client, "/game/agents/process-stack", {"agent_id": "ws-agent"})

        _post(client,
            "/game/agents/complete",
            {"agent_id": "ws-agent", "message": "What happened to the glacier?"},
        )
        assert "[CURRENT WORLD STATE]" in captured_message
        assert "Cracks appeared in the glacier" in captured_message


class TestBackfillWorldState:
    def test_backfill_updates_world_state_from_existing_episode(
        self, live_server, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        client, store = live_server
        _link_bonfire(client)
        _register_purchase(client, agent_id="bf-agent", episodes=3, wallet_address="0xowner")
        _post(client,
            "/game/create",
            {
                "bonfire_id": "bf1",
                "erc8004_bonfire_id": 7,
                "wallet_address": "0xowner",
                "game_prompt": "Backfill world",
                "initial_quest_count": 1,
            },
        )

        def fake_json_request(method: str, url: str, body=None):
            if "/bonfires/bf1/episodes" in url and "limit=" in url:
                return 200, {
                    "episodes": [
                        {
                            "_id": {"$oid": "aaa111bbb222ccc333ddd444"},
                            "summary": "Ancient ruins were discovered in the north.",
                        }
                    ]
                }
            if "/episodes/aaa111bbb222ccc333ddd444" in url:
                return 200, {
                    "episode": {
                        "_id": {"$oid": "aaa111bbb222ccc333ddd444"},
                        "summary": "Ancient ruins were discovered in the north.",
                    }
                }
            if "/reveal_nonce" in url:
                return 200, {"nonce": "abc", "message": "sign me"}
            if "/reveal_api_key" in url:
                return 200, {"api_key": "agent-key-123"}
            if "/purchase-agent" in url:
                return 200, {"agent_id": "agent-upstream", "purchase_id": "purchase-upstream"}
            if "/bonfires/" in url and url.endswith("/pricing"):
                return 200, {"price_per_episode": "0.01", "max_episodes_per_agent": 20, "max_agents": 10}
            if "/bonfires/" in url and url.endswith("/agents"):
                return 200, {"bonfire_id": "bf1", "agents": [], "total_agents": 0, "active_agents": 0}
            if "/provision?wallet_address=" in url:
                return 200, {"records": []}
            return 404, {"error": "not found"}

        def fake_agent_json_request(method: str, url: str, api_key: str, body=None):
            if url.endswith("/chat"):
                return 200, {
                    "reply": json.dumps({
                        "extension_awarded": 0,
                        "reaction": "Backfill GM reaction: ruins explored.",
                        "world_state_update": "Ruins of an ancient civilization found to the north.",
                    })
                }
            return 404, {"error": "not found"}

        monkeypatch.setattr(http_client, "_json_request", fake_json_request)
        monkeypatch.setattr(http_client, "_agent_json_request", fake_agent_json_request)

        status, data = _post(client,
            "/game/admin/backfill-world-state",
            {"bonfire_id": "bf1"},
        )
        assert status == 200
        assert data.get("backfilled") is True
        assert data.get("episode_id") == "aaa111bbb222ccc333ddd444"
        world_state = data.get("world_state")
        assert isinstance(world_state, dict)
        assert "Ruins of an ancient civilization" in str(world_state.get("world_state_summary", ""))

        game = store.get_game("bf1")
        assert game is not None
        assert "Ruins of an ancient civilization" in game.world_state_summary

    def test_backfill_with_explicit_episode_id(
        self, live_server, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        client, store = live_server
        _link_bonfire(client)
        _register_purchase(client, agent_id="bf2-agent", episodes=3, wallet_address="0xowner")
        _post(client,
            "/game/create",
            {
                "bonfire_id": "bf1",
                "erc8004_bonfire_id": 7,
                "wallet_address": "0xowner",
                "game_prompt": "Explicit backfill world",
                "initial_quest_count": 1,
            },
        )

        target_id = "eee555fff666000111222333"

        def fake_json_request(method: str, url: str, body=None):
            if f"/episodes/{target_id}" in url:
                return 200, {
                    "episode": {
                        "_id": {"$oid": target_id},
                        "summary": "The crystal cavern collapsed.",
                    }
                }
            if "/reveal_nonce" in url:
                return 200, {"nonce": "abc", "message": "sign me"}
            if "/reveal_api_key" in url:
                return 200, {"api_key": "agent-key-123"}
            if "/purchase-agent" in url:
                return 200, {"agent_id": "agent-upstream", "purchase_id": "purchase-upstream"}
            if "/bonfires/" in url and url.endswith("/pricing"):
                return 200, {"price_per_episode": "0.01", "max_episodes_per_agent": 20, "max_agents": 10}
            if "/bonfires/" in url and url.endswith("/agents"):
                return 200, {"bonfire_id": "bf1", "agents": [], "total_agents": 0, "active_agents": 0}
            if "/provision?wallet_address=" in url:
                return 200, {"records": []}
            return 404, {"error": "not found"}

        def fake_agent_json_request(method: str, url: str, api_key: str, body=None):
            if url.endswith("/chat"):
                return 200, {
                    "reply": json.dumps({
                        "extension_awarded": 0,
                        "reaction": "Cavern collapsed, new paths opened.",
                        "world_state_update": "Crystal cavern collapse revealed underground tunnels.",
                    })
                }
            return 404, {"error": "not found"}

        monkeypatch.setattr(http_client, "_json_request", fake_json_request)
        monkeypatch.setattr(http_client, "_agent_json_request", fake_agent_json_request)

        status, data = _post(client,
            "/game/admin/backfill-world-state",
            {"bonfire_id": "bf1", "episode_id": target_id},
        )
        assert status == 200
        assert data.get("episode_id") == target_id
        game = store.get_game("bf1")
        assert game is not None
        assert "underground tunnels" in game.world_state_summary

    def test_backfill_returns_404_when_no_episodes(
        self, live_server, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        client, _ = live_server
        _link_bonfire(client)
        _post(client,
            "/game/create",
            {
                "bonfire_id": "bf1",
                "erc8004_bonfire_id": 7,
                "wallet_address": "0xowner",
                "game_prompt": "Empty world",
                "initial_quest_count": 1,
            },
        )

        def fake_json_request(method: str, url: str, body=None):
            if "/bonfires/bf1/episodes" in url:
                return 200, {"episodes": []}
            if "/reveal_nonce" in url:
                return 200, {"nonce": "abc", "message": "sign me"}
            if "/reveal_api_key" in url:
                return 200, {"api_key": "agent-key-123"}
            if "/purchase-agent" in url:
                return 200, {"agent_id": "agent-upstream", "purchase_id": "purchase-upstream"}
            if "/bonfires/" in url and url.endswith("/pricing"):
                return 200, {"price_per_episode": "0.01", "max_episodes_per_agent": 20, "max_agents": 10}
            if "/bonfires/" in url and url.endswith("/agents"):
                return 200, {"bonfire_id": "bf1", "agents": [], "total_agents": 0, "active_agents": 0}
            if "/provision?wallet_address=" in url:
                return 200, {"records": []}
            return 404, {"error": "not found"}

        monkeypatch.setattr(http_client, "_json_request", fake_json_request)

        status, data = _post(client,
            "/game/admin/backfill-world-state",
            {"bonfire_id": "bf1"},
        )
        assert status == 404
        assert data.get("error") == "no_episodes_found"


class TestEpisodeUuidResolution:
    """Tests for episode UUID resolution via agent object and UUID-first fetch."""

    def test_resolve_latest_episode_from_agent(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """_resolve_latest_episode_from_agent returns last entry from episode_uuids."""

        def fake_json_request(method: str, url: str, body=None):
            if "/agents/agent-uuid-test" in url:
                return 200, {
                    "episode_uuids": [
                        "aaa-111",
                        "bbb-222",
                        "ccc-333",
                    ],
                }
            return 404, {}

        monkeypatch.setattr(http_client, "_json_request", fake_json_request)
        result = stack_processing._resolve_latest_episode_from_agent("agent-uuid-test")
        assert result == "ccc-333"

    def test_resolve_latest_episode_empty_array(self, monkeypatch: pytest.MonkeyPatch) -> None:
        def fake_json_request(method: str, url: str, body=None):
            return 200, {"episode_uuids": []}

        monkeypatch.setattr(http_client, "_json_request", fake_json_request)
        assert stack_processing._resolve_latest_episode_from_agent("agent-empty") == ""

    def test_fetch_episode_payload_tries_uuid_first(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """_fetch_episode_payload should try /episodes/by-uuid/ before ObjectId endpoints."""
        call_log: list[str] = []

        def fake_json_request(method: str, url: str, body=None):
            call_log.append(url)
            if "/episodes/by-uuid/my-uuid-123" in url:
                return 200, {
                    "summary": "found via uuid",
                    "episode_uuid": "my-uuid-123",
                    "episode_text": "content here",
                }
            return 404, {}

        monkeypatch.setattr(http_client, "_json_request", fake_json_request)
        result = stack_processing._fetch_episode_payload("bf1", "my-uuid-123")
        assert result is not None
        assert result.get("summary") == "found via uuid"
        assert any("/episodes/by-uuid/my-uuid-123" in c for c in call_log)
        assert not any("/episodes/my-uuid-123" == c.split("?")[0] for c in call_log if "by-uuid" not in c)

    def test_fetch_episode_payload_falls_back_to_objectid(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """When UUID lookup returns 404, fall back to ObjectId-based endpoints."""

        def fake_json_request(method: str, url: str, body=None):
            if "/episodes/by-uuid/" in url:
                return 404, {}
            if url.endswith("/episodes/abc123objectid"):
                return 200, {"summary": "found via objectid", "_id": "abc123objectid"}
            return 404, {}

        monkeypatch.setattr(http_client, "_json_request", fake_json_request)
        result = stack_processing._fetch_episode_payload("bf1", "abc123objectid")
        assert result is not None
        assert result.get("summary") == "found via objectid"

    def test_process_stack_uses_agent_episode_uuids_fallback(
        self, live_server, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """When stack/process returns no episode ID, fall back to agent's episode_uuids."""
        client, store = live_server
        _link_bonfire(client)
        _register_purchase(client, agent_id="uuid-agent", episodes=3, wallet_address="0xowner")
        _post(client, "/game/create", {
            "bonfire_id": "bf1",
            "erc8004_bonfire_id": 7,
            "wallet_address": "0xowner",
            "game_prompt": "UUID test world",
            "initial_quest_count": 1,
        })

        target_uuid = "e6f44be0-87f5-4477-8a0a-06a713f6f295"

        def fake_agent_json_request(method: str, url: str, api_key: str, body=None):
            if url.endswith("/stack/process"):
                return 200, {"message": "Stack processed successfully"}
            if url.endswith("/chat"):
                return 200, {"reply": json.dumps({
                    "extension_awarded": 0,
                    "reaction": "UUID GM reaction.",
                    "world_state_update": "UUID world updated.",
                })}
            return 404, {}

        def fake_json_request(method: str, url: str, body=None):
            if "/agents/uuid-agent" in url and "stack" not in url:
                return 200, {"episode_uuids": [target_uuid]}
            if f"/episodes/by-uuid/{target_uuid}" in url:
                return 200, {
                    "summary": "Episode from UUID lookup",
                    "episode_uuid": target_uuid,
                    "episode_text": '{"name": "UUID Episode", "content": "test content"}',
                }
            if "/reveal_nonce" in url:
                return 200, {"nonce": "abc", "message": "sign me"}
            if "/reveal_api_key" in url:
                return 200, {"api_key": "key-123"}
            if "/purchase-agent" in url:
                return 200, {"agent_id": "uuid-agent", "purchase_id": "p1"}
            if "/bonfires/" in url and url.endswith("/pricing"):
                return 200, {"price_per_episode": "0.01", "max_episodes_per_agent": 20, "max_agents": 10}
            if "/bonfires/" in url and url.endswith("/agents"):
                return 200, {"bonfire_id": "bf1", "agents": [], "total_agents": 0, "active_agents": 0}
            if "/provision?wallet_address=" in url:
                return 200, {"records": []}
            return 404, {}

        def fake_poll(agent_id: str, pre_uuids: list[str], max_wait: float = 30.0, interval: float = 2.0) -> str:
            return target_uuid

        monkeypatch.setattr(http_client, "_agent_json_request", fake_agent_json_request)
        monkeypatch.setattr(http_client, "_json_request", fake_json_request)
        monkeypatch.setattr(stack_processing, "_poll_for_new_episode_standalone", fake_poll)

        status, data = _post(client, "/game/stack/process-all", {})
        assert status == 200
        results = data.get("results")
        assert isinstance(results, list)
        uuid_result = next((r for r in results if r.get("agent_id") == "uuid-agent"), None)
        assert uuid_result is not None
        assert uuid_result.get("episode_id") == target_uuid

        game = store.get_game("bf1")
        assert game is not None
        assert game.world_state_summary == "UUID world updated."
        assert game.last_gm_reaction == "UUID GM reaction."


class TestGraphProxy:
    """Tests for knowledge map graph proxy endpoints."""

    def test_graph_returns_empty_when_no_episodes(self, live_server) -> None:
        client, _ = live_server
        _link_bonfire(client)
        status, data = _get(client, "/game/graph?bonfire_id=bf1")
        assert status == 200
        assert data.get("nodes") == []
        assert data.get("edges") == []

    def test_graph_returns_nodes_from_episode_expand(
        self, live_server, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        client, _ = live_server
        _link_bonfire(client)
        _register_purchase(client, agent_id="graph-agent", episodes=3, wallet_address="0xowner")

        def fake_json_request(method: str, url: str, body=None):
            if "/agents/graph-agent" in url and "stack" not in url and "knowledge_graph" not in url:
                return 200, {"episode_uuids": ["ep-uuid-1", "ep-uuid-2"]}
            if "/knowledge_graph/episodes/expand" in url:
                return 200, {
                    "nodes": [
                        {"uuid": "n1", "name": "Azure Grotto", "labels": ["Entity"], "summary": "A cave"},
                        {"uuid": "n2", "name": "Crystal Spire", "labels": ["Entity"], "summary": "A tower"},
                    ],
                    "edges": [
                        {"uuid": "e1", "source_node_uuid": "n1", "target_node_uuid": "n2", "name": "connects_to"},
                    ],
                    "episodes": [],
                }
            if "/reveal_nonce" in url:
                return 200, {"nonce": "abc", "message": "sign me"}
            if "/reveal_api_key" in url:
                return 200, {"api_key": "key"}
            if "/purchase-agent" in url:
                return 200, {"agent_id": "graph-agent", "purchase_id": "p1"}
            if "/bonfires/" in url and url.endswith("/pricing"):
                return 200, {"price_per_episode": "0.01", "max_episodes_per_agent": 20, "max_agents": 10}
            if "/bonfires/" in url and url.endswith("/agents"):
                return 200, {"bonfire_id": "bf1", "agents": [], "total_agents": 0, "active_agents": 0}
            if "/provision?wallet_address=" in url:
                return 200, {"records": []}
            return 404, {}

        monkeypatch.setattr(http_client, "_json_request", fake_json_request)

        status, data = _get(client, "/game/graph?bonfire_id=bf1&agent_id=graph-agent")
        assert status == 200
        nodes = data.get("nodes")
        assert isinstance(nodes, list)
        assert len(nodes) == 2
        assert nodes[0]["name"] == "Azure Grotto"
        edges = data.get("edges")
        assert isinstance(edges, list)
        assert len(edges) == 1
        assert edges[0]["source"] == "n1"
        assert edges[0]["target"] == "n2"


class TestEntityExpand:
    def test_entity_expand_proxies_to_delve(
        self, live_server, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        client, _ = live_server

        def fake_json_request(method: str, url: str, body=None):
            if "/knowledge_graph/expand/entity" in url:
                return 200, {
                    "nodes": [
                        {"uuid": "n1", "name": "Azure Grotto", "labels": ["Entity"]},
                        {"uuid": "n3", "name": "Hidden Path", "labels": ["Entity"]},
                    ],
                    "edges": [
                        {"uuid": "e2", "source_node_uuid": "n1", "target_node_uuid": "n3", "name": "leads_to"},
                    ],
                    "episodes": [],
                }
            if "/reveal_nonce" in url:
                return 200, {"nonce": "abc", "message": "sign me"}
            if "/reveal_api_key" in url:
                return 200, {"api_key": "key"}
            if "/purchase-agent" in url:
                return 200, {"agent_id": "a1", "purchase_id": "p1"}
            if "/bonfires/" in url and url.endswith("/pricing"):
                return 200, {"price_per_episode": "0.01", "max_episodes_per_agent": 20, "max_agents": 10}
            if "/bonfires/" in url and url.endswith("/agents"):
                return 200, {"bonfire_id": "bf1", "agents": [], "total_agents": 0, "active_agents": 0}
            if "/provision?wallet_address=" in url:
                return 200, {"records": []}
            return 404, {}

        monkeypatch.setattr(http_client, "_json_request", fake_json_request)

        status, data = _post(client, "/game/entity/expand", {
            "entity_uuid": "n1",
            "bonfire_id": "bf1",
        })
        assert status == 200
        nodes = data.get("nodes")
        assert isinstance(nodes, list)
        assert len(nodes) == 2
        edges = data.get("edges")
        assert isinstance(edges, list)
        assert len(edges) == 1
        assert edges[0]["name"] == "leads_to"


class TestQuestGeneration:
    def test_generate_quests_from_graph_entities(
        self, live_server, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        client, _ = live_server
        _link_bonfire(client)
        _register_purchase(client, agent_id="quest-agent", episodes=3, wallet_address="0xowner")
        _post(client, "/game/create", {
            "bonfire_id": "bf1",
            "erc8004_bonfire_id": 7,
            "wallet_address": "0xowner",
            "game_prompt": "Quest gen test world",
            "initial_quest_count": 0,
        })

        def fake_json_request(method: str, url: str, body=None):
            if "/delve" in url and method == "POST":
                return 200, {
                    "entities": [
                        {"uuid": "ent1", "name": "Whispering Canopy", "summary": "Dense jungle canopy"},
                        {"uuid": "ent2", "name": "Crystal Spire", "summary": "Towering crystal formation"},
                        {"uuid": "ent3", "name": "Shadow Cave", "summary": "Dark cave system"},
                    ],
                }
            if "/reveal_nonce" in url:
                return 200, {"nonce": "abc", "message": "sign me"}
            if "/reveal_api_key" in url:
                return 200, {"api_key": "key"}
            if "/purchase-agent" in url:
                return 200, {"agent_id": "quest-agent", "purchase_id": "p1"}
            if "/bonfires/" in url and url.endswith("/pricing"):
                return 200, {"price_per_episode": "0.01", "max_episodes_per_agent": 20, "max_agents": 10}
            if "/bonfires/" in url and url.endswith("/agents"):
                return 200, {"bonfire_id": "bf1", "agents": [], "total_agents": 0, "active_agents": 0}
            if "/provision?wallet_address=" in url:
                return 200, {"records": []}
            return 404, {}

        monkeypatch.setattr(http_client, "_json_request", fake_json_request)

        status, data = _post(client, "/game/quests/generate", {"bonfire_id": "bf1"})
        assert status == 200
        quests = data.get("quests")
        assert isinstance(quests, list)
        assert len(quests) == 3
        for q in quests:
            assert q.get("quest_type") == "graph_discovery"
            assert q.get("keyword")
            assert q.get("entity_uuid")
            assert q.get("reward") >= 1

    def test_generate_quests_returns_empty_when_no_entities(
        self, live_server, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        client, _ = live_server
        _link_bonfire(client)
        _post(client, "/game/create", {
            "bonfire_id": "bf1",
            "erc8004_bonfire_id": 7,
            "wallet_address": "0xowner",
            "game_prompt": "Empty world",
            "initial_quest_count": 0,
        })

        def fake_json_request(method: str, url: str, body=None):
            if "/delve" in url:
                return 200, {"entities": []}
            if "/reveal_nonce" in url:
                return 200, {"nonce": "abc", "message": "sign me"}
            if "/reveal_api_key" in url:
                return 200, {"api_key": "key"}
            if "/purchase-agent" in url:
                return 200, {"agent_id": "a1", "purchase_id": "p1"}
            if "/bonfires/" in url and url.endswith("/pricing"):
                return 200, {"price_per_episode": "0.01", "max_episodes_per_agent": 20, "max_agents": 10}
            if "/bonfires/" in url and url.endswith("/agents"):
                return 200, {"bonfire_id": "bf1", "agents": [], "total_agents": 0, "active_agents": 0}
            if "/provision?wallet_address=" in url:
                return 200, {"records": []}
            return 404, {}

        monkeypatch.setattr(http_client, "_json_request", fake_json_request)

        status, data = _post(client, "/game/quests/generate", {"bonfire_id": "bf1"})
        assert status == 200
        assert data.get("quests") == []

    def test_generate_quests_skips_duplicate_keywords(
        self, live_server, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        client, store = live_server
        _link_bonfire(client)
        _register_purchase(client, agent_id="dup-agent", episodes=3, wallet_address="0xowner")
        _post(client, "/game/create", {
            "bonfire_id": "bf1",
            "erc8004_bonfire_id": 7,
            "wallet_address": "0xowner",
            "game_prompt": "Dup test",
            "initial_quest_count": 0,
        })

        store.create_quest(
            bonfire_id="bf1", creator_wallet="0xowner", quest_type="manual",
            prompt="existing quest", keyword="whispering", reward=1,
            cooldown_seconds=60, expires_in_seconds=None,
        )

        def fake_json_request(method: str, url: str, body=None):
            if "/delve" in url:
                return 200, {
                    "entities": [
                        {"uuid": "ent1", "name": "Whispering Canopy", "summary": "jungle"},
                        {"uuid": "ent2", "name": "Crystal Spire", "summary": "tower"},
                    ],
                }
            if "/reveal_nonce" in url:
                return 200, {"nonce": "abc", "message": "sign me"}
            if "/reveal_api_key" in url:
                return 200, {"api_key": "key"}
            if "/purchase-agent" in url:
                return 200, {"agent_id": "dup-agent", "purchase_id": "p1"}
            if "/bonfires/" in url and url.endswith("/pricing"):
                return 200, {"price_per_episode": "0.01", "max_episodes_per_agent": 20, "max_agents": 10}
            if "/bonfires/" in url and url.endswith("/agents"):
                return 200, {"bonfire_id": "bf1", "agents": [], "total_agents": 0, "active_agents": 0}
            if "/provision?wallet_address=" in url:
                return 200, {"records": []}
            return 404, {}

        monkeypatch.setattr(http_client, "_json_request", fake_json_request)

        status, data = _post(client, "/game/quests/generate", {"bonfire_id": "bf1"})
        assert status == 200
        quests = data.get("quests")
        assert isinstance(quests, list)
        keywords = [q["keyword"] for q in quests]
        assert "whispering" not in keywords
        assert len(quests) == 1
        assert quests[0]["keyword"] == "crystal"


# ---------------------------------------------------------------------------
# Room System Tests
# ---------------------------------------------------------------------------


class TestRoomSystem:
    def test_create_room_and_get_map(self, live_server) -> None:
        client, store = live_server
        _link_bonfire(client)
        store.create_or_replace_game(
            bonfire_id="bf1", owner_wallet="0xowner", game_prompt="test",
            gm_agent_id=None, initial_episode_summary="",
        )
        room = store.create_room("bf1", "The Hearth", "Starting room", [])
        assert room.name == "The Hearth"
        room_map = store.get_room_map("bf1")
        assert len(room_map["rooms"]) == 1
        assert room_map["rooms"][0]["room_id"] == room.room_id

    def test_move_player_to_room(self, live_server) -> None:
        client, store = live_server
        _link_bonfire(client)
        store.create_or_replace_game(
            bonfire_id="bf1", owner_wallet="0xowner", game_prompt="test",
            gm_agent_id=None, initial_episode_summary="",
        )
        room = store.create_room("bf1", "The Hearth")
        _register_purchase(client)
        assert store.move_player("agent-1", room.room_id)
        player = store.get_player("agent-1")
        assert player is not None
        assert player.current_room == room.room_id

    def test_move_to_invalid_room_fails(self, live_server) -> None:
        client, store = live_server
        _link_bonfire(client)
        store.create_or_replace_game(
            bonfire_id="bf1", owner_wallet="0xowner", game_prompt="test",
            gm_agent_id=None, initial_episode_summary="",
        )
        _register_purchase(client)
        assert not store.move_player("agent-1", "nonexistent-room-id")

    def test_ensure_starting_room(self, live_server) -> None:
        client, store = live_server
        _link_bonfire(client)
        store.create_or_replace_game(
            bonfire_id="bf1", owner_wallet="0xowner", game_prompt="test",
            gm_agent_id=None, initial_episode_summary="",
        )
        room_id = store.ensure_starting_room("bf1")
        assert room_id != ""
        second_id = store.ensure_starting_room("bf1")
        assert second_id == room_id

    def test_place_player_in_starting_room(self, live_server) -> None:
        client, store = live_server
        _link_bonfire(client)
        store.create_or_replace_game(
            bonfire_id="bf1", owner_wallet="0xowner", game_prompt="test",
            gm_agent_id=None, initial_episode_summary="",
        )
        store.ensure_starting_room("bf1")
        _register_purchase(client)
        player = store.get_player("agent-1")
        assert player is not None
        assert player.current_room != ""

    def test_get_map_endpoint(self, live_server) -> None:
        client, store = live_server
        _link_bonfire(client)
        store.create_or_replace_game(
            bonfire_id="bf1", owner_wallet="0xowner", game_prompt="test",
            gm_agent_id=None, initial_episode_summary="",
        )
        store.ensure_starting_room("bf1")
        _register_purchase(client)
        status, data = _get(client, "/game/map?bonfire_id=bf1")
        assert status == 200
        assert len(data["rooms"]) == 1
        assert len(data["players"]) == 1
        assert data["players"][0]["current_room"] != ""

    def test_map_init_endpoint(self, live_server) -> None:
        client, store = live_server
        _link_bonfire(client)
        store.create_or_replace_game(
            bonfire_id="bf1", owner_wallet="0xowner", game_prompt="test",
            gm_agent_id=None, initial_episode_summary="",
        )
        status, data = _post(client, "/game/map/init", {"bonfire_id": "bf1"})
        assert status == 200
        assert len(data["rooms"]) == 1
        assert data["rooms"][0]["name"] == "The Hearth"


# ---------------------------------------------------------------------------
# GM Agent Separation Tests
# ---------------------------------------------------------------------------


class TestGmAgentSeparation:
    def test_prefers_gm_agent_id_from_game(self, live_server) -> None:
        client, store = live_server
        _link_bonfire(client)
        store.create_or_replace_game(
            bonfire_id="bf1", owner_wallet="0xowner", game_prompt="test",
            gm_agent_id="gm-agent-explicit", initial_episode_summary="",
        )
        _register_purchase(client, agent_id="agent-1")
        gm_id = store.get_owner_agent_id("bf1")
        assert gm_id == "gm-agent-explicit"

    def test_falls_back_to_non_player_agent(self, live_server) -> None:
        client, store = live_server
        _link_bonfire(client)
        store.create_or_replace_game(
            bonfire_id="bf1", owner_wallet="0xowner", game_prompt="test",
            gm_agent_id=None, initial_episode_summary="",
        )
        _register_purchase(client, agent_id="agent-1")
        gm_id = store.get_owner_agent_id("bf1")
        assert gm_id == "agent-1"

    def test_create_game_warns_no_gm_agent(self, live_server) -> None:
        client, _store = live_server
        _link_bonfire(client)
        status, data = _post(client, "/game/create", {
            "bonfire_id": "bf1",
            "erc8004_bonfire_id": 7,
            "wallet_address": "0xowner",
            "game_prompt": "test game",
        })
        assert status == 200
        assert "warning" in data

    def test_create_game_no_warning_with_gm(self, live_server) -> None:
        client, _store = live_server
        _link_bonfire(client)
        status, data = _post(client, "/game/create", {
            "bonfire_id": "bf1",
            "erc8004_bonfire_id": 7,
            "wallet_address": "0xowner",
            "game_prompt": "test game",
            "gm_agent_id": "gm-agent-99",
        })
        assert status == 200
        assert "warning" not in data


# ---------------------------------------------------------------------------
# End Turn Tests
# ---------------------------------------------------------------------------


class TestEndTurn:
    def test_end_turn_unregistered_agent(self, live_server) -> None:
        client, _store = live_server
        status, data = _post(client, "/game/agents/end-turn", {"agent_id": "unknown"})
        assert status == 404
        assert data["error"] == "agent is not registered in game"

    def test_end_turn_creates_episode_and_gm_decision(
        self, live_server, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        client, store = live_server
        _link_bonfire(client)
        store.create_or_replace_game(
            bonfire_id="bf1", owner_wallet="0xowner", game_prompt="test",
            gm_agent_id="gm-agent-77", initial_episode_summary="",
        )
        store.ensure_starting_room("bf1")
        _register_purchase(client, agent_id="agent-1")

        gm_response_json = json.dumps({
            "extension_awarded": 1,
            "reaction": "The GM approves.",
            "world_state_update": "A new dawn.",
            "room_movements": [{"agent_id": "agent-1", "to_room": "nonexistent"}],
        })

        call_log: list[str] = []

        def fake_agent_json(method: str, url: str, api_key: str, body=None):
            call_log.append(url)
            if url.endswith("/stack/process"):
                return 200, {"episode_id": "ep-turn-1", "message": "episode processed"}
            if "/agents/gm-agent-77/chat" in url:
                return 200, {"reply": gm_response_json}
            if url.endswith("/stack/add"):
                return 200, {"success": True}
            return 404, {"error": "not found"}

        monkeypatch.setattr(http_client, "_agent_json_request", fake_agent_json)
        monkeypatch.setattr(stack_processing, "_poll_for_new_episode_standalone", lambda *a, **k: "")
        monkeypatch.setattr(stack_processing, "_get_agent_episode_uuids_standalone", lambda *a: [])

        status, data = _post(client, "/game/agents/end-turn", {"agent_id": "agent-1"})
        assert status == 200
        assert data.get("episode_id") == "ep-turn-1"
        assert data.get("gm_decision") is not None
        assert data["gm_decision"]["reaction"] == "The GM approves."
        assert data["gm_decision"]["extension_awarded"] == 1

        gm_chat_calls = [u for u in call_log if "/agents/gm-agent-77/chat" in u]
        assert len(gm_chat_calls) == 1

        user_stack_process = [u for u in call_log if "/agents/agent-1/stack/process" in u]
        assert len(user_stack_process) == 1

    def test_end_turn_returns_room_map(
        self, live_server, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        client, store = live_server
        _link_bonfire(client)
        store.create_or_replace_game(
            bonfire_id="bf1", owner_wallet="0xowner", game_prompt="test",
            gm_agent_id="gm-agent-77", initial_episode_summary="",
        )
        store.ensure_starting_room("bf1")
        _register_purchase(client, agent_id="agent-1")

        def fake_agent_json(method: str, url: str, api_key: str, body=None):
            if url.endswith("/stack/process"):
                return 200, {"episode_id": "ep-2", "message": "processed"}
            if "/chat" in url:
                return 200, {"reply": json.dumps({
                    "extension_awarded": 0, "reaction": "Ok.",
                    "world_state_update": "", "room_movements": [],
                })}
            if url.endswith("/stack/add"):
                return 200, {"success": True}
            return 404, {}

        monkeypatch.setattr(http_client, "_agent_json_request", fake_agent_json)
        monkeypatch.setattr(stack_processing, "_poll_for_new_episode_standalone", lambda *a, **k: "")
        monkeypatch.setattr(stack_processing, "_get_agent_episode_uuids_standalone", lambda *a: [])

        status, data = _post(client, "/game/agents/end-turn", {"agent_id": "agent-1"})
        assert status == 200
        assert "room_map" in data
        assert len(data["room_map"]["rooms"]) >= 1


# ---------------------------------------------------------------------------
# Room Movement Tests
# ---------------------------------------------------------------------------


class TestRoomMovements:
    def test_gm_room_movements_applied(
        self, live_server, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        client, store = live_server
        _link_bonfire(client)
        store.create_or_replace_game(
            bonfire_id="bf1", owner_wallet="0xowner", game_prompt="test",
            gm_agent_id="gm-agent-77", initial_episode_summary="",
        )
        store.ensure_starting_room("bf1")
        room2 = store.create_room("bf1", "Dark Forest", "A dangerous forest")
        _register_purchase(client, agent_id="agent-1")

        gm_resp = json.dumps({
            "extension_awarded": 0,
            "reaction": "You enter the forest.",
            "world_state_update": "The forest beckons.",
            "room_movements": [{"agent_id": "agent-1", "to_room": room2.room_id}],
        })

        def fake_agent_json(method: str, url: str, api_key: str, body=None):
            if url.endswith("/stack/process"):
                return 200, {"episode_id": "ep-mv-1", "message": "processed"}
            if "/chat" in url:
                return 200, {"reply": gm_resp}
            if url.endswith("/stack/add"):
                return 200, {"success": True}
            return 404, {}

        monkeypatch.setattr(http_client, "_agent_json_request", fake_agent_json)
        monkeypatch.setattr(stack_processing, "_poll_for_new_episode_standalone", lambda *a, **k: "")
        monkeypatch.setattr(stack_processing, "_get_agent_episode_uuids_standalone", lambda *a: [])

        status, data = _post(client, "/game/agents/end-turn", {"agent_id": "agent-1"})
        assert status == 200
        changes = data.get("room_changes", {})
        applied = changes.get("movements_applied", [])
        assert len(applied) == 1
        assert applied[0]["agent_id"] == "agent-1"
        assert applied[0]["to_room"] == room2.room_id

        player = store.get_player("agent-1")
        assert player is not None
        assert player.current_room == room2.room_id

    def test_gm_room_movement_by_name(
        self, live_server, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        client, store = live_server
        _link_bonfire(client)
        store.create_or_replace_game(
            bonfire_id="bf1", owner_wallet="0xowner", game_prompt="test",
            gm_agent_id="gm-agent-77", initial_episode_summary="",
        )
        store.ensure_starting_room("bf1")
        room2 = store.create_room("bf1", "Crystal Cave")
        _register_purchase(client, agent_id="agent-1")

        gm_resp = json.dumps({
            "extension_awarded": 0,
            "reaction": "Caves ahead.",
            "world_state_update": "",
            "room_movements": [{"agent_id": "agent-1", "to_room": "crystal cave"}],
        })

        def fake_agent_json(method: str, url: str, api_key: str, body=None):
            if url.endswith("/stack/process"):
                return 200, {"episode_id": "ep-mv-2", "message": "ok"}
            if "/chat" in url:
                return 200, {"reply": gm_resp}
            if url.endswith("/stack/add"):
                return 200, {"success": True}
            return 404, {}

        monkeypatch.setattr(http_client, "_agent_json_request", fake_agent_json)
        monkeypatch.setattr(stack_processing, "_poll_for_new_episode_standalone", lambda *a, **k: "")
        monkeypatch.setattr(stack_processing, "_get_agent_episode_uuids_standalone", lambda *a: [])

        status, data = _post(client, "/game/agents/end-turn", {"agent_id": "agent-1"})
        assert status == 200
        changes = data.get("room_changes", {})
        applied = changes.get("movements_applied", [])
        assert len(applied) == 1
        player = store.get_player("agent-1")
        assert player is not None
        assert player.current_room == room2.room_id


# ---------------------------------------------------------------------------
# GM Batch Timer Tests
# ---------------------------------------------------------------------------


class TestGmBatchTimer:
    def test_process_gm_stacks_function(
        self, live_server, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        client, store = live_server
        _link_bonfire(client)
        store.create_or_replace_game(
            bonfire_id="bf1", owner_wallet="0xowner", game_prompt="test",
            gm_agent_id="gm-agent-77", initial_episode_summary="",
        )
        _register_purchase(client, agent_id="agent-1")

        processed_agents: list[str] = []

        def fake_agent_json(method: str, url: str, api_key: str, body=None):
            if url.endswith("/stack/process"):
                for part in url.split("/"):
                    if part.startswith("gm-"):
                        processed_agents.append(part)
                return 200, {"episode_id": "gm-ep-1", "message": "ok"}
            return 404, {}

        monkeypatch.setattr(http_client, "_agent_json_request", fake_agent_json)
        monkeypatch.setattr(stack_processing, "_poll_for_new_episode_standalone", lambda *a, **k: "gm-ep-1")
        monkeypatch.setattr(stack_processing, "_get_agent_episode_uuids_standalone", lambda *a: [])

        fn = stack_processing._process_gm_stacks
        result = fn(store)
        assert result["processed_count"] == 1
        assert "gm-agent-77" in processed_agents

    def test_gm_batch_timer_runner_starts_and_stops(self) -> None:
        store_cls = GameStore
        store = store_cls(storage_path=Path(tempfile.gettempdir()) / f"gm-timer-test-{time.time_ns()}.json")
        timer_cls = timers.GmBatchTimerRunner
        timer = timer_cls(store=store, interval_seconds=30)
        assert not timer.is_running
        timer.start()
        assert timer.is_running
        timer.stop()
        assert not timer.is_running


# ---------------------------------------------------------------------------
# Room Chat Store Tests
# ---------------------------------------------------------------------------


class TestRoomChatStore:
    def test_append_and_get_room_messages(self) -> None:
        store_cls = GameStore
        store = store_cls(storage_path=Path(tempfile.gettempdir()) / f"chat-test-{time.time_ns()}.json")
        store.create_or_replace_game(
            bonfire_id="bf1", owner_wallet="0xowner", game_prompt="test",
            gm_agent_id=None, initial_episode_summary="",
        )
        store.ensure_starting_room("bf1")
        room_map = store.get_room_map("bf1")
        room_id = room_map["rooms"][0]["room_id"]

        entry = store.append_room_message(
            room_id=room_id,
            sender_agent_id="agent-1",
            sender_wallet="0xwallet",
            role="user",
            text="Hello room!",
        )
        assert entry["room_id"] == room_id
        assert entry["role"] == "user"
        assert entry["text"] == "Hello room!"

        messages = store.get_room_messages(room_id, limit=10)
        assert len(messages) == 1
        assert messages[0]["sender_agent_id"] == "agent-1"

    def test_room_messages_limit(self) -> None:
        store_cls = GameStore
        store = store_cls(storage_path=Path(tempfile.gettempdir()) / f"chat-limit-{time.time_ns()}.json")
        store.create_or_replace_game(
            bonfire_id="bf1", owner_wallet="0xowner", game_prompt="test",
            gm_agent_id=None, initial_episode_summary="",
        )
        store.ensure_starting_room("bf1")
        room_map = store.get_room_map("bf1")
        room_id = room_map["rooms"][0]["room_id"]

        for i in range(10):
            store.append_room_message(room_id, "agent-1", "0xw", "user", f"msg-{i}")

        limited = store.get_room_messages(room_id, limit=3)
        assert len(limited) == 3
        assert limited[0]["text"] == "msg-7"

    def test_room_chat_persists(self) -> None:
        path = Path(tempfile.gettempdir()) / f"chat-persist-{time.time_ns()}.json"
        store_cls = GameStore
        store = store_cls(storage_path=path)
        store.create_or_replace_game(
            bonfire_id="bf1", owner_wallet="0xowner", game_prompt="test",
            gm_agent_id=None, initial_episode_summary="",
        )
        store.ensure_starting_room("bf1")
        room_map = store.get_room_map("bf1")
        room_id = room_map["rooms"][0]["room_id"]
        store.append_room_message(room_id, "a1", "0xw", "user", "persisted msg")

        store2 = store_cls(storage_path=path)
        messages = store2.get_room_messages(room_id, limit=50)
        assert len(messages) == 1
        assert messages[0]["text"] == "persisted msg"

    def test_empty_room_returns_no_messages(self) -> None:
        store_cls = GameStore
        store = store_cls(storage_path=Path(tempfile.gettempdir()) / f"chat-empty-{time.time_ns()}.json")
        messages = store.get_room_messages("nonexistent-room", limit=50)
        assert messages == []


# ---------------------------------------------------------------------------
# Room Chat Endpoint Tests
# ---------------------------------------------------------------------------


class TestRoomChatEndpoint:
    def test_get_room_chat(self, live_server) -> None:
        client, store = live_server
        _link_bonfire(client)
        store.create_or_replace_game(
            bonfire_id="bf1", owner_wallet="0xowner", game_prompt="test",
            gm_agent_id=None, initial_episode_summary="",
        )
        store.ensure_starting_room("bf1")
        room_map = store.get_room_map("bf1")
        room_id = room_map["rooms"][0]["room_id"]
        store.append_room_message(room_id, "agent-1", "0xw", "user", "test message")

        status, data = _get(client, f"/game/room/chat?room_id={room_id}&limit=10")
        assert status == 200
        assert data["room_id"] == room_id
        assert len(data["messages"]) == 1
        assert data["messages"][0]["text"] == "test message"

    def test_get_room_chat_missing_id(self, live_server) -> None:
        client, _store = live_server
        status, data = _get(client, "/game/room/chat")
        assert status == 422
        detail = data.get("detail", [])
        assert any("room_id" in str(e) for e in detail)

    def test_get_room_chat_empty(self, live_server) -> None:
        client, _store = live_server
        status, data = _get(client, "/game/room/chat?room_id=no-such-room")
        assert status == 200
        assert data["messages"] == []


# ---------------------------------------------------------------------------
# Room Update / GM Room CRUD Tests
# ---------------------------------------------------------------------------


class TestRoomCrud:
    def test_update_room_description(self) -> None:
        store_cls = GameStore
        store = store_cls(storage_path=Path(tempfile.gettempdir()) / f"crud-{time.time_ns()}.json")
        store.create_or_replace_game(
            bonfire_id="bf1", owner_wallet="0xowner", game_prompt="test",
            gm_agent_id=None, initial_episode_summary="",
        )
        room = store.create_room("bf1", "The Library", "Dusty shelves")

        updated = store.update_room("bf1", room.room_id, description="Freshly cleaned shelves")
        assert updated is True
        room_data = store.get_room_by_id("bf1", room.room_id)
        assert room_data is not None
        assert room_data["description"] == "Freshly cleaned shelves"

    def test_update_room_connections(self) -> None:
        store_cls = GameStore
        store = store_cls(storage_path=Path(tempfile.gettempdir()) / f"crud-conn-{time.time_ns()}.json")
        store.create_or_replace_game(
            bonfire_id="bf1", owner_wallet="0xowner", game_prompt="test",
            gm_agent_id=None, initial_episode_summary="",
        )
        room1 = store.create_room("bf1", "Room A")
        room2 = store.create_room("bf1", "Room B")

        store.update_room("bf1", room1.room_id, connections=[room2.room_id])
        room_data = store.get_room_by_id("bf1", room1.room_id)
        assert room_data is not None
        assert room2.room_id in room_data["connections"]

    def test_update_nonexistent_room(self) -> None:
        store_cls = GameStore
        store = store_cls(storage_path=Path(tempfile.gettempdir()) / f"crud-ne-{time.time_ns()}.json")
        store.create_or_replace_game(
            bonfire_id="bf1", owner_wallet="0xowner", game_prompt="test",
            gm_agent_id=None, initial_episode_summary="",
        )
        assert store.update_room("bf1", "fake-id", description="x") is False

    def test_apply_gm_room_changes_creates_rooms(self) -> None:
        store_cls = GameStore
        store = store_cls(storage_path=Path(tempfile.gettempdir()) / f"crud-apply-{time.time_ns()}.json")
        store.create_or_replace_game(
            bonfire_id="bf1", owner_wallet="0xowner", game_prompt="test",
            gm_agent_id=None, initial_episode_summary="",
        )
        store.ensure_starting_room("bf1")

        fn = gm_engine._apply_gm_room_changes
        decision: dict[str, object] = {
            "new_rooms": [
                {"name": "Secret Passage", "description": "A hidden way", "connections": []},
            ],
            "room_updates": [],
            "room_movements": [],
        }
        result = fn(store, "bf1", decision)
        created = result["new_rooms_created"]
        assert len(created) == 1
        assert created[0]["name"] == "Secret Passage"

        game = store.get_game("bf1")
        assert game is not None
        assert len(game.rooms) == 2

    def test_apply_gm_room_changes_updates_and_moves(self) -> None:
        store_cls = GameStore
        store = store_cls(storage_path=Path(tempfile.gettempdir()) / f"crud-umv-{time.time_ns()}.json")
        store.create_or_replace_game(
            bonfire_id="bf1", owner_wallet="0xowner", game_prompt="test",
            gm_agent_id=None, initial_episode_summary="",
        )
        store.ensure_starting_room("bf1")
        room2 = store.create_room("bf1", "Cavern", "Dark and damp")
        store.register_agent("0xwallet", "agent-1", "bf1", 1, 5, purchase_id="p1")
        store.place_player_in_starting_room("agent-1")

        fn = gm_engine._apply_gm_room_changes
        decision: dict[str, object] = {
            "new_rooms": [],
            "room_updates": [{"room_id": room2.room_id, "description": "Now lit by torches"}],
            "room_movements": [{"agent_id": "agent-1", "to_room": room2.room_id}],
        }
        result = fn(store, "bf1", decision)
        assert len(result["rooms_updated"]) == 1
        assert len(result["movements_applied"]) == 1

        updated = store.get_room_by_id("bf1", room2.room_id)
        assert updated is not None
        assert updated["description"] == "Now lit by torches"

        player = store.get_player("agent-1")
        assert player is not None
        assert player.current_room == room2.room_id


# ---------------------------------------------------------------------------
# Room Graph Entity Tests
# ---------------------------------------------------------------------------


class TestRoomGraphEntity:
    def test_set_room_graph_entity(self) -> None:
        store_cls = GameStore
        store = store_cls(storage_path=Path(tempfile.gettempdir()) / f"graph-{time.time_ns()}.json")
        store.create_or_replace_game(
            bonfire_id="bf1", owner_wallet="0xowner", game_prompt="test",
            gm_agent_id=None, initial_episode_summary="",
        )
        room = store.create_room("bf1", "The Forge")

        store.set_room_graph_entity("bf1", room.room_id, "entity-uuid-123")
        room_data = store.get_room_by_id("bf1", room.room_id)
        assert room_data is not None
        assert room_data["graph_entity_uuid"] == "entity-uuid-123"

    def test_graph_entity_uuid_in_room_state(self) -> None:
        room_state_cls = models.RoomState
        room = room_state_cls(room_id="r1", name="Test", graph_entity_uuid="uuid-1")
        assert room.graph_entity_uuid == "uuid-1"

    def test_room_chat_endpoint_completion_stores_messages(
        self, live_server, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Agent completion should store messages in room chat log."""
        client, store = live_server
        _link_bonfire(client)
        store.create_or_replace_game(
            bonfire_id="bf1", owner_wallet="0xowner", game_prompt="test",
            gm_agent_id=None, initial_episode_summary="",
        )
        store.ensure_starting_room("bf1")
        _register_purchase(client, agent_id="agent-1")
        store.place_player_in_starting_room("agent-1")

        room_map = store.get_room_map("bf1")
        room_id = room_map["rooms"][0]["room_id"]

        def fake_agent_json(method: str, url: str, api_key: str, body=None):
            if "/chat" in url and not url.endswith("/stack/add"):
                return 200, {"reply": "You see a warm hearth."}
            if url.endswith("/stack/add"):
                return 200, {"success": True}
            return 404, {}

        monkeypatch.setattr(http_client, "_agent_json_request", fake_agent_json)

        status, _data = _post(client, "/game/agents/complete", {
            "agent_id": "agent-1",
            "message": "Look around",
            "chat_id": "room-test",
            "user_id": "player-1",
        })
        assert status == 200

        messages = store.get_room_messages(room_id, limit=50)
        assert len(messages) == 2
        assert messages[0]["role"] == "user"
        assert messages[0]["text"] == "Look around"
        assert messages[1]["role"] == "agent"
        assert "warm hearth" in messages[1]["text"]


# ---------------------------------------------------------------------------
# Narrator Preamble Tests
# ---------------------------------------------------------------------------


class TestNarratorPreamble:
    def test_preamble_includes_narrator_role(
        self, live_server, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        client, store = live_server
        _link_bonfire(client)
        store.create_or_replace_game(
            bonfire_id="bf1", owner_wallet="0xowner", game_prompt="A dark fantasy world",
            gm_agent_id=None, initial_episode_summary="",
        )
        store.ensure_starting_room("bf1")
        _register_purchase(client, agent_id="agent-1")
        store.place_player_in_starting_room("agent-1")

        captured_messages: list[str] = []

        def fake_agent_json(method: str, url: str, api_key: str, body=None):
            if "/chat" in url and body and isinstance(body, dict):
                msg = str(body.get("message", ""))
                captured_messages.append(msg)
                return 200, {"reply": "The fire crackles softly."}
            if url.endswith("/stack/add"):
                return 200, {"success": True}
            return 404, {}

        monkeypatch.setattr(http_client, "_agent_json_request", fake_agent_json)

        _post(client, "/game/agents/complete", {
            "agent_id": "agent-1",
            "message": "What do I see?",
        })

        assert len(captured_messages) == 1
        preamble = captured_messages[0]
        assert "[NARRATOR ROLE]" in preamble
        assert "internal monologue" in preamble.lower()
        assert "[GAME WORLD]" in preamble
        assert "[CURRENT ROOM]" in preamble

    def test_preamble_includes_room_activity(
        self, live_server, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        client, store = live_server
        _link_bonfire(client)
        store.create_or_replace_game(
            bonfire_id="bf1", owner_wallet="0xowner", game_prompt="test",
            gm_agent_id=None, initial_episode_summary="",
        )
        store.ensure_starting_room("bf1")
        _register_purchase(client, agent_id="agent-1")
        _register_purchase(client, agent_id="agent-2", wallet_address="0xother")
        store.place_player_in_starting_room("agent-1")
        store.place_player_in_starting_room("agent-2")

        room_map = store.get_room_map("bf1")
        room_id = room_map["rooms"][0]["room_id"]
        store.append_room_message(room_id, "agent-2", "0xother", "user", "I search the chest")

        captured_messages: list[str] = []

        def fake_agent_json(method: str, url: str, api_key: str, body=None):
            if "/chat" in url and body and isinstance(body, dict):
                captured_messages.append(str(body.get("message", "")))
                return 200, {"reply": "ok"}
            if url.endswith("/stack/add"):
                return 200, {"success": True}
            return 404, {}

        monkeypatch.setattr(http_client, "_agent_json_request", fake_agent_json)

        _post(client, "/game/agents/complete", {
            "agent_id": "agent-1",
            "message": "test",
        })

        assert len(captured_messages) == 1
        assert "[ROOM ACTIVITY]" in captured_messages[0]
        assert "search the chest" in captured_messages[0]


# ---------------------------------------------------------------------------
# Room-Structured GM Summary Tests
# ---------------------------------------------------------------------------


class TestRoomStructuredSummary:
    def test_build_room_structured_summary(self) -> None:
        store_cls = GameStore
        store = store_cls(storage_path=Path(tempfile.gettempdir()) / f"summary-{time.time_ns()}.json")
        store.create_or_replace_game(
            bonfire_id="bf1", owner_wallet="0xowner", game_prompt="test",
            gm_agent_id=None, initial_episode_summary="",
        )
        store.ensure_starting_room("bf1")
        room2 = store.create_room("bf1", "The Dungeon", "A dark place")
        store.register_agent("0xw1", "agent-1", "bf1", 1, 5, purchase_id="p1")
        store.register_agent("0xw2", "agent-2", "bf1", 1, 5, purchase_id="p2")
        store.place_player_in_starting_room("agent-1")
        store.move_player("agent-2", room2.room_id)

        room_map = store.get_room_map("bf1")
        hearth_id = room_map["rooms"][0]["room_id"]
        store.append_room_message(hearth_id, "agent-1", "0xw1", "user", "I warm my hands")
        store.append_room_message(room2.room_id, "agent-2", "0xw2", "user", "I light a torch")

        fn = gm_engine._build_room_structured_summary
        summary = fn(store, "bf1")
        assert "The Hearth" in summary
        assert "The Dungeon" in summary
        assert "warm my hands" in summary
        assert "light a torch" in summary
        assert "agent-1" in summary
        assert "agent-2" in summary


# ---------------------------------------------------------------------------
# NPC System Tests
# ---------------------------------------------------------------------------


class TestNpcSystem:
    def _make_store(self) -> GameStore:
        store = GameStore(storage_path=Path(tempfile.gettempdir()) / f"npc-{time.time_ns()}.json")
        store.create_or_replace_game(
            bonfire_id="bf1", owner_wallet="0xowner", game_prompt="test",
            gm_agent_id=None, initial_episode_summary="",
        )
        store.ensure_starting_room("bf1")
        return store

    def test_create_npc(self) -> None:
        store = self._make_store()
        room_map = store.get_room_map("bf1")
        room_id = room_map["rooms"][0]["room_id"]
        npc = store.create_npc("bf1", "Thorn", room_id, "gruff blacksmith", description="A muscular figure")
        assert npc.npc_id
        assert npc.name == "Thorn"
        assert npc.room_id == room_id
        assert npc.personality == "gruff blacksmith"

    def test_get_npcs_in_room(self) -> None:
        store = self._make_store()
        room_map = store.get_room_map("bf1")
        room_id = room_map["rooms"][0]["room_id"]
        store.create_npc("bf1", "Thorn", room_id, "blacksmith")
        store.create_npc("bf1", "Elara", room_id, "healer")
        room2 = store.create_room("bf1", "The Dungeon")
        store.create_npc("bf1", "Goblin", room2.room_id, "hostile")

        npcs_in_room = store.get_npcs_in_room("bf1", room_id)
        assert len(npcs_in_room) == 2
        names = {n.name for n in npcs_in_room}
        assert names == {"Thorn", "Elara"}

    def test_update_npc_room(self) -> None:
        store = self._make_store()
        room_map = store.get_room_map("bf1")
        room_id = room_map["rooms"][0]["room_id"]
        room2 = store.create_room("bf1", "The Forge")
        npc = store.create_npc("bf1", "Thorn", room_id, "blacksmith")

        assert store.update_npc("bf1", npc.npc_id, room_id=room2.room_id)
        updated = store.get_npc("bf1", npc.npc_id)
        assert updated is not None
        assert updated.room_id == room2.room_id

    def test_remove_npc(self) -> None:
        store = self._make_store()
        room_map = store.get_room_map("bf1")
        room_id = room_map["rooms"][0]["room_id"]
        npc = store.create_npc("bf1", "Thorn", room_id, "blacksmith")

        assert store.remove_npc("bf1", npc.npc_id)
        assert len(store.get_npcs_in_room("bf1", room_id)) == 0

    def test_npc_persistence(self) -> None:
        path = Path(tempfile.gettempdir()) / f"npc-persist-{time.time_ns()}.json"
        store_cls = GameStore
        store = store_cls(storage_path=path)
        store.create_or_replace_game(
            bonfire_id="bf1", owner_wallet="0xowner", game_prompt="test",
            gm_agent_id=None, initial_episode_summary="",
        )
        store.ensure_starting_room("bf1")
        room_map = store.get_room_map("bf1")
        room_id = room_map["rooms"][0]["room_id"]
        npc = store.create_npc("bf1", "Thorn", room_id, "blacksmith", description="Forge master")

        store2 = store_cls(storage_path=path)
        loaded = store2.get_npc("bf1", npc.npc_id)
        assert loaded is not None
        assert loaded.name == "Thorn"
        assert loaded.description == "Forge master"

    def test_npc_in_room_map(self) -> None:
        store = self._make_store()
        room_map = store.get_room_map("bf1")
        room_id = room_map["rooms"][0]["room_id"]
        store.create_npc("bf1", "Thorn", room_id, "blacksmith")

        full_map = store.get_room_map("bf1")
        assert "npcs_by_room" in full_map
        npc_map = full_map["npcs_by_room"]
        assert room_id in npc_map
        assert len(npc_map[room_id]) == 1
        assert npc_map[room_id][0]["name"] == "Thorn"

    def test_npc_list_endpoint(self, live_server) -> None:
        client, store = live_server
        _link_bonfire(client)
        store.create_or_replace_game(
            bonfire_id="bf1", owner_wallet="0xowner", game_prompt="test",
            gm_agent_id=None, initial_episode_summary="",
        )
        store.ensure_starting_room("bf1")
        room_map = store.get_room_map("bf1")
        room_id = room_map["rooms"][0]["room_id"]
        store.create_npc("bf1", "Thorn", room_id, "blacksmith")

        status, data = _get(client, f"/game/room/npcs?bonfire_id=bf1&room_id={room_id}")
        assert status == 200
        assert len(data["npcs"]) == 1
        assert data["npcs"][0]["name"] == "Thorn"


# ---------------------------------------------------------------------------
# Object / Inventory System Tests
# ---------------------------------------------------------------------------


class TestObjectSystem:
    def _make_store(self) -> GameStore:
        store = GameStore(storage_path=Path(tempfile.gettempdir()) / f"obj-{time.time_ns()}.json")
        store.create_or_replace_game(
            bonfire_id="bf1", owner_wallet="0xowner", game_prompt="test",
            gm_agent_id=None, initial_episode_summary="",
        )
        store.ensure_starting_room("bf1")
        return store

    def test_create_object_in_room(self) -> None:
        store = self._make_store()
        room_map = store.get_room_map("bf1")
        room_id = room_map["rooms"][0]["room_id"]
        obj = store.create_object(
            "bf1", "Iron Key", "Opens the dungeon gate", "key",
            properties={"unlocks_room": "dungeon-id", "location_type": "room", "location_id": room_id},
        )
        assert obj.object_id
        assert obj.name == "Iron Key"
        assert obj.obj_type == "key"

        room_objects = store.get_objects_in_room("bf1", room_id)
        assert len(room_objects) == 1
        assert room_objects[0].name == "Iron Key"

    def test_grant_object_to_player(self) -> None:
        store = self._make_store()
        room_map = store.get_room_map("bf1")
        room_id = room_map["rooms"][0]["room_id"]
        store.register_agent("0xw1", "agent-1", "bf1", 1, 5, purchase_id="p1")
        store.place_player_in_starting_room("agent-1")

        obj = store.create_object(
            "bf1", "Healing Potion", "Restores health", "consumable",
            properties={"location_type": "room", "location_id": room_id},
        )
        assert store.grant_object_to_player("bf1", "agent-1", obj.object_id)

        inv = store.get_player_inventory("bf1", "agent-1")
        assert len(inv) == 1
        assert inv[0]["name"] == "Healing Potion"

        assert store.get_objects_in_room("bf1", room_id) == []

    def test_grant_object_to_npc(self) -> None:
        store = self._make_store()
        room_map = store.get_room_map("bf1")
        room_id = room_map["rooms"][0]["room_id"]
        npc = store.create_npc("bf1", "Merchant", room_id, "trader")
        obj = store.create_object(
            "bf1", "Magic Scroll", "A mysterious scroll", "artifact",
            properties={"location_type": "room", "location_id": room_id},
        )
        assert store.grant_object_to_npc("bf1", npc.npc_id, obj.object_id)

        loaded_npc = store.get_npc("bf1", npc.npc_id)
        assert loaded_npc is not None
        assert obj.object_id in loaded_npc.inventory

    def test_drop_object_in_room(self) -> None:
        store = self._make_store()
        room_map = store.get_room_map("bf1")
        room_id = room_map["rooms"][0]["room_id"]
        store.register_agent("0xw1", "agent-1", "bf1", 1, 5, purchase_id="p1")
        store.place_player_in_starting_room("agent-1")

        obj = store.create_object(
            "bf1", "Sword", "A sharp blade", "tool",
            properties={"location_type": "player", "location_id": "agent-1"},
        )
        store.grant_object_to_player("bf1", "agent-1", obj.object_id)
        assert len(store.get_player_inventory("bf1", "agent-1")) == 1

        assert store.drop_object_in_room("bf1", room_id, obj.object_id)
        assert len(store.get_player_inventory("bf1", "agent-1")) == 0
        assert len(store.get_objects_in_room("bf1", room_id)) == 1

    def test_use_consumable(self) -> None:
        store = self._make_store()
        store.register_agent("0xw1", "agent-1", "bf1", 1, 5, purchase_id="p1")
        store.place_player_in_starting_room("agent-1")

        obj = store.create_object("bf1", "Potion", "Heal", "consumable")
        store.grant_object_to_player("bf1", "agent-1", obj.object_id)
        result = store.use_object("bf1", "agent-1", obj.object_id)
        assert result["success"]
        assert "Item consumed" in result["effects"]
        assert len(store.get_player_inventory("bf1", "agent-1")) == 0

    def test_use_key_unlocks_room(self) -> None:
        store = self._make_store()
        room_map = store.get_room_map("bf1")
        room_id = room_map["rooms"][0]["room_id"]
        room2 = store.create_room("bf1", "Secret Chamber")
        store.register_agent("0xw1", "agent-1", "bf1", 1, 5, purchase_id="p1")
        store.place_player_in_starting_room("agent-1")

        obj = store.create_object(
            "bf1", "Skeleton Key", "Unlocks the secret chamber", "key",
            properties={"unlocks_room": room2.room_id},
        )
        store.grant_object_to_player("bf1", "agent-1", obj.object_id)
        result = store.use_object("bf1", "agent-1", obj.object_id)
        assert result["success"]
        assert any("Unlocked" in str(e) for e in result["effects"])

        updated_room = store.get_room_by_id("bf1", room_id)
        assert updated_room is not None
        assert room2.room_id in updated_room.get("connections", [])

    def test_object_persistence(self) -> None:
        path = Path(tempfile.gettempdir()) / f"obj-persist-{time.time_ns()}.json"
        store_cls = GameStore
        store = store_cls(storage_path=path)
        store.create_or_replace_game(
            bonfire_id="bf1", owner_wallet="0xowner", game_prompt="test",
            gm_agent_id=None, initial_episode_summary="",
        )
        room_map = store.get_room_map("bf1")
        room_id = room_map["rooms"][0]["room_id"] if room_map["rooms"] else ""
        if not room_id:
            store.ensure_starting_room("bf1")
            room_map = store.get_room_map("bf1")
            room_id = room_map["rooms"][0]["room_id"]

        obj = store.create_object(
            "bf1", "Ancient Tome", "Contains forgotten knowledge", "artifact",
            properties={"location_type": "room", "location_id": room_id},
        )

        store2 = store_cls(storage_path=path)
        loaded = store2.get_object("bf1", obj.object_id)
        assert loaded is not None
        assert loaded.name == "Ancient Tome"

    def test_inventory_endpoint(self, live_server) -> None:
        client, store = live_server
        _link_bonfire(client)
        store.create_or_replace_game(
            bonfire_id="bf1", owner_wallet="0xowner", game_prompt="test",
            gm_agent_id=None, initial_episode_summary="",
        )
        store.ensure_starting_room("bf1")
        _register_purchase(client, agent_id="agent-1")
        store.place_player_in_starting_room("agent-1")

        obj = store.create_object("bf1", "Shield", "Blocks attacks", "tool")
        store.grant_object_to_player("bf1", "agent-1", obj.object_id)

        status, data = _get(client, "/game/inventory?agent_id=agent-1&bonfire_id=bf1")
        assert status == 200
        assert len(data["items"]) == 1
        assert data["items"][0]["name"] == "Shield"

    def test_inventory_use_endpoint(self, live_server) -> None:
        client, store = live_server
        _link_bonfire(client)
        store.create_or_replace_game(
            bonfire_id="bf1", owner_wallet="0xowner", game_prompt="test",
            gm_agent_id=None, initial_episode_summary="",
        )
        store.ensure_starting_room("bf1")
        _register_purchase(client, agent_id="agent-1")
        store.place_player_in_starting_room("agent-1")

        obj = store.create_object("bf1", "Potion", "Heal", "consumable")
        store.grant_object_to_player("bf1", "agent-1", obj.object_id)

        status, data = _post(client, "/game/inventory/use", {
            "agent_id": "agent-1", "object_id": obj.object_id,
        })
        assert status == 200
        assert data["success"]

    def test_objects_in_map(self) -> None:
        store = self._make_store()
        room_map = store.get_room_map("bf1")
        room_id = room_map["rooms"][0]["room_id"]
        store.create_object(
            "bf1", "Gem", "Sparkly", "artifact",
            properties={"location_type": "room", "location_id": room_id},
        )
        full_map = store.get_room_map("bf1")
        assert "objects_by_room" in full_map
        obj_map = full_map["objects_by_room"]
        assert room_id in obj_map
        assert len(obj_map[room_id]) == 1
        assert obj_map[room_id][0]["name"] == "Gem"


# ---------------------------------------------------------------------------
# GM NPC + Object Decision Tests
# ---------------------------------------------------------------------------


class TestGmNpcObjectDecisions:
    def _make_store(self) -> GameStore:
        store = GameStore(storage_path=Path(tempfile.gettempdir()) / f"gm-npc-{time.time_ns()}.json")
        store.create_or_replace_game(
            bonfire_id="bf1", owner_wallet="0xowner", game_prompt="test",
            gm_agent_id=None, initial_episode_summary="",
        )
        store.ensure_starting_room("bf1")
        return store

    def test_apply_new_npcs(self) -> None:
        store = self._make_store()
        room_map = store.get_room_map("bf1")
        room_id = room_map["rooms"][0]["room_id"]
        fn = gm_engine._apply_gm_npc_and_object_changes
        result = fn(store, "bf1", {
            "new_npcs": [{"name": "Guard", "room_id": room_id, "personality": "stern", "description": "Armed"}],
        })
        assert len(result["npcs_created"]) == 1
        assert result["npcs_created"][0]["name"] == "Guard"
        npcs = store.get_npcs_in_room("bf1", room_id)
        assert len(npcs) == 1

    def test_apply_new_objects_in_room(self) -> None:
        store = self._make_store()
        room_map = store.get_room_map("bf1")
        room_id = room_map["rooms"][0]["room_id"]
        fn = gm_engine._apply_gm_npc_and_object_changes
        result = fn(store, "bf1", {
            "new_objects": [{
                "name": "Ruby", "description": "A glowing gem", "obj_type": "artifact",
                "location_type": "room", "location_id": room_id, "properties": {},
            }],
        })
        assert len(result["objects_created"]) == 1
        objs = store.get_objects_in_room("bf1", room_id)
        assert len(objs) == 1

    def test_apply_object_grants(self) -> None:
        store = self._make_store()
        store.register_agent("0xw1", "agent-1", "bf1", 1, 5, purchase_id="p1")
        obj = store.create_object("bf1", "Amulet", "Protects wearer", "artifact")
        fn = gm_engine._apply_gm_npc_and_object_changes
        result = fn(store, "bf1", {
            "object_grants": [{"object_id": obj.object_id, "to_agent_id": "agent-1"}],
        })
        assert len(result["objects_granted"]) == 1
        inv = store.get_player_inventory("bf1", "agent-1")
        assert len(inv) == 1

    def test_apply_npc_updates(self) -> None:
        store = self._make_store()
        room_map = store.get_room_map("bf1")
        room_id = room_map["rooms"][0]["room_id"]
        room2 = store.create_room("bf1", "Cellar")
        npc = store.create_npc("bf1", "Rat", room_id, "sneaky")

        fn = gm_engine._apply_gm_npc_and_object_changes
        result = fn(store, "bf1", {
            "npc_updates": [{"npc_id": npc.npc_id, "room_id": room2.room_id}],
        })
        assert len(result["npcs_moved"]) == 1
        updated = store.get_npc("bf1", npc.npc_id)
        assert updated is not None
        assert updated.room_id == room2.room_id


# ---------------------------------------------------------------------------
# NPC Interaction Tests
# ---------------------------------------------------------------------------


class TestNpcInteraction:
    def test_npc_interact_endpoint(
        self, live_server, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        client, store = live_server
        _link_bonfire(client)
        store.create_or_replace_game(
            bonfire_id="bf1", owner_wallet="0xowner", game_prompt="test",
            gm_agent_id="agent-gm", initial_episode_summary="",
        )
        store.ensure_starting_room("bf1")
        _register_purchase(client, agent_id="agent-1")
        store.place_player_in_starting_room("agent-1")
        store.game_admin_by_bonfire["bf1"]["agent_id"] = "agent-gm"
        room_map = store.get_room_map("bf1")
        room_id = room_map["rooms"][0]["room_id"]
        npc = store.create_npc("bf1", "Oracle", room_id, "mysterious seer")

        status, data = _post(client, "/game/npc/interact", {
            "agent_id": "agent-1", "npc_id": npc.npc_id, "message": "Hello, Oracle!",
        })
        assert status == 200
        assert data["npc_name"] == "Oracle"
        assert "reply" in data

    def test_npc_interact_stores_room_message(
        self, live_server, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        client, store = live_server
        _link_bonfire(client)
        store.create_or_replace_game(
            bonfire_id="bf1", owner_wallet="0xowner", game_prompt="test",
            gm_agent_id="agent-gm", initial_episode_summary="",
        )
        store.ensure_starting_room("bf1")
        _register_purchase(client, agent_id="agent-1")
        store.place_player_in_starting_room("agent-1")
        store.game_admin_by_bonfire["bf1"]["agent_id"] = "agent-gm"
        room_map = store.get_room_map("bf1")
        room_id = room_map["rooms"][0]["room_id"]
        npc = store.create_npc("bf1", "Oracle", room_id, "seer")

        _post(client, "/game/npc/interact", {
            "agent_id": "agent-1", "npc_id": npc.npc_id, "message": "What is my fate?",
        })

        msgs = store.get_room_messages(room_id, limit=10)
        npc_msgs = [m for m in msgs if m.get("role") == "npc"]
        assert len(npc_msgs) >= 1
        assert "[Oracle]" in str(npc_msgs[0].get("text", ""))

    def test_npc_interact_not_found(self, live_server) -> None:
        client, store = live_server
        _link_bonfire(client)
        store.create_or_replace_game(
            bonfire_id="bf1", owner_wallet="0xowner", game_prompt="test",
            gm_agent_id="agent-gm", initial_episode_summary="",
        )
        _register_purchase(client, agent_id="agent-1")

        status, data = _post(client, "/game/npc/interact", {
            "agent_id": "agent-1", "npc_id": "nonexistent", "message": "Hello",
        })
        assert status == 404
        assert data["error"] == "npc_not_found"


# ---------------------------------------------------------------------------
# Inventory Preamble Tests
# ---------------------------------------------------------------------------


class TestInventoryPreamble:
    def test_preamble_includes_room_npcs(
        self, live_server, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        client, store = live_server
        _link_bonfire(client)
        store.create_or_replace_game(
            bonfire_id="bf1", owner_wallet="0xowner", game_prompt="A grand adventure",
            gm_agent_id=None, initial_episode_summary="",
        )
        store.ensure_starting_room("bf1")
        _register_purchase(client, agent_id="agent-1")
        store.place_player_in_starting_room("agent-1")
        room_map = store.get_room_map("bf1")
        room_id = room_map["rooms"][0]["room_id"]
        store.create_npc("bf1", "Blacksmith", room_id, "gruff but kind")

        captured: list[str] = []

        def fake_agent_json(method: str, url: str, api_key: str, body=None):
            if "/chat" in url and body and isinstance(body, dict):
                captured.append(str(body.get("message", "")))
                return 200, {"reply": "narrator says hi"}
            if url.endswith("/stack/add"):
                return 200, {"success": True}
            return 404, {}

        monkeypatch.setattr(http_client, "_agent_json_request", fake_agent_json)
        _post(client, "/game/agents/complete", {
            "agent_id": "agent-1", "message": "Look around",
        })

        assert len(captured) >= 1
        preamble = captured[0]
        assert "[ROOM NPCS]" in preamble
        assert "Blacksmith" in preamble

    def test_preamble_includes_inventory(
        self, live_server, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        client, store = live_server
        _link_bonfire(client)
        store.create_or_replace_game(
            bonfire_id="bf1", owner_wallet="0xowner", game_prompt="A grand adventure",
            gm_agent_id=None, initial_episode_summary="",
        )
        store.ensure_starting_room("bf1")
        _register_purchase(client, agent_id="agent-1")
        store.place_player_in_starting_room("agent-1")
        obj = store.create_object("bf1", "Magic Sword", "Glows blue", "tool")
        store.grant_object_to_player("bf1", "agent-1", obj.object_id)

        captured: list[str] = []

        def fake_agent_json(method: str, url: str, api_key: str, body=None):
            if "/chat" in url and body and isinstance(body, dict):
                captured.append(str(body.get("message", "")))
                return 200, {"reply": "narrator says hi"}
            if url.endswith("/stack/add"):
                return 200, {"success": True}
            return 404, {}

        monkeypatch.setattr(http_client, "_agent_json_request", fake_agent_json)
        _post(client, "/game/agents/complete", {
            "agent_id": "agent-1", "message": "Check bag",
        })

        assert len(captured) >= 1
        preamble = captured[0]
        assert "[YOUR INVENTORY]" in preamble
        assert "Magic Sword" in preamble

    def test_preamble_includes_room_items(
        self, live_server, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        client, store = live_server
        _link_bonfire(client)
        store.create_or_replace_game(
            bonfire_id="bf1", owner_wallet="0xowner", game_prompt="A grand adventure",
            gm_agent_id=None, initial_episode_summary="",
        )
        store.ensure_starting_room("bf1")
        _register_purchase(client, agent_id="agent-1")
        store.place_player_in_starting_room("agent-1")
        room_map = store.get_room_map("bf1")
        room_id = room_map["rooms"][0]["room_id"]
        store.create_object(
            "bf1", "Gold Coin", "Shiny", "artifact",
            properties={"location_type": "room", "location_id": room_id},
        )

        captured: list[str] = []

        def fake_agent_json(method: str, url: str, api_key: str, body=None):
            if "/chat" in url and body and isinstance(body, dict):
                captured.append(str(body.get("message", "")))
                return 200, {"reply": "narrator says hi"}
            if url.endswith("/stack/add"):
                return 200, {"success": True}
            return 404, {}

        monkeypatch.setattr(http_client, "_agent_json_request", fake_agent_json)
        _post(client, "/game/agents/complete", {
            "agent_id": "agent-1", "message": "Look at floor",
        })

        assert len(captured) >= 1
        preamble = captured[0]
        assert "[ROOM ITEMS]" in preamble
        assert "Gold Coin" in preamble
