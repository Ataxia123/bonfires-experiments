#!/usr/bin/env python3
"""Shared-bonfire quest game demo server.

This demo intentionally keeps storage in-memory so it can run quickly for local
experiments. It integrates with existing Delve endpoints for purchased-agent
reveal flow and resolves ERC-8004 ownership to gate Game Master actions.
"""

from __future__ import annotations

import http.server
import json
import os
import socketserver
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
import asyncio
import importlib
from collections.abc import Mapping
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime, timedelta
from decimal import InvalidOperation
from pathlib import Path
from typing import Callable

from dotenv import load_dotenv

load_dotenv(Path(__file__).parent / ".env")

GAME_DIR = Path(__file__).parent
PORT = int(os.environ.get("PORT", "9997"))
GAME_STORE_PATH = Path(os.environ.get("GAME_STORE_PATH", str(GAME_DIR / "game_store.json")))
DELVE_BASE_URL = os.environ.get("DELVE_BASE_URL", "http://localhost:8000").rstrip("/")
DELVE_API_KEY = os.environ.get("DELVE_API_KEY", "").strip()
ERC8004_REGISTRY_ADDRESS = os.environ.get(
    "ERC8004_REGISTRY_ADDRESS",
    "0x8004A169FB4a3325136EB29fA0ceB6D2e539a432",
).strip()
PAYMENT_NETWORK = os.environ.get("PAYMENT_NETWORK", "base").strip()
PAYMENT_SOURCE_NETWORK = os.environ.get("PAYMENT_SOURCE_NETWORK", PAYMENT_NETWORK).strip()
PAYMENT_DESTINATION_NETWORK = os.environ.get("PAYMENT_DESTINATION_NETWORK", PAYMENT_NETWORK).strip()
ONCHAINFI_INTERMEDIARY_ADDRESS = os.environ.get("ONCHAINFI_INTERMEDIARY_ADDRESS", "").strip()
PAYMENT_TOKEN_ADDRESS = os.environ.get(
    "PAYMENT_TOKEN_ADDRESS",
    "0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913",
).strip()
PAYMENT_CHAIN_ID = int(os.environ.get("PAYMENT_CHAIN_ID", "8453"))
PAYMENT_DEFAULT_AMOUNT = os.environ.get("PAYMENT_DEFAULT_AMOUNT", "0.01").strip()
DEFAULT_CLAIM_COOLDOWN_SECONDS = int(os.environ.get("QUEST_CLAIM_COOLDOWN_SECONDS", "60"))
STACK_PROCESS_INTERVAL_SECONDS = int(os.environ.get("STACK_PROCESS_INTERVAL_SECONDS", "120"))
GM_BATCH_INTERVAL_SECONDS = int(os.environ.get("GM_BATCH_INTERVAL_SECONDS", "900"))


@dataclass
class PlayerState:
    wallet: str
    agent_id: str
    bonfire_id: str
    erc8004_bonfire_id: int
    purchase_id: str
    purchase_tx_hash: str
    base_quota: int
    bonus_quota: int = 0
    turns_used: int = 0
    is_active: bool = True
    created_at: str = field(default_factory=lambda: datetime.now(UTC).isoformat())
    current_room: str = ""

    @property
    def remaining_episodes(self) -> int:
        return max(self.base_quota + self.bonus_quota - self.turns_used, 0)

    @property
    def total_quota(self) -> int:
        return self.base_quota + self.bonus_quota


@dataclass
class QuestState:
    quest_id: str
    bonfire_id: str
    creator_wallet: str
    quest_type: str
    prompt: str
    keyword: str
    reward: int
    cooldown_seconds: int
    status: str = "active"
    created_at: str = field(default_factory=lambda: datetime.now(UTC).isoformat())
    expires_at: str | None = None


@dataclass
class AttemptState:
    quest_id: str
    agent_id: str
    submission: str
    verdict: str
    reward_granted: int
    created_at: str = field(default_factory=lambda: datetime.now(UTC).isoformat())


@dataclass
class RoomState:
    room_id: str
    name: str
    description: str = ""
    connections: list[str] = field(default_factory=list)
    graph_entity_uuid: str = ""


@dataclass
class GameState:
    bonfire_id: str
    owner_wallet: str
    game_prompt: str
    status: str = "active"
    game_id: str = field(default_factory=lambda: str(uuid.uuid4()))
    created_at: str = field(default_factory=lambda: datetime.now(UTC).isoformat())
    updated_at: str = field(default_factory=lambda: datetime.now(UTC).isoformat())
    archived_at: str | None = None
    gm_agent_id: str | None = None
    initial_episode_summary: str = ""
    world_state_summary: str = ""
    last_gm_reaction: str = ""
    last_episode_id: str = ""
    rooms: list[dict[str, object]] = field(default_factory=list)


class GameStore:
    """In-memory game store and business rules."""

    def __init__(self, storage_path: Path | None = None) -> None:
        self._lock = threading.Lock()
        self._storage_path = Path(storage_path or GAME_STORE_PATH)
        self.players_by_agent: dict[str, PlayerState] = {}
        self.players_by_purchase: dict[str, PlayerState] = {}
        self.players_by_wallet: dict[str, list[str]] = {}
        self.game_admin_by_bonfire: dict[str, dict[str, str]] = {}
        self.games_by_bonfire: dict[str, GameState] = {}
        self.quests_by_bonfire: dict[str, dict[str, QuestState]] = {}
        self.attempts: list[AttemptState] = []
        self.claimed_by_quest: dict[str, set[str]] = {}
        self.last_claim_at: dict[str, datetime] = {}
        self.events_by_bonfire: dict[str, list[dict[str, object]]] = {}
        self.ledger_by_agent: dict[str, list[dict[str, object]]] = {}
        self.agent_context_by_agent: dict[str, dict[str, object]] = {}
        self.room_chat_by_room: dict[str, list[dict[str, object]]] = {}
        self._load_from_disk()

    def _snapshot_locked(self) -> dict[str, object]:
        return {
            "players": [asdict(player) for player in self.players_by_agent.values()],
            "game_admin_by_bonfire": self.game_admin_by_bonfire,
            "games": [asdict(game) for game in self.games_by_bonfire.values()],
            "quests_by_bonfire": {
                bonfire_id: {quest_id: asdict(quest) for quest_id, quest in quests.items()}
                for bonfire_id, quests in self.quests_by_bonfire.items()
            },
            "attempts": [asdict(attempt) for attempt in self.attempts],
            "claimed_by_quest": {quest_id: sorted(agent_ids) for quest_id, agent_ids in self.claimed_by_quest.items()},
            "last_claim_at": {
                key: claimed_at.isoformat() for key, claimed_at in self.last_claim_at.items() if isinstance(claimed_at, datetime)
            },
            "events_by_bonfire": self.events_by_bonfire,
            "ledger_by_agent": self.ledger_by_agent,
            "agent_context_by_agent": self.agent_context_by_agent,
            "room_chat_by_room": {k: v[-200:] for k, v in self.room_chat_by_room.items()},
        }

    def _persist_locked(self) -> None:
        self._storage_path.parent.mkdir(parents=True, exist_ok=True)
        temp_path = self._storage_path.with_suffix(f"{self._storage_path.suffix}.tmp")
        temp_path.write_text(json.dumps(self._snapshot_locked(), indent=2), encoding="utf-8")
        temp_path.replace(self._storage_path)

    def _load_from_disk(self) -> None:
        if not self._storage_path.exists():
            return
        try:
            payload = json.loads(self._storage_path.read_text(encoding="utf-8"))
        except Exception:
            return
        if not isinstance(payload, dict):
            return

        players_obj = payload.get("players")
        if isinstance(players_obj, list):
            for player_obj in players_obj:
                if not isinstance(player_obj, dict):
                    continue
                try:
                    player = PlayerState(**player_obj)
                except TypeError:
                    continue
                self.players_by_agent[player.agent_id] = player
                if player.purchase_id:
                    self.players_by_purchase[player.purchase_id] = player
                self.players_by_wallet.setdefault(player.wallet, [])
                if player.agent_id not in self.players_by_wallet[player.wallet]:
                    self.players_by_wallet[player.wallet].append(player.agent_id)
                self.ledger_by_agent.setdefault(player.agent_id, [])

        admins_obj = payload.get("game_admin_by_bonfire")
        if isinstance(admins_obj, dict):
            self.game_admin_by_bonfire = {
                str(k): dict(v)
                for k, v in admins_obj.items()
                if isinstance(v, dict)
            }

        games_obj = payload.get("games")
        if isinstance(games_obj, list):
            for game_obj in games_obj:
                if not isinstance(game_obj, dict):
                    continue
                try:
                    game = GameState(**game_obj)
                except TypeError:
                    continue
                self.games_by_bonfire[game.bonfire_id] = game

        quests_obj = payload.get("quests_by_bonfire")
        if isinstance(quests_obj, dict):
            loaded_quests: dict[str, dict[str, QuestState]] = {}
            for bonfire_id, quest_map_obj in quests_obj.items():
                if not isinstance(quest_map_obj, dict):
                    continue
                loaded_quests[str(bonfire_id)] = {}
                for quest_id, quest_obj in quest_map_obj.items():
                    if not isinstance(quest_obj, dict):
                        continue
                    try:
                        quest = QuestState(**quest_obj)
                    except TypeError:
                        continue
                    loaded_quests[str(bonfire_id)][str(quest_id)] = quest
            self.quests_by_bonfire = loaded_quests

        attempts_obj = payload.get("attempts")
        if isinstance(attempts_obj, list):
            loaded_attempts: list[AttemptState] = []
            for attempt_obj in attempts_obj:
                if not isinstance(attempt_obj, dict):
                    continue
                try:
                    loaded_attempts.append(AttemptState(**attempt_obj))
                except TypeError:
                    continue
            self.attempts = loaded_attempts

        claimed_obj = payload.get("claimed_by_quest")
        if isinstance(claimed_obj, dict):
            self.claimed_by_quest = {
                str(quest_id): {str(agent_id) for agent_id in agent_ids if isinstance(agent_id, str)}
                for quest_id, agent_ids in claimed_obj.items()
                if isinstance(agent_ids, list)
            }

        last_claim_obj = payload.get("last_claim_at")
        if isinstance(last_claim_obj, dict):
            parsed_last_claim: dict[str, datetime] = {}
            for key, value in last_claim_obj.items():
                if not isinstance(key, str) or not isinstance(value, str):
                    continue
                try:
                    parsed_last_claim[key] = datetime.fromisoformat(value)
                except ValueError:
                    continue
            self.last_claim_at = parsed_last_claim

        events_obj = payload.get("events_by_bonfire")
        if isinstance(events_obj, dict):
            self.events_by_bonfire = {
                str(k): list(v) for k, v in events_obj.items() if isinstance(v, list)
            }

        ledger_obj = payload.get("ledger_by_agent")
        if isinstance(ledger_obj, dict):
            self.ledger_by_agent = {
                str(k): list(v) for k, v in ledger_obj.items() if isinstance(v, list)
            }

        context_obj = payload.get("agent_context_by_agent")
        if isinstance(context_obj, dict):
            self.agent_context_by_agent = {
                str(k): dict(v) for k, v in context_obj.items() if isinstance(v, dict)
            }

        room_chat_obj = payload.get("room_chat_by_room")
        if isinstance(room_chat_obj, dict):
            self.room_chat_by_room = {
                str(k): list(v) for k, v in room_chat_obj.items() if isinstance(v, list)
            }

        self._migrate_rooms()

    def _migrate_rooms(self) -> None:
        """Seed a starting room for any active game that has no rooms."""
        dirty = False
        for game in self.games_by_bonfire.values():
            if game.status != "active":
                continue
            if game.rooms:
                continue
            room = RoomState(
                room_id=str(uuid.uuid4()),
                name="The Hearth",
                description="A warm gathering place where all adventurers begin their journey.",
            )
            game.rooms.append(asdict(room))
            game.updated_at = datetime.now(UTC).isoformat()
            dirty = True
        if dirty:
            for player in self.players_by_agent.values():
                if player.current_room:
                    continue
                game = self.games_by_bonfire.get(player.bonfire_id)
                if not game or not game.rooms:
                    continue
                first = game.rooms[0]
                if isinstance(first, dict) and "room_id" in first:
                    player.current_room = str(first["room_id"])
            self._persist_locked()

    def _append_event(self, bonfire_id: str, event_type: str, payload: dict[str, object]) -> None:
        events = self.events_by_bonfire.setdefault(bonfire_id, [])
        events.append(
            {
                "event_id": str(uuid.uuid4()),
                "event_type": event_type,
                "at": datetime.now(UTC).isoformat(),
                "payload": payload,
            }
        )
        if len(events) > 500:
            del events[: len(events) - 500]
        self._persist_locked()

    def link_bonfire(
        self,
        bonfire_id: str,
        erc8004_bonfire_id: int,
        owner_wallet: str,
    ) -> dict[str, str | int]:
        with self._lock:
            self.game_admin_by_bonfire[bonfire_id] = {
                "bonfire_id": bonfire_id,
                "erc8004_bonfire_id": str(erc8004_bonfire_id),
                "owner_wallet": owner_wallet.lower(),
                "last_verified_at": datetime.now(UTC).isoformat(),
            }
            self._append_event(
                bonfire_id,
                "bonfire_linked",
                {
                    "erc8004_bonfire_id": erc8004_bonfire_id,
                    "owner_wallet": owner_wallet.lower(),
                },
            )
            return {
                "bonfire_id": bonfire_id,
                "erc8004_bonfire_id": erc8004_bonfire_id,
                "owner_wallet": owner_wallet.lower(),
            }

    def register_agent(
        self,
        wallet: str,
        agent_id: str,
        bonfire_id: str,
        erc8004_bonfire_id: int,
        episodes_purchased: int,
        purchase_id: str = "",
        purchase_tx_hash: str = "",
    ) -> PlayerState:
        with self._lock:
            wallet_normalized = wallet.lower()
            existing_by_agent = self.players_by_agent.get(agent_id)
            if existing_by_agent:
                if existing_by_agent.wallet != wallet_normalized:
                    raise ValueError("agent_id already belongs to a different wallet")
                return existing_by_agent

            if purchase_id:
                existing_by_purchase = self.players_by_purchase.get(purchase_id)
                if existing_by_purchase:
                    if existing_by_purchase.agent_id != agent_id or existing_by_purchase.wallet != wallet_normalized:
                        raise ValueError("purchase_id already belongs to a different wallet or agent")
                    return existing_by_purchase

            if episodes_purchased <= 0:
                raise ValueError("episodes_purchased must be positive")

            player = PlayerState(
                wallet=wallet_normalized,
                agent_id=agent_id,
                bonfire_id=bonfire_id,
                erc8004_bonfire_id=erc8004_bonfire_id,
                purchase_id=purchase_id,
                purchase_tx_hash=purchase_tx_hash,
                base_quota=episodes_purchased,
            )
            self.players_by_agent[agent_id] = player
            if purchase_id:
                self.players_by_purchase[purchase_id] = player
            agent_ids = self.players_by_wallet.setdefault(player.wallet, [])
            if agent_id not in agent_ids:
                agent_ids.append(agent_id)
            self.ledger_by_agent.setdefault(agent_id, [])
            self._append_event(
                bonfire_id,
                "player_registered",
                {
                    "wallet": player.wallet,
                    "agent_id": player.agent_id,
                    "base_quota": player.base_quota,
                },
            )
            return player

    def register_purchase(
        self,
        wallet: str,
        agent_id: str,
        bonfire_id: str,
        erc8004_bonfire_id: int,
        purchase_id: str,
        purchase_tx_hash: str,
        episodes_purchased: int,
    ) -> PlayerState:
        return self.register_agent(
            wallet=wallet,
            agent_id=agent_id,
            bonfire_id=bonfire_id,
            erc8004_bonfire_id=erc8004_bonfire_id,
            episodes_purchased=episodes_purchased,
            purchase_id=purchase_id,
            purchase_tx_hash=purchase_tx_hash,
        )

    def create_or_replace_game(
        self,
        bonfire_id: str,
        owner_wallet: str,
        game_prompt: str,
        gm_agent_id: str | None,
        initial_episode_summary: str,
    ) -> GameState:
        with self._lock:
            existing = self.games_by_bonfire.get(bonfire_id)
            if existing and existing.status == "active":
                existing.status = "archived"
                existing.archived_at = datetime.now(UTC).isoformat()
                existing.updated_at = datetime.now(UTC).isoformat()
                self._append_event(
                    bonfire_id,
                    "game_archived",
                    {"game_id": existing.game_id, "reason": "replaced_by_new_game"},
                )

            game = GameState(
                bonfire_id=bonfire_id,
                owner_wallet=owner_wallet.lower(),
                game_prompt=game_prompt.strip(),
                gm_agent_id=gm_agent_id,
                initial_episode_summary=initial_episode_summary.strip(),
            )
            self.games_by_bonfire[bonfire_id] = game
            self._append_event(
                bonfire_id,
                "game_created",
                {"game_id": game.game_id, "owner_wallet": game.owner_wallet},
            )
            return game

    def create_quest(
        self,
        bonfire_id: str,
        creator_wallet: str,
        quest_type: str,
        prompt: str,
        keyword: str,
        reward: int,
        cooldown_seconds: int,
        expires_in_seconds: int | None,
    ) -> QuestState:
        with self._lock:
            if reward < 1:
                raise ValueError("reward must be >= 1")
            if cooldown_seconds < 0:
                raise ValueError("cooldown_seconds must be >= 0")
            quest_id = str(uuid.uuid4())
            expires_at: str | None = None
            if expires_in_seconds is not None and expires_in_seconds > 0:
                expires_at = (datetime.now(UTC) + timedelta(seconds=expires_in_seconds)).isoformat()
            quest = QuestState(
                quest_id=quest_id,
                bonfire_id=bonfire_id,
                creator_wallet=creator_wallet.lower(),
                quest_type=quest_type,
                prompt=prompt.strip(),
                keyword=keyword.strip().lower(),
                reward=reward,
                cooldown_seconds=cooldown_seconds,
                expires_at=expires_at,
            )
            self.quests_by_bonfire.setdefault(bonfire_id, {})[quest_id] = quest
            self.claimed_by_quest.setdefault(quest_id, set())
            self._append_event(
                bonfire_id,
                "quest_created",
                {
                    "quest_id": quest_id,
                    "quest_type": quest_type,
                    "reward": reward,
                    "keyword": quest.keyword,
                },
            )
            return quest

    def run_turn(self, agent_id: str, action: str) -> dict[str, object]:
        with self._lock:
            player = self.players_by_agent.get(agent_id)
            if not player:
                raise ValueError("agent is not registered in game")
            if player.remaining_episodes <= 0:
                player.is_active = False
                raise PermissionError("episode_quota_exhausted")

            player.turns_used += 1
            if player.remaining_episodes <= 0:
                player.is_active = False
            self._append_event(
                player.bonfire_id,
                "turn_processed",
                {
                    "agent_id": agent_id,
                    "action": action.strip(),
                    "turns_used": player.turns_used,
                    "remaining_episodes": player.remaining_episodes,
                },
            )
            return {
                "agent_id": player.agent_id,
                "remaining_episodes": player.remaining_episodes,
                "turns_used": player.turns_used,
                "is_active": player.is_active,
            }

    def claim_quest(self, quest_id: str, agent_id: str, submission: str) -> dict[str, object]:
        with self._lock:
            player = self.players_by_agent.get(agent_id)
            if not player:
                raise ValueError("agent is not registered in game")

            quests = self.quests_by_bonfire.get(player.bonfire_id, {})
            quest = quests.get(quest_id)
            if not quest:
                raise ValueError("quest not found")
            if quest.status != "active":
                raise ValueError("quest is not active")
            if quest.expires_at and datetime.now(UTC) > datetime.fromisoformat(quest.expires_at):
                raise ValueError("quest expired")

            claimed = self.claimed_by_quest.setdefault(quest_id, set())
            if agent_id in claimed:
                raise PermissionError("quest already claimed by this agent")

            now = datetime.now(UTC)
            cooldown_key = f"{quest_id}:{agent_id}"
            last_claim = self.last_claim_at.get(cooldown_key)
            if last_claim and (now - last_claim).total_seconds() < quest.cooldown_seconds:
                raise PermissionError("claim is in cooldown window")

            normalized_submission = submission.strip().lower()
            if len(normalized_submission) < 10:
                verdict = "rejected"
                reward_granted = 0
            elif quest.keyword and quest.keyword not in normalized_submission:
                verdict = "rejected"
                reward_granted = 0
            else:
                verdict = "accepted"
                reward_granted = quest.reward
                player.bonus_quota += reward_granted
                if player.remaining_episodes > 0:
                    player.is_active = True
                claimed.add(agent_id)
                self.last_claim_at[cooldown_key] = now
                self.ledger_by_agent.setdefault(agent_id, []).append(
                    {
                        "entry_id": str(uuid.uuid4()),
                        "type": "credit",
                        "reason": "quest_reward",
                        "amount": reward_granted,
                        "quest_id": quest_id,
                        "created_at": now.isoformat(),
                    }
                )

            attempt = AttemptState(
                quest_id=quest_id,
                agent_id=agent_id,
                submission=submission,
                verdict=verdict,
                reward_granted=reward_granted,
            )
            self.attempts.append(attempt)
            self._append_event(
                player.bonfire_id,
                "quest_claimed",
                {
                    "quest_id": quest_id,
                    "agent_id": agent_id,
                    "verdict": verdict,
                    "reward_granted": reward_granted,
                },
            )
            return {
                "quest_id": quest_id,
                "agent_id": agent_id,
                "verdict": verdict,
                "reward_granted": reward_granted,
                "remaining_episodes": player.remaining_episodes,
                "is_active": player.is_active,
            }

    def recharge_agent(self, bonfire_id: str, agent_id: str, amount: int, reason: str) -> dict[str, object]:
        with self._lock:
            if amount < 1:
                raise ValueError("amount must be >= 1")
            player = self.players_by_agent.get(agent_id)
            if not player or player.bonfire_id != bonfire_id:
                raise ValueError("agent is not registered to this bonfire")

            player.bonus_quota += amount
            if player.remaining_episodes > 0:
                player.is_active = True
            self.ledger_by_agent.setdefault(agent_id, []).append(
                {
                    "entry_id": str(uuid.uuid4()),
                    "type": "credit",
                    "reason": reason,
                    "amount": amount,
                    "created_at": datetime.now(UTC).isoformat(),
                }
            )
            self._append_event(
                bonfire_id,
                "agent_recharged",
                {"agent_id": agent_id, "amount": amount, "reason": reason},
            )
            return {
                "agent_id": agent_id,
                "remaining_episodes": player.remaining_episodes,
                "total_quota": player.total_quota,
                "is_active": player.is_active,
            }

    def list_active_games(self) -> list[dict[str, object]]:
        with self._lock:
            active: list[dict[str, object]] = []
            for game in self.games_by_bonfire.values():
                if game.status != "active":
                    continue
                players = [p for p in self.players_by_agent.values() if p.bonfire_id == game.bonfire_id]
                active.append(
                    {
                        "game_id": game.game_id,
                        "bonfire_id": game.bonfire_id,
                        "owner_wallet": game.owner_wallet,
                        "game_prompt": game.game_prompt,
                        "gm_agent_id": game.gm_agent_id,
                        "initial_episode_summary": game.initial_episode_summary,
                        "created_at": game.created_at,
                        "updated_at": game.updated_at,
                        "active_agent_count": len(players),
                    }
                )
            active.sort(key=lambda g: str(g.get("created_at", "")), reverse=True)
            return active

    def get_game(self, bonfire_id: str) -> GameState | None:
        with self._lock:
            return self.games_by_bonfire.get(bonfire_id)

    def update_game_world_state(
        self,
        bonfire_id: str,
        episode_id: str,
        world_state_summary: str,
        gm_reaction: str,
    ) -> dict[str, str]:
        with self._lock:
            game = self.games_by_bonfire.get(bonfire_id)
            if not game:
                return {}
            if world_state_summary.strip():
                game.world_state_summary = world_state_summary.strip()
            if gm_reaction.strip():
                game.last_gm_reaction = gm_reaction.strip()
            game.last_episode_id = episode_id.strip()
            game.updated_at = datetime.now(UTC).isoformat()
            self._append_event(
                bonfire_id,
                "world_state_updated",
                {
                    "game_id": game.game_id,
                    "episode_id": episode_id,
                    "world_state_summary": game.world_state_summary,
                },
            )
            return {
                "world_state_summary": game.world_state_summary,
                "last_gm_reaction": game.last_gm_reaction,
                "last_episode_id": game.last_episode_id,
            }

    def get_owner_agent_id(self, bonfire_id: str) -> str | None:
        with self._lock:
            game = self.games_by_bonfire.get(bonfire_id)
            if game and game.gm_agent_id:
                return game.gm_agent_id
            admin = self.game_admin_by_bonfire.get(bonfire_id)
            owner = str(admin.get("owner_wallet") or "").lower() if admin else ""
            if not owner:
                return None
            owner_agents = self.players_by_wallet.get(owner, [])
            for aid in owner_agents:
                if aid not in self.players_by_agent:
                    return aid
            return owner_agents[0] if owner_agents else None

    def create_room(
        self, bonfire_id: str, name: str, description: str = "", connections: list[str] | None = None
    ) -> RoomState:
        with self._lock:
            game = self.games_by_bonfire.get(bonfire_id)
            if not game:
                raise ValueError(f"No game for bonfire {bonfire_id}")
            room = RoomState(
                room_id=str(uuid.uuid4()),
                name=name,
                description=description,
                connections=connections or [],
            )
            game.rooms.append(asdict(room))
            game.updated_at = datetime.now(UTC).isoformat()
            self._persist_locked()
            return room

    def move_player(self, agent_id: str, room_id: str) -> bool:
        with self._lock:
            player = self.players_by_agent.get(agent_id)
            if not player:
                return False
            game = self.games_by_bonfire.get(player.bonfire_id)
            if not game:
                return False
            valid_ids = {r["room_id"] for r in game.rooms if isinstance(r, dict) and "room_id" in r}
            if room_id not in valid_ids:
                return False
            player.current_room = room_id
            self._persist_locked()
            return True

    def get_room_map(self, bonfire_id: str) -> dict[str, object]:
        with self._lock:
            game = self.games_by_bonfire.get(bonfire_id)
            rooms = list(game.rooms) if game else []
            players: list[dict[str, str]] = []
            for player in self.players_by_agent.values():
                if player.bonfire_id == bonfire_id:
                    players.append({
                        "agent_id": player.agent_id,
                        "wallet": player.wallet,
                        "current_room": player.current_room,
                    })
            return {"rooms": rooms, "players": players}

    def ensure_starting_room(self, bonfire_id: str) -> str:
        """Ensure at least one room exists for the game and return its room_id."""
        with self._lock:
            game = self.games_by_bonfire.get(bonfire_id)
            if not game:
                return ""
            if game.rooms:
                first = game.rooms[0]
                return str(first.get("room_id", "")) if isinstance(first, dict) else ""
            room = RoomState(
                room_id=str(uuid.uuid4()),
                name="The Hearth",
                description="A warm gathering place where all adventurers begin their journey.",
            )
            game.rooms.append(asdict(room))
            game.updated_at = datetime.now(UTC).isoformat()
            self._persist_locked()
            return room.room_id

    def place_player_in_starting_room(self, agent_id: str) -> None:
        """Place a player in the first room of their game if they have no room."""
        with self._lock:
            player = self.players_by_agent.get(agent_id)
            if not player or player.current_room:
                return
            game = self.games_by_bonfire.get(player.bonfire_id)
            if not game or not game.rooms:
                return
            first = game.rooms[0]
            if isinstance(first, dict) and "room_id" in first:
                player.current_room = str(first["room_id"])
                self._persist_locked()

    def append_room_message(
        self, room_id: str, sender_agent_id: str, sender_wallet: str, role: str, text: str,
    ) -> dict[str, object]:
        with self._lock:
            entry: dict[str, object] = {
                "room_id": room_id,
                "sender_agent_id": sender_agent_id,
                "sender_wallet": sender_wallet,
                "role": role,
                "text": text,
                "timestamp": datetime.now(UTC).isoformat(),
            }
            messages = self.room_chat_by_room.setdefault(room_id, [])
            messages.append(entry)
            if len(messages) > 200:
                del messages[: len(messages) - 200]
            self._persist_locked()
            return entry

    def get_room_messages(self, room_id: str, limit: int = 50) -> list[dict[str, object]]:
        with self._lock:
            messages = self.room_chat_by_room.get(room_id, [])
            return list(messages[-limit:])

    def update_room(
        self, bonfire_id: str, room_id: str, description: str | None = None, connections: list[str] | None = None,
    ) -> bool:
        with self._lock:
            game = self.games_by_bonfire.get(bonfire_id)
            if not game:
                return False
            for room in game.rooms:
                if isinstance(room, dict) and room.get("room_id") == room_id:
                    if description is not None:
                        room["description"] = description
                    if connections is not None:
                        room["connections"] = connections
                    game.updated_at = datetime.now(UTC).isoformat()
                    self._persist_locked()
                    return True
            return False

    def set_room_graph_entity(self, bonfire_id: str, room_id: str, entity_uuid: str) -> bool:
        with self._lock:
            game = self.games_by_bonfire.get(bonfire_id)
            if not game:
                return False
            for room in game.rooms:
                if isinstance(room, dict) and room.get("room_id") == room_id:
                    room["graph_entity_uuid"] = entity_uuid
                    self._persist_locked()
                    return True
            return False

    def get_room_by_id(self, bonfire_id: str, room_id: str) -> dict[str, object] | None:
        with self._lock:
            game = self.games_by_bonfire.get(bonfire_id)
            if not game:
                return None
            for room in game.rooms:
                if isinstance(room, dict) and room.get("room_id") == room_id:
                    return dict(room)
            return None

    def restore_players(self, wallet: str, purchase_tx_hash: str | None = None) -> list[dict[str, object]]:
        with self._lock:
            restored: list[dict[str, object]] = []
            for player in self.players_by_agent.values():
                if player.wallet != wallet.lower():
                    continue
                if purchase_tx_hash and player.purchase_tx_hash != purchase_tx_hash:
                    continue
                restored.append(
                    {
                        "wallet": player.wallet,
                        "agent_id": player.agent_id,
                        "bonfire_id": player.bonfire_id,
                        "purchase_id": player.purchase_id,
                        "purchase_tx_hash": player.purchase_tx_hash,
                        "remaining_episodes": player.remaining_episodes,
                        "total_quota": player.total_quota,
                        "is_active": player.is_active,
                    }
                )
            return restored

    def get_state(self, bonfire_id: str) -> dict[str, object]:
        with self._lock:
            players = [p for p in self.players_by_agent.values() if p.bonfire_id == bonfire_id]
            quests = list(self.quests_by_bonfire.get(bonfire_id, {}).values())
            contexts = [
                ctx
                for agent_id, ctx in self.agent_context_by_agent.items()
                if any(p.agent_id == agent_id for p in players)
            ]
            return {
                "bonfire_id": bonfire_id,
                "players": [
                    {
                        "wallet": p.wallet,
                        "agent_id": p.agent_id,
                        "remaining_episodes": p.remaining_episodes,
                        "turns_used": p.turns_used,
                        "total_quota": p.total_quota,
                        "is_active": p.is_active,
                    }
                    for p in players
                ],
                "quests": [
                    {
                        "quest_id": q.quest_id,
                        "quest_type": q.quest_type,
                        "prompt": q.prompt,
                        "keyword": q.keyword,
                        "reward": q.reward,
                        "status": q.status,
                        "expires_at": q.expires_at,
                    }
                    for q in quests
                ],
                "agent_context": contexts,
            }

    def get_events(self, bonfire_id: str, limit: int) -> list[dict[str, object]]:
        with self._lock:
            events = self.events_by_bonfire.get(bonfire_id, [])
            return list(events[-limit:])

    def get_owner_wallet(self, bonfire_id: str) -> str | None:
        with self._lock:
            admin = self.game_admin_by_bonfire.get(bonfire_id)
            if not admin:
                return None
            return str(admin.get("owner_wallet") or "")

    def get_player(self, agent_id: str) -> PlayerState | None:
        with self._lock:
            return self.players_by_agent.get(agent_id)

    def get_agent_context(self, agent_id: str) -> dict[str, object]:
        with self._lock:
            ctx = self.agent_context_by_agent.get(agent_id, {})
            return dict(ctx) if isinstance(ctx, dict) else {}

    def get_all_agent_ids(self) -> list[str]:
        with self._lock:
            return list(self.players_by_agent.keys())

    def update_agent_context_from_episode(
        self,
        agent_id: str,
        episode_id: str,
        episode_summary: str,
    ) -> dict[str, object]:
        with self._lock:
            player = self.players_by_agent.get(agent_id)
            if not player:
                raise ValueError("agent is not registered in game")
            context = self.agent_context_by_agent.setdefault(
                agent_id,
                {
                    "agent_id": agent_id,
                    "bonfire_id": player.bonfire_id,
                    "episode_count": 0,
                    "recent_episode_ids": [],
                    "last_episode_id": "",
                    "last_episode_summary": "",
                    "updated_at": datetime.now(UTC).isoformat(),
                },
            )

            ids_obj = context.get("recent_episode_ids")
            ids: list[str]
            if isinstance(ids_obj, list):
                ids = [str(x) for x in ids_obj]
            else:
                ids = []
            ids.append(episode_id)
            if len(ids) > 20:
                ids = ids[-20:]

            current_count = context.get("episode_count")
            if not isinstance(current_count, int):
                current_count = 0

            context.update(
                {
                    "episode_count": current_count + 1,
                    "recent_episode_ids": ids,
                    "last_episode_id": episode_id,
                    "last_episode_summary": episode_summary,
                    "updated_at": datetime.now(UTC).isoformat(),
                }
            )
            self._append_event(
                player.bonfire_id,
                "game_master_context_updated",
                {
                    "agent_id": agent_id,
                    "episode_id": episode_id,
                },
            )
            return dict(context)

    def update_agent_context_with_gm_response(
        self,
        agent_id: str,
        episode_id: str,
        gm_reaction: str,
        world_state_update: str,
    ) -> dict[str, object]:
        with self._lock:
            player = self.players_by_agent.get(agent_id)
            if not player:
                raise ValueError("agent is not registered in game")
            context = self.agent_context_by_agent.setdefault(
                agent_id,
                {
                    "agent_id": agent_id,
                    "bonfire_id": player.bonfire_id,
                    "episode_count": 0,
                    "recent_episode_ids": [],
                    "last_episode_id": "",
                    "last_episode_summary": "",
                    "updated_at": datetime.now(UTC).isoformat(),
                },
            )
            context.update(
                {
                    "gm_last_reaction": gm_reaction.strip(),
                    "gm_world_state_update": world_state_update.strip(),
                    "gm_last_episode_id": episode_id.strip(),
                    "gm_updated_at": datetime.now(UTC).isoformat(),
                    "updated_at": datetime.now(UTC).isoformat(),
                }
            )
            self._append_event(
                player.bonfire_id,
                "gm_response_recorded",
                {"agent_id": agent_id, "episode_id": episode_id},
            )
            return dict(context)


def _json_request(method: str, url: str, body: dict[str, object] | None = None) -> tuple[int, dict[str, object]]:
    payload = None
    headers: dict[str, str] = {"Content-Type": "application/json"}
    if DELVE_API_KEY:
        headers["Authorization"] = f"Bearer {DELVE_API_KEY}"
    if body is not None:
        payload = json.dumps(body).encode("utf-8")
    request = urllib.request.Request(url=url, data=payload, headers=headers, method=method)
    try:
        with urllib.request.urlopen(request, timeout=20) as response:
            raw = response.read().decode("utf-8")
            decoded = json.loads(raw) if raw else {}
            if isinstance(decoded, dict):
                return response.status, decoded
            return response.status, {"data": decoded}
    except urllib.error.HTTPError as exc:
        raw = exc.read().decode("utf-8", errors="replace")
        try:
            decoded = json.loads(raw)
        except json.JSONDecodeError:
            decoded = {"error": raw}
        if isinstance(decoded, dict):
            return exc.code, {str(k): v for k, v in decoded.items()}
        return exc.code, {"error": decoded}
    except urllib.error.URLError as exc:
        return 503, {"error": f"Backend request failed: {exc}"}


def _agent_json_request(
    method: str,
    url: str,
    api_key: str,
    body: dict[str, object] | None = None,
) -> tuple[int, dict[str, object]]:
    if not api_key.strip():
        return 503, {"error": "Agent API key is not configured"}
    payload = None
    headers: dict[str, str] = {
        "Content-Type": "application/json",
        "Authorization": f"Bearer {api_key}",
    }
    if body is not None:
        payload = json.dumps(body).encode("utf-8")
    request = urllib.request.Request(url=url, data=payload, headers=headers, method=method)
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            raw = response.read().decode("utf-8")
            decoded = json.loads(raw) if raw else {}
            if isinstance(decoded, dict):
                return response.status, decoded
            return response.status, {"data": decoded}
    except urllib.error.HTTPError as exc:
        raw = exc.read().decode("utf-8", errors="replace")
        try:
            decoded = json.loads(raw)
        except json.JSONDecodeError:
            decoded = {"error": raw}
        if isinstance(decoded, dict):
            return exc.code, {str(k): v for k, v in decoded.items()}
        return exc.code, {"error": decoded}
    except urllib.error.URLError as exc:
        return 503, {"error": f"Backend request failed: {exc}"}


def _resolve_owner_wallet_default(erc8004_bonfire_id: int) -> str:
    """Resolve owner wallet via existing EthereumRpcService."""
    repo_root: Path | None = None
    for candidate in [GAME_DIR, *GAME_DIR.parents]:
        target = candidate / "src" / "core" / "services" / "provision" / "ethereum_rpc_service.py"
        if target.exists():
            repo_root = candidate
            break
    if repo_root is None:
        raise RuntimeError("Unable to resolve repository root for src imports")

    repo_root_str = str(repo_root)
    if repo_root_str not in sys.path:
        sys.path.insert(0, repo_root_str)

    try:
        module = importlib.import_module("src.core.services.provision.ethereum_rpc_service")
    except ModuleNotFoundError as exc:
        raise RuntimeError(
            "Cannot import EthereumRpcService. Start server from this repository and ensure dependencies are installed."
        ) from exc

    service_cls = getattr(module, "EthereumRpcService", None)
    if service_cls is None:
        raise RuntimeError("EthereumRpcService class not found in provision service module")

    service = service_cls()
    return asyncio.run(service.get_nft_owner(erc8004_bonfire_id))


def _resolve_latest_episode_from_agent(agent_id: str) -> str:
    """Fetch the agent object and return the last entry in episode_uuids."""
    url = f"{DELVE_BASE_URL}/agents/{agent_id}"
    status, payload = _json_request("GET", url)
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
    uuid_url = f"{DELVE_BASE_URL}/episodes/by-uuid/{episode_id}"
    uuid_status, uuid_payload = _json_request("GET", uuid_url)
    if uuid_status == 200 and isinstance(uuid_payload, dict):
        return uuid_payload

    for url in (
        f"{DELVE_BASE_URL}/episodes/{episode_id}",
        f"{DELVE_BASE_URL}/bonfires/{bonfire_id}/episodes/{episode_id}",
    ):
        status, payload = _json_request("GET", url)
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
    list_url = f"{DELVE_BASE_URL}/bonfires/{bonfire_id}/episodes?limit=50"
    list_status, list_payload = _json_request("GET", list_url)
    if list_status == 200:
        episodes = list_payload.get("episodes")
        if isinstance(episodes, list):
            for item in episodes:
                if isinstance(item, dict):
                    eid = QuestGameHandler._extract_episode_id_from_payload(item)
                    if eid == episode_id:
                        return item
    return None


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
    if owner_agent_id and DELVE_API_KEY:
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
        gm_url = f"{DELVE_BASE_URL}/agents/{owner_agent_id}/chat"
        gm_status, gm_payload = _agent_json_request(
            "POST",
            gm_url,
            DELVE_API_KEY,
            body={
                "message": (
                    "You are the Game Master for a shared world. Read the episode and return strict JSON "
                    '{"extension_awarded": int, "reaction": string, "world_state_update": string, '
                    '"room_movements": [{"agent_id": string, "to_room": string}], '
                    '"new_rooms": [{"name": string, "description": string, "connections": [string]}], '
                    '"room_updates": [{"room_id": string, "description": string}]}. '
                    "extension_awarded must be between 0 and 3. "
                    "room_movements moves players between rooms when narratively appropriate. "
                    "new_rooms creates new areas for exploration (only when the story demands it). "
                    "room_updates changes descriptions of existing rooms as the world evolves. "
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
                parsed = QuestGameHandler._safe_json_object(reply)
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
        "source": "fallback",
    }


def _get_agent_episode_uuids_standalone(agent_id: str) -> list[str]:
    """Snapshot current episode_uuids for an agent (module-level helper)."""
    status, payload = _json_request("GET", f"{DELVE_BASE_URL}/agents/{agent_id}")
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
        url = f"{DELVE_BASE_URL}/agents/{agent_id}/stack/process"
        pre_uuids = _get_agent_episode_uuids_standalone(agent_id)
        status, payload = _agent_json_request("POST", url, DELVE_API_KEY, body={})
        player = store.get_player(agent_id)
        result_entry: dict[str, object] = {"agent_id": agent_id, "status": status, "payload": payload}

        if status == 200 and isinstance(payload, dict):
            episode_id = QuestGameHandler._extract_episode_id_from_payload(payload)
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
                    QuestGameHandler._extract_episode_summary(episode_payload)
                    if episode_payload is not None
                    else str(payload.get("message") or payload.get("detail") or f"Episode {episode_id} processed.")
                )

                store.update_agent_context_from_episode(agent_id, episode_id, episode_summary)

                gm_decision = _make_gm_decision(store, agent_id, episode_summary, episode_id, episode_payload)
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


class StackTimerRunner:
    """Background timer that processes all known agent stacks periodically."""

    def __init__(self, store: GameStore, interval_seconds: int) -> None:
        self._store = store
        self._interval_seconds = max(5, interval_seconds)
        self._stop_event = threading.Event()
        self._thread: threading.Thread | None = None
        self.last_run_at: str | None = None
        self.last_result: dict[str, object] | None = None

    @property
    def is_running(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    def start(self) -> None:
        if self.is_running:
            return

        def _loop() -> None:
            while not self._stop_event.is_set():
                self.last_result = _process_all_agent_stacks(self._store)
                self.last_run_at = datetime.now(UTC).isoformat()
                self._stop_event.wait(self._interval_seconds)

        self._stop_event.clear()
        self._thread = threading.Thread(target=_loop, daemon=True)
        self._thread.start()

    def stop(self) -> None:
        if not self.is_running:
            return
        self._stop_event.set()
        if self._thread is not None:
            self._thread.join(timeout=2)


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
        if not DELVE_API_KEY:
            continue

        room_summary = _build_room_structured_summary(store, bonfire_id)
        if room_summary:
            now_iso = datetime.now(UTC).isoformat()
            game_obj = store.get_game(bonfire_id)
            world_state = game_obj.world_state_summary if game_obj else ""
            summary_msg = (
                "You are the Game Master. Here is the current room-by-room activity summary "
                "for your world. Use this to inform your next narrative episode.\n"
                f"World state: {world_state}\n\n{room_summary}"
            )
            _agent_json_request(
                "POST",
                f"{DELVE_BASE_URL}/agents/{gm_agent_id}/stack/add",
                DELVE_API_KEY,
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

        url = f"{DELVE_BASE_URL}/agents/{gm_agent_id}/stack/process"
        pre_uuids = _get_agent_episode_uuids_standalone(gm_agent_id)
        status, payload = _agent_json_request("POST", url, DELVE_API_KEY, body={})
        episode_id = QuestGameHandler._extract_episode_id_from_payload(payload) if status == 200 else ""
        if status == 200 and not episode_id:
            episode_id = _poll_for_new_episode_standalone(gm_agent_id, pre_uuids)
        entry: dict[str, object] = {"gm_agent_id": gm_agent_id, "bonfire_id": bonfire_id, "status": status}
        if episode_id:
            entry["episode_id"] = episode_id
            episode_payload = _fetch_episode_payload(bonfire_id, episode_id)
            episode_summary = (
                QuestGameHandler._extract_episode_summary(episode_payload)
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


class GmBatchTimerRunner:
    """Background timer that processes GM agent stacks periodically (default 15 min)."""

    def __init__(self, store: GameStore, interval_seconds: int) -> None:
        self._store = store
        self._interval_seconds = max(30, interval_seconds)
        self._stop_event = threading.Event()
        self._thread: threading.Thread | None = None
        self.last_run_at: str | None = None
        self.last_result: dict[str, object] | None = None

    @property
    def is_running(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    def start(self) -> None:
        if self.is_running:
            return

        def _loop() -> None:
            while not self._stop_event.is_set():
                self._stop_event.wait(self._interval_seconds)
                if self._stop_event.is_set():
                    break
                self.last_result = _process_gm_stacks(self._store)
                self.last_run_at = datetime.now(UTC).isoformat()
                print(f"  [gm-timer] Processed GM stacks at {self.last_run_at}")

        self._stop_event.clear()
        self._thread = threading.Thread(target=_loop, daemon=True)
        self._thread.start()

    def stop(self) -> None:
        if not self.is_running:
            return
        self._stop_event.set()
        if self._thread is not None:
            self._thread.join(timeout=2)


class QuestGameHandler(http.server.SimpleHTTPRequestHandler):
    def __init__(
        self,
        *args,
        store: GameStore,
        resolve_owner_wallet: Callable[[int], str],
        stack_timer: StackTimerRunner | None = None,
        gm_timer: GmBatchTimerRunner | None = None,
        **kwargs,
    ) -> None:
        self._store = store
        self._resolve_owner_wallet = resolve_owner_wallet
        self._stack_timer = stack_timer
        self._gm_timer = gm_timer
        super().__init__(*args, directory=str(GAME_DIR), **kwargs)

    def _cors_headers(self) -> None:
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type, Authorization, X-Agent-Api-Key")

    def do_OPTIONS(self) -> None:
        self.send_response(200)
        self._cors_headers()
        self.send_header("Access-Control-Max-Age", "600")
        self.end_headers()

    def _strip_path(self) -> str:
        return urllib.parse.urlparse(self.path).path

    def _json_response(self, status: int, data: Mapping[str, object]) -> None:
        body = json.dumps(data).encode("utf-8")
        self.send_response(status)
        self._cors_headers()
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _read_json_body(self) -> dict[str, object]:
        content_length = int(self.headers.get("Content-Length", 0))
        if content_length <= 0:
            return {}
        raw = self.rfile.read(content_length).decode("utf-8")
        decoded = json.loads(raw)
        if not isinstance(decoded, dict):
            raise json.JSONDecodeError("Expected JSON object", raw, 0)
        return decoded

    @staticmethod
    def _required_string(data: dict[str, object], key: str) -> str:
        value = data.get(key)
        if not isinstance(value, str) or not value.strip():
            raise ValueError(f"{key} is required")
        return value.strip()

    @staticmethod
    def _required_int(data: dict[str, object], key: str) -> int:
        value = data.get(key)
        if not isinstance(value, int):
            raise ValueError(f"{key} must be an integer")
        return value

    @staticmethod
    def _resolve_graph_mode(data: dict[str, object], default: str = "regenerate") -> str:
        graph_mode_obj = data.get("graph_mode", default)
        graph_mode = str(graph_mode_obj).strip().lower()
        valid_modes = {"adaptive", "static", "regenerate", "append"}
        if graph_mode not in valid_modes:
            raise ValueError(f"graph_mode must be one of: {sorted(valid_modes)}")
        return graph_mode

    def _resolve_agent_api_key(self) -> tuple[str, str]:
        header_key = self.headers.get("X-Agent-Api-Key", "").strip()
        if header_key:
            return header_key, "header"
        if DELVE_API_KEY:
            return DELVE_API_KEY, "server"
        return "", "missing"

    def _assert_owner(self, bonfire_id: str, wallet: str) -> None:
        owner = self._store.get_owner_wallet(bonfire_id)
        if not owner:
            raise PermissionError("bonfire is not linked")
        if owner.lower() != wallet.lower():
            raise PermissionError("wallet is not the bonfire NFT owner")

    @staticmethod
    def _derive_keyword_from_text(text: str) -> str:
        words = [w.strip(".,!?;:()[]{}\"'").lower() for w in text.split()]
        filtered = [w for w in words if len(w) >= 4 and w.isalpha()]
        if not filtered:
            return "quest"
        return filtered[0]

    def _build_agent_chat_context(self, agent_id: str) -> dict[str, object]:
        player = self._store.get_player(agent_id)
        if not player:
            return {}
        state = self._store.get_state(player.bonfire_id)
        events = self._store.get_events(player.bonfire_id, 12)
        game = self._store.get_game(player.bonfire_id)

        players_obj = state.get("players")
        players = players_obj if isinstance(players_obj, list) else []
        quests_obj = state.get("quests")
        quests = quests_obj if isinstance(quests_obj, list) else []
        contexts_obj = state.get("agent_context")
        contexts = contexts_obj if isinstance(contexts_obj, list) else []

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

        visible_agents: list[dict[str, object]] = []
        for item in players:
            if not isinstance(item, dict):
                continue
            visible_agents.append(
                {
                    "agent_id": str(item.get("agent_id", "")),
                    "remaining_episodes": item.get("remaining_episodes"),
                    "is_active": item.get("is_active"),
                }
            )

        active_quests: list[dict[str, object]] = []
        for item in quests:
            if not isinstance(item, dict):
                continue
            if str(item.get("status", "")) != "active":
                continue
            active_quests.append(
                {
                    "quest_id": str(item.get("quest_id", "")),
                    "prompt": str(item.get("prompt", "")),
                    "keyword": str(item.get("keyword", "")),
                    "reward": item.get("reward"),
                }
            )

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
                event_summaries.append(
                    f"agent_recharged:{payload.get('agent_id')}:+{payload.get('amount')}"
                )
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

    def _build_game_context_preamble(self, agent_id: str) -> str:
        """Build a text preamble that injects game world state directly into the LLM prompt."""
        ctx = self._build_agent_chat_context(agent_id)
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

        player = self._store.get_player(agent_id)
        if player and player.current_room:
            room = self._store.get_room_by_id(player.bonfire_id, player.current_room)
            if room:
                room_name = str(room.get("name", "Unknown"))
                room_desc = str(room.get("description", ""))
                conns = room.get("connections", [])
                exits = ", ".join(str(c) for c in conns) if isinstance(conns, list) and conns else "none"
                parts.append(f"[CURRENT ROOM]\nName: {room_name}\nDescription: {room_desc}\nExits: {exits}")
            room_msgs = self._store.get_room_messages(player.current_room, limit=20)
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
                graph_context = self._fetch_room_graph_context(player.bonfire_id, room_graph_uuid)
                if graph_context:
                    parts.append(f"[ROOM KNOWLEDGE]\n{graph_context}")

        quests_obj = ctx.get("active_quests")
        quests = quests_obj if isinstance(quests_obj, list) else []
        if quests:
            quest_lines = [f"- {q.get('keyword', '?')}: {q.get('prompt', '')}" for q in quests if isinstance(q, dict)]
            if quest_lines:
                parts.append("[ACTIVE QUESTS]\n" + "\n".join(quest_lines))
        events_obj = ctx.get("recent_events")
        events = events_obj if isinstance(events_obj, list) else []
        if events:
            parts.append("[RECENT EVENTS]\n" + "\n".join(str(e) for e in events[-6:]))
        agent_ctx_obj = ctx.get("agent_game_context")
        agent_ctx = agent_ctx_obj if isinstance(agent_ctx_obj, dict) else {}
        last_summary = str(agent_ctx.get("last_episode_summary", "")).strip()
        if last_summary:
            parts.append(f"[YOUR LAST EPISODE]\n{last_summary}")
        return "\n\n".join(parts) + "\n\n---\n\n"

    def _fetch_room_graph_context(self, bonfire_id: str, entity_uuid: str) -> str:
        """Expand a room's graph entity to get its neighborhood for context injection."""
        url = f"{DELVE_BASE_URL}/knowledge_graph/expand/entity"
        body: dict[str, object] = {"entity_uuid": entity_uuid, "bonfire_id": bonfire_id, "limit": 30}
        status, payload = _json_request("POST", url, body)
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

    def _try_pin_room_graph_entity(self, bonfire_id: str, room_id: str) -> None:
        """Search the graph for a room entity by name and pin its UUID on the RoomState."""
        room = self._store.get_room_by_id(bonfire_id, room_id)
        if not room:
            return
        if room.get("graph_entity_uuid"):
            return
        room_name = str(room.get("name", ""))
        if not room_name:
            return
        url = f"{DELVE_BASE_URL}/delve"
        body: dict[str, object] = {
            "query": f"Room: {room_name}",
            "bonfire_id": bonfire_id,
            "limit": 5,
        }
        status, payload = _json_request("POST", url, body)
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
                self._store.set_room_graph_entity(bonfire_id, room_id, ent_uuid)
                return

    @staticmethod
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

    def _seed_game_from_prompt(
        self,
        bonfire_id: str,
        owner_wallet: str,
        game_prompt: str,
        gm_agent_id: str | None,
        quest_count: int,
    ) -> dict[str, object]:
        episode_summary = f"The game begins: {game_prompt.strip()}"
        seeded_quests: list[dict[str, object]] = []

        if gm_agent_id and DELVE_API_KEY:
            gm_url = f"{DELVE_BASE_URL}/agents/{gm_agent_id}/chat"
            gm_status, gm_payload = _agent_json_request(
                "POST",
                gm_url,
                DELVE_API_KEY,
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
                    parsed = self._safe_json_object(reply)
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

        self._store._append_event(
            bonfire_id,
            "game_seed_episode",
            {"episode_summary": episode_summary, "owner_wallet": owner_wallet.lower()},
        )

        created_quests: list[dict[str, object]] = []
        for i, seeded in enumerate(seeded_quests[: max(1, quest_count)]):
            prompt = str(seeded.get("prompt", "")).strip() or f"Quest seed {i + 1}"
            keyword = str(seeded.get("keyword", "")).strip().lower() or self._derive_keyword_from_text(prompt)
            reward_val = seeded.get("reward", 1)
            reward = reward_val if isinstance(reward_val, int) and reward_val >= 1 else 1
            quest = self._store.create_quest(
                bonfire_id=bonfire_id,
                creator_wallet=owner_wallet.lower(),
                quest_type="game_seed",
                prompt=prompt,
                keyword=keyword,
                reward=reward,
                cooldown_seconds=DEFAULT_CLAIM_COOLDOWN_SECONDS,
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

    @staticmethod
    def _extract_episode_summary(episode: dict[str, object]) -> str:
        for key in ("summary", "message", "content", "text", "body", "title"):
            value = episode.get(key)
            if isinstance(value, str) and value.strip():
                return value.strip()
        return json.dumps(episode)

    def _fetch_episode_by_id(self, bonfire_id: str, episode_id: str) -> dict[str, object] | None:
        return _fetch_episode_payload(bonfire_id, episode_id)

    def _auto_gm_decision(
        self,
        agent_id: str,
        episode_summary: str,
        episode_id: str,
        episode_payload: dict[str, object] | None,
    ) -> dict[str, object]:
        return _make_gm_decision(self._store, agent_id, episode_summary, episode_id, episode_payload)

    def _trigger_gm_reaction_for_agent(
        self, agent_id: str, episode_id: str | None = None
    ) -> tuple[int, dict[str, object]]:
        player = self._store.get_player(agent_id)
        if not player:
            return 404, {"error": "agent is not registered in game"}

        chosen_episode_id = episode_id or ""
        ctx = self._store.get_agent_context(agent_id)
        if not chosen_episode_id:
            episode_id_obj = ctx.get("last_episode_id")
            if isinstance(episode_id_obj, str) and episode_id_obj.strip():
                chosen_episode_id = episode_id_obj.strip()

        episode_summary = ""
        episode_payload: dict[str, object] | None = None
        if chosen_episode_id:
            episode_payload = self._fetch_episode_by_id(player.bonfire_id, chosen_episode_id)
            if episode_payload is not None:
                episode_summary = self._extract_episode_summary(episode_payload)

        if not episode_summary:
            summary_obj = ctx.get("last_episode_summary")
            if isinstance(summary_obj, str) and summary_obj.strip():
                episode_summary = summary_obj.strip()

        if not chosen_episode_id:
            chosen_episode_id = f"manual-{agent_id}-{int(time.time())}"
        if not episode_summary:
            return 400, {"error": "No episode context found. Process stack first."}

        gm_decision = self._auto_gm_decision(
            agent_id=agent_id,
            episode_summary=episode_summary,
            episode_id=chosen_episode_id,
            episode_payload=episode_payload,
        )
        reaction = str(gm_decision.get("reaction", "")).strip()
        world_update = str(gm_decision.get("world_state_update", "")).strip()
        updated_world = self._store.update_game_world_state(
            bonfire_id=player.bonfire_id,
            episode_id=chosen_episode_id,
            world_state_summary=world_update,
            gm_reaction=reaction,
        )
        gm_agent_ctx = self._store.update_agent_context_with_gm_response(
            agent_id=agent_id,
            episode_id=chosen_episode_id,
            gm_reaction=reaction,
            world_state_update=world_update,
        )
        extension_obj = gm_decision.get("extension_awarded", 0)
        extension = extension_obj if isinstance(extension_obj, int) else 0
        extension_payload: dict[str, object] | None = None
        if extension > 0:
            extension_payload = self._store.recharge_agent(
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

    def _handle_trigger_gm_reaction(self, data: dict[str, object]) -> None:
        agent_id = self._required_string(data, "agent_id")
        episode_id_obj = data.get("episode_id")
        episode_id = (
            self._required_string(data, "episode_id")
            if isinstance(episode_id_obj, str) and episode_id_obj.strip()
            else None
        )
        status, payload = self._trigger_gm_reaction_for_agent(agent_id, episode_id=episode_id)
        self._json_response(status, payload)

    def _handle_generate_world_episode(self, data: dict[str, object]) -> None:
        bonfire_id = self._required_string(data, "bonfire_id")
        game = self._store.get_game(bonfire_id)
        if not game:
            self._json_response(404, {"error": "game not found for bonfire"})
            return
        owner_agent_id = self._store.get_owner_agent_id(bonfire_id)
        if not owner_agent_id:
            self._json_response(400, {"error": "no owner agent available to publish world episode"})
            return
        if not DELVE_API_KEY:
            self._json_response(503, {"error": "DELVE_API_KEY is required for GM world episode generation"})
            return
        world_summary = game.world_state_summary.strip()
        gm_reaction = game.last_gm_reaction.strip()
        if not world_summary and not gm_reaction:
            self._json_response(400, {"error": "no GM world update available; trigger GM reaction first"})
            return

        gm_prompt = (
            "You are the Game Master. Publish an in-world update as a short episode entry.\n"
            f"World state update: {world_summary or 'n/a'}\n"
            f"GM reaction: {gm_reaction or 'n/a'}\n"
            "Return a concise narrative update."
        )
        chat_url = f"{DELVE_BASE_URL}/agents/{owner_agent_id}/chat"
        chat_status, chat_payload = _agent_json_request(
            "POST",
            chat_url,
            DELVE_API_KEY,
            body={
                "message": gm_prompt,
                "chat_history": [],
                "graph_mode": "append",
                "context": {"role": "game_master", "bonfire_id": bonfire_id},
            },
        )
        if chat_status != 200:
            self._json_response(chat_status, {"error": "gm world chat failed", "upstream": chat_payload})
            return
        reply_obj = chat_payload.get("reply")
        reply = str(reply_obj) if reply_obj is not None else ""
        now_iso = datetime.now(UTC).isoformat()

        add_url = f"{DELVE_BASE_URL}/agents/{owner_agent_id}/stack/add"
        add_status, add_payload = _agent_json_request(
            "POST",
            add_url,
            DELVE_API_KEY,
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
            self._json_response(add_status, {"error": "gm stack add failed", "chat": chat_payload, "stack": add_payload})
            return

        process_url = f"{DELVE_BASE_URL}/agents/{owner_agent_id}/stack/process"
        process_status, process_payload = _agent_json_request("POST", process_url, DELVE_API_KEY, body={})
        episode_id = self._extract_episode_id_from_payload(process_payload) if process_status == 200 else ""
        if process_status == 200 and episode_id:
            self._store.update_game_world_state(
                bonfire_id=bonfire_id,
                episode_id=episode_id,
                world_state_summary=world_summary or reply,
                gm_reaction=gm_reaction or "World update published.",
            )
        self._json_response(
            process_status,
            {
                "bonfire_id": bonfire_id,
                "owner_agent_id": owner_agent_id,
                "episode_id": episode_id,
                "chat": chat_payload,
                "stack_add": add_payload,
                "stack_process": process_payload,
            },
        )

    def _fetch_bonfire_episodes(self, bonfire_id: str, limit: int) -> list[dict[str, object]]:
        # This endpoint can differ by environment; we fail-soft to keep the demo usable.
        url = f"{DELVE_BASE_URL}/bonfires/{bonfire_id}/episodes?limit={limit}"
        status, payload = _json_request("GET", url)
        if status != 200:
            return []
        episodes = payload.get("episodes")
        if not isinstance(episodes, list):
            return []
        normalized: list[dict[str, object]] = []
        for item in episodes:
            if isinstance(item, dict):
                normalized.append(item)
        return normalized

    @staticmethod
    def _extract_id_like(value: object) -> str:
        if isinstance(value, str) and value.strip():
            return value.strip()
        if isinstance(value, dict):
            oid_obj = value.get("$oid")
            if isinstance(oid_obj, str) and oid_obj.strip():
                return oid_obj.strip()
            for key in ("episode_id", "episodeId", "id", "_id", "oid"):
                nested = value.get(key)
                nested_id = QuestGameHandler._extract_id_like(nested)
                if nested_id:
                    return nested_id
        return ""

    @staticmethod
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
            extracted = QuestGameHandler._extract_id_like(value)
            if extracted:
                return extracted
        return ""

    def _fetch_bonfire_pricing(self, bonfire_id: str) -> tuple[int, dict[str, object]]:
        url = f"{DELVE_BASE_URL}/bonfires/{bonfire_id}/pricing"
        return _json_request("GET", url)

    def _fetch_provision_records_for_wallet(self, wallet_address: str) -> list[dict[str, object]]:
        url = f"{DELVE_BASE_URL}/provision?wallet_address={urllib.parse.quote(wallet_address)}"
        status, payload = _json_request("GET", url)
        if status != 200:
            return []
        records_obj = payload.get("records")
        if isinstance(records_obj, list):
            return [r for r in records_obj if isinstance(r, dict)]
        return []

    def _fetch_owned_bonfires_for_wallet(self, wallet_address: str) -> list[dict[str, object]]:
        records = self._fetch_provision_records_for_wallet(wallet_address)

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
                owner = self._resolve_owner_wallet(token_id_obj).lower()
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

    def _fetch_wallet_purchased_agents(
        self, wallet_address: str, bonfire_id: str
    ) -> list[dict[str, object]]:
        purchased_by_agent_id: dict[str, dict[str, object]] = {}

        # Primary source: purchased_agents collection via dedicated API.
        purchased_url = (
            f"{DELVE_BASE_URL}/purchased-agents?"
            f"wallet_address={urllib.parse.quote(wallet_address)}&bonfire_id={urllib.parse.quote(bonfire_id)}"
        )
        purchased_status, purchased_payload = _json_request("GET", purchased_url)
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
                    purchase_tx_hash_obj = (
                        rec.get("purchase_tx_hash")
                        or rec.get("purchaseTxHash")
                        or rec.get("tx_hash")
                        or rec.get("txHash")
                    )
                    if isinstance(purchase_tx_hash_obj, str) and purchase_tx_hash_obj:
                        purchased_item["purchase_tx_hash"] = purchase_tx_hash_obj
                    purchased_by_agent_id[rec_agent_obj] = purchased_item

        records = self._fetch_provision_records_for_wallet(wallet_address)
        for rec in records:
            if not isinstance(rec, dict):
                continue
            rec_bonfire_id = rec.get("bonfire_id")
            if not isinstance(rec_bonfire_id, str) or not rec_bonfire_id:
                continue
            if rec_bonfire_id != bonfire_id:
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

        # Fallback/augmentation: include all agents currently registered to the bonfire.
        # In practice many flows only expose one wallet provision record even when
        # multiple purchased agents exist for the same bonfire.
        bonfire_agents_url = f"{DELVE_BASE_URL}/bonfires/{bonfire_id}/agents"
        status, payload = _json_request("GET", bonfire_agents_url)
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
                    purchase_tx_hash_obj = (
                        item_obj.get("purchase_tx_hash")
                        or item_obj.get("purchaseTxHash")
                        or item_obj.get("tx_hash")
                        or item_obj.get("txHash")
                    )
                    if isinstance(purchase_tx_hash_obj, str) and purchase_tx_hash_obj:
                        merged["purchase_tx_hash"] = purchase_tx_hash_obj
                    purchased_by_agent_id[agent_id_raw] = merged

        purchased = list(purchased_by_agent_id.values())
        purchased.sort(key=lambda x: str(x.get("agent_id", "")))
        return purchased

    def _fetch_agent_configs_for_bonfire(self, bonfire_id: str) -> list[dict[str, object]]:
        status, payload = _json_request("GET", f"{DELVE_BASE_URL}/agents?bonfire_id={urllib.parse.quote(bonfire_id)}")
        if status != 200 or not isinstance(payload, dict):
            return []
        agents_obj = payload.get("agents")
        if not isinstance(agents_obj, list):
            return []
        return [obj for obj in agents_obj if isinstance(obj, dict)]

    @staticmethod
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
            deploy_candidates = (
                deployment_obj.get("purchase_tx_hash"),
                deployment_obj.get("purchaseTxHash"),
                deployment_obj.get("tx_hash"),
                deployment_obj.get("txHash"),
            )
            for value in deploy_candidates:
                if isinstance(value, str) and value:
                    return value
        return None

    def _handle_purchase_proxy(self, bonfire_id: str, data: dict[str, object]) -> None:
        url = f"{DELVE_BASE_URL}/bonfires/{bonfire_id}/purchase-agent"
        status, payload = _json_request("POST", url, data)
        self._json_response(status, payload)

    def _handle_reveal_nonce_proxy(self, data: dict[str, object]) -> None:
        purchase_id = self._required_string(data, "purchase_id")
        url = f"{DELVE_BASE_URL}/purchased-agents/{purchase_id}/reveal_nonce"
        status, payload = _json_request("GET", url)
        self._json_response(status, payload)

    def _handle_reveal_api_key_proxy(self, data: dict[str, object]) -> None:
        purchase_id = self._required_string(data, "purchase_id")
        nonce = self._required_string(data, "nonce")
        signature = self._required_string(data, "signature")
        url = f"{DELVE_BASE_URL}/purchased-agents/{purchase_id}/reveal_api_key"
        status, payload = _json_request("POST", url, {"nonce": nonce, "signature": signature})
        self._json_response(status, payload)

    def _resolve_purchase_id_for_selected_agent(
        self,
        wallet_address: str,
        bonfire_id: str,
        agent_id: str,
    ) -> str | None:
        wallet_lower = wallet_address.lower()
        player = self._store.get_player(agent_id)
        if (
            player
            and player.wallet == wallet_lower
            and player.bonfire_id == bonfire_id
            and player.purchase_id
        ):
            return player.purchase_id

        purchased_agents = self._fetch_wallet_purchased_agents(wallet_lower, bonfire_id)
        for item in purchased_agents:
            if not isinstance(item, dict):
                continue
            item_agent_id = item.get("agent_id")
            if item_agent_id != agent_id:
                continue
            purchase_id_obj = item.get("purchase_id")
            if isinstance(purchase_id_obj, str) and purchase_id_obj:
                return purchase_id_obj

        # Some environments expose purchase identifiers on agent config
        # payloads (often camelCase or alternate naming), not provision records.
        status, payload = _json_request("GET", f"{DELVE_BASE_URL}/agents/{agent_id}")
        if status == 200 and isinstance(payload, dict):
            candidate_keys = (
                "purchase_id",
                "purchaseId",
            )
            for key in candidate_keys:
                candidate_obj = payload.get(key)
                if isinstance(candidate_obj, str) and candidate_obj:
                    return candidate_obj

        # Final fallback: in some deployments purchased-agent IDs map to agent IDs.
        # Probe reveal_nonce to validate before accepting.
        probe_url = f"{DELVE_BASE_URL}/purchased-agents/{agent_id}/reveal_nonce"
        probe_status, _probe_payload = _json_request("GET", probe_url)
        if probe_status == 200:
            return agent_id
        return None

    def _resolve_purchase_tx_hash_for_selected_agent(
        self,
        wallet_address: str,
        bonfire_id: str,
        agent_id: str,
    ) -> str | None:
        wallet_lower = wallet_address.lower()
        player = self._store.get_player(agent_id)
        if (
            player
            and player.wallet == wallet_lower
            and player.bonfire_id == bonfire_id
            and player.purchase_tx_hash
        ):
            return player.purchase_tx_hash

        purchased_agents = self._fetch_wallet_purchased_agents(wallet_lower, bonfire_id)
        for item in purchased_agents:
            if not isinstance(item, dict):
                continue
            if item.get("agent_id") != agent_id:
                continue
            tx_obj = (
                item.get("purchase_tx_hash")
                or item.get("purchaseTxHash")
                or item.get("tx_hash")
                or item.get("txHash")
            )
            if isinstance(tx_obj, str) and tx_obj:
                return tx_obj

        # Public list route can expose agent-config purchase fields without requiring
        # direct agent access grants.
        for agent_payload in self._fetch_agent_configs_for_bonfire(bonfire_id):
            payload_agent_id_obj = agent_payload.get("id") or agent_payload.get("_id") or agent_payload.get("agent_id")
            if not isinstance(payload_agent_id_obj, str) or payload_agent_id_obj != agent_id:
                continue
            tx_obj = self._extract_purchase_tx_hash_from_agent_payload(agent_payload)
            if isinstance(tx_obj, str) and tx_obj:
                return tx_obj

        status, payload = _json_request("GET", f"{DELVE_BASE_URL}/agents/{agent_id}")
        if status == 200 and isinstance(payload, dict):
            tx_obj = self._extract_purchase_tx_hash_from_agent_payload(payload)
            if isinstance(tx_obj, str) and tx_obj:
                return tx_obj
        return None

    def _handle_reveal_nonce_selected(self, data: dict[str, object]) -> None:
        wallet = self._required_string(data, "wallet_address").lower()
        bonfire_id = self._required_string(data, "bonfire_id")
        agent_id = self._required_string(data, "agent_id")
        purchase_id = self._resolve_purchase_id_for_selected_agent(
            wallet_address=wallet,
            bonfire_id=bonfire_id,
            agent_id=agent_id,
        )
        if purchase_id:
            url = f"{DELVE_BASE_URL}/purchased-agents/{purchase_id}/reveal_nonce"
            status, payload = _json_request("GET", url)
            if isinstance(payload, dict):
                response_payload = dict(payload)
                response_payload["purchase_id"] = purchase_id
                response_payload["resolution"] = "purchase_id"
                self._json_response(status, response_payload)
                return
            self._json_response(status, {"purchase_id": purchase_id, "upstream_payload": payload})
            return

        purchase_tx_hash = self._resolve_purchase_tx_hash_for_selected_agent(
            wallet_address=wallet,
            bonfire_id=bonfire_id,
            agent_id=agent_id,
        )
        if purchase_tx_hash:
            url = f"{DELVE_BASE_URL}/provision/reveal_nonce?tx_hash={urllib.parse.quote(purchase_tx_hash)}"
            status, payload = _json_request("GET", url)
            if isinstance(payload, dict):
                response_payload = dict(payload)
                response_payload["purchase_tx_hash"] = purchase_tx_hash
                response_payload["resolution"] = "purchase_tx_hash"
                self._json_response(status, response_payload)
                return
            self._json_response(status, {"purchase_tx_hash": purchase_tx_hash, "upstream_payload": payload})
            return

        self._json_response(
            404,
            {
                "error": "purchase_id_not_found_for_selected_agent",
                "detail": "Selected agent has no purchase_id or purchase_tx_hash in available purchase records.",
                "wallet_address": wallet,
                "bonfire_id": bonfire_id,
                "agent_id": agent_id,
            },
        )

    def _handle_reveal_api_key_selected(self, data: dict[str, object]) -> None:
        wallet = self._required_string(data, "wallet_address").lower()
        bonfire_id = self._required_string(data, "bonfire_id")
        agent_id = self._required_string(data, "agent_id")
        nonce = self._required_string(data, "nonce")
        signature = self._required_string(data, "signature")
        purchase_id_obj = data.get("purchase_id")
        purchase_id = str(purchase_id_obj).strip() if isinstance(purchase_id_obj, str) else ""
        if not purchase_id:
            purchase_id = self._resolve_purchase_id_for_selected_agent(
                wallet_address=wallet,
                bonfire_id=bonfire_id,
                agent_id=agent_id,
            )
        if purchase_id:
            url = f"{DELVE_BASE_URL}/purchased-agents/{purchase_id}/reveal_api_key"
            status, payload = _json_request("POST", url, {"nonce": nonce, "signature": signature})
            if isinstance(payload, dict):
                response_payload = dict(payload)
                response_payload["purchase_id"] = purchase_id
                response_payload["resolution"] = "purchase_id"
                self._json_response(status, response_payload)
                return
            self._json_response(status, {"purchase_id": purchase_id, "upstream_payload": payload})
            return

        purchase_tx_hash_obj = data.get("purchase_tx_hash")
        purchase_tx_hash = str(purchase_tx_hash_obj).strip() if isinstance(purchase_tx_hash_obj, str) else ""
        if not purchase_tx_hash:
            purchase_tx_hash = self._resolve_purchase_tx_hash_for_selected_agent(
                wallet_address=wallet,
                bonfire_id=bonfire_id,
                agent_id=agent_id,
            )
        if purchase_tx_hash:
            url = f"{DELVE_BASE_URL}/provision/reveal_api_key"
            status, payload = _json_request(
                "POST",
                url,
                {"tx_hash": purchase_tx_hash, "nonce": nonce, "signature": signature},
            )
            if isinstance(payload, dict):
                response_payload = dict(payload)
                response_payload["purchase_tx_hash"] = purchase_tx_hash
                response_payload["resolution"] = "purchase_tx_hash"
                self._json_response(status, response_payload)
                return
            self._json_response(status, {"purchase_tx_hash": purchase_tx_hash, "upstream_payload": payload})
            return

        self._json_response(
            404,
            {
                "error": "purchase_id_not_found_for_selected_agent",
                "detail": "Selected agent has no purchase_id or purchase_tx_hash in available purchase records.",
                "wallet_address": wallet,
                "bonfire_id": bonfire_id,
                "agent_id": agent_id,
            },
        )

    def _handle_register_purchase(self, data: dict[str, object]) -> None:
        wallet = self._required_string(data, "wallet_address").lower()
        agent_id = self._required_string(data, "agent_id")
        bonfire_id = self._required_string(data, "bonfire_id")
        purchase_id = self._required_string(data, "purchase_id")
        purchase_tx_hash = self._required_string(data, "purchase_tx_hash")
        erc8004_bonfire_id = self._required_int(data, "erc8004_bonfire_id")
        episodes_purchased = self._required_int(data, "episodes_purchased")

        # Validate purchase exists using existing purchased-agent route family.
        reveal_nonce_url = f"{DELVE_BASE_URL}/purchased-agents/{purchase_id}/reveal_nonce"
        status, payload = _json_request("GET", reveal_nonce_url)
        if status != 200:
            self._json_response(
                400,
                {
                    "error": "invalid_purchase_id",
                    "detail": "purchase_id could not be validated against purchased-agent endpoints",
                    "upstream_status": status,
                    "upstream_payload": payload,
                },
            )
            return

        # Demo-friendly flow: trust registered wallet as bonfire owner context.
        # We only set this once per bonfire to avoid accidental ownership swaps.
        owner_wallet_existing = self._store.get_owner_wallet(bonfire_id)
        owner_wallet = owner_wallet_existing.lower() if owner_wallet_existing else wallet
        if not owner_wallet_existing:
            self._store.link_bonfire(
                bonfire_id=bonfire_id,
                erc8004_bonfire_id=erc8004_bonfire_id,
                owner_wallet=owner_wallet,
            )

        player = self._store.register_purchase(
            wallet=wallet,
            agent_id=agent_id,
            bonfire_id=bonfire_id,
            erc8004_bonfire_id=erc8004_bonfire_id,
            purchase_id=purchase_id,
            purchase_tx_hash=purchase_tx_hash,
            episodes_purchased=episodes_purchased,
        )
        self._store.place_player_in_starting_room(agent_id)
        self._json_response(
            200,
            {
                "agent_id": player.agent_id,
                "purchase_id": player.purchase_id,
                "owner_wallet": owner_wallet,
                "remaining_episodes": player.remaining_episodes,
                "total_quota": player.total_quota,
            },
        )

    def _handle_register_selected_agent(self, data: dict[str, object]) -> None:
        wallet = self._required_string(data, "wallet_address").lower()
        agent_id = self._required_string(data, "agent_id")
        bonfire_id = self._required_string(data, "bonfire_id")
        erc8004_bonfire_id = self._required_int(data, "erc8004_bonfire_id")
        episodes_obj = data.get("episodes_purchased", 2)
        if not isinstance(episodes_obj, int):
            raise ValueError("episodes_purchased must be an integer")
        episodes_purchased = max(1, episodes_obj)

        owner_wallet_existing = self._store.get_owner_wallet(bonfire_id)
        owner_wallet = owner_wallet_existing.lower() if owner_wallet_existing else wallet
        if not owner_wallet_existing:
            self._store.link_bonfire(
                bonfire_id=bonfire_id,
                erc8004_bonfire_id=erc8004_bonfire_id,
                owner_wallet=owner_wallet,
            )

        player = self._store.register_agent(
            wallet=wallet,
            agent_id=agent_id,
            bonfire_id=bonfire_id,
            erc8004_bonfire_id=erc8004_bonfire_id,
            episodes_purchased=episodes_purchased,
        )
        self._store.place_player_in_starting_room(agent_id)
        self._json_response(
            200,
            {
                "agent_id": player.agent_id,
                "owner_wallet": owner_wallet,
                "remaining_episodes": player.remaining_episodes,
                "total_quota": player.total_quota,
                "note": "Selected agent registered using local game config only.",
            },
        )

    def _handle_create_game(self, data: dict[str, object]) -> None:
        bonfire_id = self._required_string(data, "bonfire_id")
        wallet = self._required_string(data, "wallet_address").lower()
        game_prompt = self._required_string(data, "game_prompt")
        erc8004_bonfire_id = self._required_int(data, "erc8004_bonfire_id")
        gm_agent_id_raw = data.get("gm_agent_id")
        gm_agent_id = str(gm_agent_id_raw).strip() if isinstance(gm_agent_id_raw, str) and gm_agent_id_raw.strip() else None

        quest_count_obj = data.get("initial_quest_count", 2)
        if not isinstance(quest_count_obj, int):
            raise ValueError("initial_quest_count must be an integer")
        quest_count = max(1, min(quest_count_obj, 5))

        owner_wallet_existing = self._store.get_owner_wallet(bonfire_id)
        if owner_wallet_existing and owner_wallet_existing.lower() != wallet:
            raise PermissionError("wallet is not the bonfire game owner")
        if not owner_wallet_existing:
            self._store.link_bonfire(
                bonfire_id=bonfire_id,
                erc8004_bonfire_id=erc8004_bonfire_id,
                owner_wallet=wallet,
            )

        seed = self._seed_game_from_prompt(
            bonfire_id=bonfire_id,
            owner_wallet=wallet,
            game_prompt=game_prompt,
            gm_agent_id=gm_agent_id,
            quest_count=quest_count,
        )
        game = self._store.create_or_replace_game(
            bonfire_id=bonfire_id,
            owner_wallet=wallet,
            game_prompt=game_prompt,
            gm_agent_id=gm_agent_id,
            initial_episode_summary=str(seed.get("episode_summary", "")),
        )
        self._store.ensure_starting_room(bonfire_id)
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
        self._json_response(200, response)

    def _handle_restore_players(self, data: dict[str, object]) -> None:
        wallet = self._required_string(data, "wallet_address").lower()
        tx_hash_obj = data.get("purchase_tx_hash")
        tx_hash = self._required_string(data, "purchase_tx_hash") if tx_hash_obj else None
        restored = self._store.restore_players(wallet=wallet, purchase_tx_hash=tx_hash)
        self._json_response(
            200,
            {"wallet_address": wallet, "purchase_tx_hash": tx_hash, "players": restored},
        )

    def _handle_agent_completion(self, data: dict[str, object]) -> None:
        agent_id = self._required_string(data, "agent_id")
        message = self._required_string(data, "message")
        chat_id = self._required_string(data, "chat_id") if data.get("chat_id") else f"game-{agent_id}"
        user_id = self._required_string(data, "user_id") if data.get("user_id") else "game-user"
        as_game_master = bool(data.get("as_game_master", False))
        graph_mode = self._resolve_graph_mode(data, default="regenerate")

        player = self._store.get_player(agent_id)
        if not player:
            self._json_response(404, {"error": "agent is not registered in game"})
            return
        agent_api_key, api_key_source = self._resolve_agent_api_key()
        if not agent_api_key:
            self._json_response(503, {"error": "Provide X-Agent-Api-Key or set DELVE_API_KEY on server"})
            return

        preamble = self._build_game_context_preamble(agent_id)
        augmented_message = f"{preamble}{message}" if preamble else message

        chat_url = f"{DELVE_BASE_URL}/agents/{agent_id}/chat"
        chat_status, chat_payload = _agent_json_request(
            "POST",
            chat_url,
            agent_api_key,
            body={
                "message": augmented_message,
                "chat_history": [],
                "graph_mode": graph_mode,
                "context": self._build_agent_chat_context(agent_id),
            },
        )
        if chat_status != 200:
            self._json_response(
                chat_status,
                {
                    "error": "agent chat failed",
                    "upstream": chat_payload,
                },
            )
            return

        assistant_reply_obj = chat_payload.get("reply")
        if isinstance(assistant_reply_obj, str):
            assistant_reply = assistant_reply_obj
        else:
            assistant_reply = json.dumps(chat_payload)

        now_iso = datetime.now(UTC).isoformat()
        room_prefix = ""
        if player.current_room:
            room_data = self._store.get_room_by_id(player.bonfire_id, player.current_room)
            if room_data:
                room_prefix = f"[Room: {room_data.get('name', 'Unknown')}] "

        stack_text_user = f"{room_prefix}{message}" if room_prefix else message
        stack_text_agent = f"{room_prefix}{assistant_reply}" if room_prefix else assistant_reply

        stack_url = f"{DELVE_BASE_URL}/agents/{agent_id}/stack/add"
        stack_status, stack_payload = _agent_json_request(
            "POST",
            stack_url,
            agent_api_key,
            body={
                "messages": [
                    {
                        "text": stack_text_user,
                        "userId": user_id,
                        "chatId": chat_id,
                        "timestamp": now_iso,
                    },
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
            self._json_response(
                stack_status,
                {
                    "error": "stack add failed",
                    "chat": chat_payload,
                    "stack": stack_payload,
                },
            )
            return

        if player.current_room:
            self._store.append_room_message(
                room_id=player.current_room,
                sender_agent_id=agent_id,
                sender_wallet=player.wallet,
                role="user",
                text=message,
            )
            self._store.append_room_message(
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
            owner_wallet = self._store.get_owner_wallet(player.bonfire_id)
            if not owner_wallet or owner_wallet.lower() != player.wallet.lower():
                self._json_response(
                    403,
                    {
                        "error": "Only bonfire NFT owner agent can generate quests via completions",
                    },
                )
                return

            reward = data.get("reward", 1)
            if not isinstance(reward, int):
                raise ValueError("reward must be an integer")
            cooldown = data.get("cooldown_seconds", DEFAULT_CLAIM_COOLDOWN_SECONDS)
            if not isinstance(cooldown, int):
                raise ValueError("cooldown_seconds must be an integer")
            quest_type = str(data.get("quest_type", "gm_generated"))
            keyword_raw = data.get("keyword")
            if isinstance(keyword_raw, str) and keyword_raw.strip():
                keyword = keyword_raw.strip().lower()
            else:
                keyword = self._derive_keyword_from_text(assistant_reply)

            quest = self._store.create_quest(
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

        self._json_response(200, response_body)

    def _get_agent_episode_uuids(self, agent_id: str) -> list[str]:
        """Snapshot current episode_uuids for an agent."""
        status, payload = _json_request("GET", f"{DELVE_BASE_URL}/agents/{agent_id}")
        if status != 200 or not isinstance(payload, dict):
            print(f"  [poll] GET /agents/{agent_id} returned {status}")
            return []
        uuids = payload.get("episode_uuids") or payload.get("episodeUuids") or []
        return [str(u) for u in uuids] if isinstance(uuids, list) else []

    def _poll_for_new_episode(
        self, agent_id: str, pre_uuids: list[str], max_wait: float = 45.0, interval: float = 3.0
    ) -> str:
        """Poll agent's episode_uuids until a new entry appears or timeout."""
        pre_set = set(pre_uuids)
        elapsed = 0.0
        print(f"  [poll] Waiting for new episode on agent {agent_id} (pre={len(pre_uuids)} uuids, max_wait={max_wait}s)")
        while elapsed < max_wait:
            time.sleep(interval)
            elapsed += interval
            current = self._get_agent_episode_uuids(agent_id)
            new_uuids = [u for u in current if u not in pre_set]
            if new_uuids:
                print(f"  [poll] Found new episode after {elapsed:.0f}s: {new_uuids[-1]}")
                return new_uuids[-1]
            print(f"  [poll] {elapsed:.0f}s elapsed, {len(current)} total uuids, no new yet")
        print(f"  [poll] Timed out after {max_wait}s for agent {agent_id}")
        return _resolve_latest_episode_from_agent(agent_id) or ""

    def _handle_process_stack(self, data: dict[str, object]) -> None:
        agent_id = self._required_string(data, "agent_id")
        agent_api_key, api_key_source = self._resolve_agent_api_key()
        if not agent_api_key:
            self._json_response(503, {"error": "Provide X-Agent-Api-Key or set DELVE_API_KEY on server"})
            return
        url = f"{DELVE_BASE_URL}/agents/{agent_id}/stack/process"
        pre_uuids = self._get_agent_episode_uuids(agent_id)
        status, payload = _agent_json_request("POST", url, agent_api_key, body={})
        episode_id = self._extract_episode_id_from_payload(payload) if status == 200 else ""
        if status == 200 and not episode_id:
            episode_id = self._poll_for_new_episode(agent_id, pre_uuids)
        response_payload: dict[str, object] = dict(payload)
        response_payload["api_key_source"] = api_key_source
        if status == 200:
            if episode_id:
                summary_obj = payload.get("message") or payload.get("detail") or ""
                player = self._store.get_player(agent_id)
                bonfire_id = player.bonfire_id if player else ""
                episode_payload = (
                    self._fetch_episode_by_id(bonfire_id, episode_id) if bonfire_id else None
                )
                if episode_payload is None:
                    payload_episode_obj = payload.get("episode")
                    if isinstance(payload_episode_obj, dict):
                        episode_payload = payload_episode_obj
                episode_summary = (
                    self._extract_episode_summary(episode_payload)
                    if episode_payload is not None
                    else str(summary_obj) or f"Episode {episode_id} processed."
                )
                gm_context = self._store.update_agent_context_from_episode(agent_id, episode_id, episode_summary)
                response_payload["game_master_context"] = gm_context
                gm_decision = self._auto_gm_decision(
                    agent_id=agent_id,
                    episode_summary=episode_summary,
                    episode_id=episode_id,
                    episode_payload=episode_payload,
                )
                reaction_obj = gm_decision.get("reaction", "")
                reaction = str(reaction_obj).strip()
                world_update_obj = gm_decision.get("world_state_update", "")
                world_update = str(world_update_obj).strip()
                if player:
                    updated_world = self._store.update_game_world_state(
                        bonfire_id=player.bonfire_id,
                        episode_id=episode_id,
                        world_state_summary=world_update,
                        gm_reaction=reaction,
                    )
                    if updated_world:
                        response_payload["world_state"] = updated_world
                gm_agent_ctx = self._store.update_agent_context_with_gm_response(
                    agent_id=agent_id,
                    episode_id=episode_id,
                    gm_reaction=reaction,
                    world_state_update=world_update,
                )
                response_payload["agent_gm_context"] = gm_agent_ctx
                extension_obj = gm_decision.get("extension_awarded", 0)
                extension = extension_obj if isinstance(extension_obj, int) else 0
                if extension > 0:
                    if player:
                        recharge_result = self._store.recharge_agent(
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
        self._json_response(status, response_payload)

    def _handle_end_turn(self, data: dict[str, object]) -> None:
        """Orchestrate a full player turn: process user stack, get GM reaction on
        the dedicated GM agent, inject context, queue the GM's stack, and apply
        room movements — all in one request."""
        agent_id = self._required_string(data, "agent_id")
        agent_api_key, api_key_source = self._resolve_agent_api_key()
        if not agent_api_key:
            self._json_response(503, {"error": "Provide X-Agent-Api-Key or set DELVE_API_KEY on server"})
            return
        player = self._store.get_player(agent_id)
        if not player:
            self._json_response(404, {"error": "agent is not registered in game"})
            return

        bonfire_id = player.bonfire_id
        gm_agent_id = self._store.get_owner_agent_id(bonfire_id)

        # --- Step 1: Process user agent stack -> user episode ---
        url = f"{DELVE_BASE_URL}/agents/{agent_id}/stack/process"
        pre_uuids = self._get_agent_episode_uuids(agent_id)
        proc_status, proc_payload = _agent_json_request("POST", url, agent_api_key, body={})
        episode_id = self._extract_episode_id_from_payload(proc_payload) if proc_status == 200 else ""
        if proc_status == 200 and not episode_id:
            episode_id = self._poll_for_new_episode(agent_id, pre_uuids)

        if proc_status != 200:
            self._json_response(proc_status, {"error": "stack processing failed", "upstream": proc_payload})
            return

        response: dict[str, object] = {
            "agent_id": agent_id,
            "episode_id": episode_id,
            "api_key_source": api_key_source,
        }

        if not episode_id:
            response["episode_pending"] = True
            response["note"] = "Stack processed but no episode yet. Try again shortly."
            self._json_response(200, response)
            return

        episode_payload = self._fetch_episode_by_id(bonfire_id, episode_id)
        if episode_payload is None:
            ep_inline = proc_payload.get("episode")
            if isinstance(ep_inline, dict):
                episode_payload = ep_inline
        episode_summary = (
            self._extract_episode_summary(episode_payload)
            if episode_payload is not None
            else str(proc_payload.get("message") or proc_payload.get("detail") or f"Episode {episode_id}")
        )
        self._store.update_agent_context_from_episode(agent_id, episode_id, episode_summary)

        if player.current_room:
            self._try_pin_room_graph_entity(bonfire_id, player.current_room)

        # --- Step 2: GM reacts on the *separate* GM agent ---
        gm_decision: dict[str, object] = {}
        if gm_agent_id and DELVE_API_KEY and gm_agent_id != agent_id:
            game = self._store.get_game(bonfire_id)
            room_map = self._store.get_room_map(bonfire_id)
            room_summary = _build_room_structured_summary(self._store, bonfire_id)
            game_context: dict[str, object] = {
                "bonfire_id": bonfire_id,
                "game_prompt": game.game_prompt if game else "",
                "world_state_summary": game.world_state_summary if game else "",
                "last_gm_reaction": game.last_gm_reaction if game else "",
                "rooms": room_map.get("rooms", []),
                "player_positions": room_map.get("players", []),
            }
            gm_url = f"{DELVE_BASE_URL}/agents/{gm_agent_id}/chat"
            gm_status, gm_payload = _agent_json_request(
                "POST",
                gm_url,
                DELVE_API_KEY,
                body={
                    "message": (
                        "You are the Game Master for a shared world. Read the episode and return strict JSON "
                        '{"extension_awarded": int, "reaction": string, "world_state_update": string, '
                        '"room_movements": [{"agent_id": string, "to_room": string}], '
                        '"new_rooms": [{"name": string, "description": string, "connections": [string]}], '
                        '"room_updates": [{"room_id": string, "description": string}]}. '
                        "extension_awarded must be between 0 and 3. "
                        "room_movements moves players between known rooms when narratively appropriate. "
                        "new_rooms creates new areas for exploration (only when the story demands it). "
                        "room_updates changes descriptions of existing rooms as the world evolves. "
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
                    parsed = self._safe_json_object(reply)
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
                            "source": "gm_llm",
                        }

            # --- Step 2b: Queue GM response onto the GM agent's stack ---
            gm_reaction_text = str(gm_decision.get("reaction", "")).strip()
            gm_world_update = str(gm_decision.get("world_state_update", "")).strip()
            if gm_reaction_text or gm_world_update:
                now_iso = datetime.now(UTC).isoformat()
                stack_add_url = f"{DELVE_BASE_URL}/agents/{gm_agent_id}/stack/add"
                _agent_json_request(
                    "POST",
                    stack_add_url,
                    DELVE_API_KEY,
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
            gm_decision = _make_gm_decision(self._store, agent_id, episode_summary, episode_id, episode_payload)

        # --- Step 3: Apply GM decision (world state, room CRUD, movements) ---
        reaction = str(gm_decision.get("reaction", "")).strip()
        world_update = str(gm_decision.get("world_state_update", "")).strip()

        self._store.update_game_world_state(
            bonfire_id=bonfire_id,
            episode_id=episode_id,
            world_state_summary=world_update,
            gm_reaction=reaction,
        )
        self._store.update_agent_context_with_gm_response(
            agent_id=agent_id,
            episode_id=episode_id,
            gm_reaction=reaction,
            world_state_update=world_update,
        )

        ext = gm_decision.get("extension_awarded", 0)
        extension_awarded = ext if isinstance(ext, int) else 0
        if extension_awarded > 0:
            recharge = self._store.recharge_agent(bonfire_id, agent_id, extension_awarded, "gm_episode_extension")
            response["episode_extension"] = {"extension_awarded": extension_awarded, "recharge": recharge}

        # --- Step 4: Apply room CRUD and movements ---
        room_changes = _apply_gm_room_changes(self._store, bonfire_id, gm_decision)

        response["gm_decision"] = gm_decision
        response["room_changes"] = room_changes
        response["room_map"] = self._store.get_room_map(bonfire_id)

        game_obj = self._store.get_game(bonfire_id)
        if game_obj:
            response["world_state"] = {
                "world_state_summary": game_obj.world_state_summary,
                "last_gm_reaction": game_obj.last_gm_reaction,
                "last_episode_id": game_obj.last_episode_id,
            }

        self._json_response(200, response)

    # ------ Knowledge Map endpoints ------

    def _handle_graph_fetch(self, bonfire_id: str, agent_id: str) -> None:
        """Fetch graph data for the knowledge map by expanding recent episodes."""
        episode_uuids: list[str] = []
        if agent_id:
            episode_uuids = self._get_agent_episode_uuids(agent_id)
        if not episode_uuids:
            agent_ids = self._store.get_all_agent_ids()
            for aid in agent_ids:
                p = self._store.get_player(aid)
                if p and p.bonfire_id == bonfire_id:
                    episode_uuids = self._get_agent_episode_uuids(aid)
                    if episode_uuids:
                        break

        if not episode_uuids:
            self._json_response(200, {"nodes": [], "edges": [], "episodes": []})
            return

        uuids_batch = episode_uuids[-20:]
        url = f"{DELVE_BASE_URL}/knowledge_graph/episodes/expand"
        body: dict[str, object] = {
            "episode_uuids": uuids_batch,
            "bonfire_id": bonfire_id,
            "limit": 200,
        }
        status, payload = _json_request("POST", url, body)
        if status != 200:
            self._json_response(status, payload)
            return

        nodes = self._normalize_graph_nodes(payload.get("nodes") or payload.get("entities") or [])
        edges = self._normalize_graph_edges(payload.get("edges") or [])
        episodes = payload.get("episodes") or []

        self._json_response(200, {"nodes": nodes, "edges": edges, "episodes": episodes})

    def _handle_entity_expand(self, data: dict[str, object]) -> None:
        """Expand an entity node to reveal its neighborhood."""
        entity_uuid = self._required_string(data, "entity_uuid")
        bonfire_id = self._required_string(data, "bonfire_id")
        limit = data.get("limit", 50)
        if not isinstance(limit, int):
            limit = 50

        url = f"{DELVE_BASE_URL}/knowledge_graph/expand/entity"
        body: dict[str, object] = {
            "entity_uuid": entity_uuid,
            "bonfire_id": bonfire_id,
            "limit": limit,
        }
        status, payload = _json_request("POST", url, body)
        if status != 200:
            self._json_response(status, payload)
            return

        nodes = self._normalize_graph_nodes(payload.get("nodes") or payload.get("entities") or [])
        edges = self._normalize_graph_edges(payload.get("edges") or [])
        episodes = payload.get("episodes") or []

        self._json_response(200, {"nodes": nodes, "edges": edges, "episodes": episodes})

    @staticmethod
    def _normalize_graph_nodes(raw_nodes: object) -> list[dict[str, object]]:
        if not isinstance(raw_nodes, list):
            return []
        normalized: list[dict[str, object]] = []
        for n in raw_nodes:
            if not isinstance(n, dict):
                continue
            normalized.append({
                "uuid": n.get("uuid") or n.get("id") or "",
                "name": n.get("name") or n.get("label") or "?",
                "labels": n.get("labels") or [],
                "summary": n.get("summary") or n.get("description") or "",
                "group_id": n.get("group_id") or "",
            })
        return normalized

    @staticmethod
    def _normalize_graph_edges(raw_edges: object) -> list[dict[str, object]]:
        if not isinstance(raw_edges, list):
            return []
        normalized: list[dict[str, object]] = []
        for e in raw_edges:
            if not isinstance(e, dict):
                continue
            source = e.get("source_node_uuid") or e.get("source") or e.get("from") or ""
            target = e.get("target_node_uuid") or e.get("target") or e.get("to") or ""
            normalized.append({
                "uuid": e.get("uuid") or e.get("id") or "",
                "source": source,
                "target": target,
                "name": e.get("name") or e.get("fact") or e.get("label") or "",
                "fact": e.get("fact") or "",
            })
        return normalized

    def _handle_generate_quests(self, data: dict[str, object]) -> None:
        """Generate quests from knowledge graph entities using the GM agent."""
        bonfire_id = self._required_string(data, "bonfire_id")
        game = self._store.get_game(bonfire_id)
        if not game:
            self._json_response(404, {"error": "game_not_found"})
            return

        world_state = game.world_state_summary or game.game_prompt or ""
        query_text = world_state[:300] if world_state else "explore the world"

        delve_url = f"{DELVE_BASE_URL}/delve"
        delve_body: dict[str, object] = {
            "query": query_text,
            "bonfire_id": bonfire_id,
            "num_results": 15,
        }
        status, delve_payload = _json_request("POST", delve_url, delve_body)
        entities: list[dict[str, object]] = []
        if status == 200 and isinstance(delve_payload, dict):
            raw = delve_payload.get("entities") or delve_payload.get("nodes") or []
            if isinstance(raw, list):
                entities = [e for e in raw if isinstance(e, dict)]

        if not entities:
            self._json_response(200, {"quests": [], "note": "No graph entities available for quest generation"})
            return

        existing_keywords: set[str] = set()
        for q_dict in (self._store.quests_by_bonfire.get(bonfire_id) or {}).values():
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
            self._json_response(200, {"quests": [], "note": "All interesting entities already have active quests"})
            return

        owner_agent_id = game.gm_agent_id or ""
        created_quests: list[dict[str, object]] = []

        for ent in candidates:
            ent_name = str(ent.get("name") or "entity")
            ent_summary = str(ent.get("summary") or ent.get("description") or "")
            keyword = ent_name.lower().split()[0] if ent_name else "explore"

            if owner_agent_id and DELVE_API_KEY:
                gm_prompt = (
                    f"You are the Game Master. Generate a short quest (1-2 sentences) about investigating "
                    f"'{ent_name}' in the game world. Context: {ent_summary[:200]}. "
                    f"World state: {world_state[:200]}. "
                    f"Reply with ONLY a JSON object: "
                    f'{{"prompt": "quest text", "keyword": "single_word", "reward": 1}}'
                )
                gm_url = f"{DELVE_BASE_URL}/agents/{owner_agent_id}/chat"
                gm_status, gm_payload = _agent_json_request("POST", gm_url, DELVE_API_KEY, body={
                    "message": gm_prompt,
                    "graph_mode": "static",
                })
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
                        ent_name = f"Investigate {ent_name}: {reply[:100]}" if reply else f"Investigate {ent_name}"
                else:
                    reward = 1
                    ent_name = f"Investigate the entity known as '{ent_name}' and discover its role in the world."
            else:
                reward = 1
                ent_name = f"Investigate the entity known as '{ent_name}' and discover its role in the world."

            if keyword.lower() in existing_keywords:
                continue
            existing_keywords.add(keyword.lower())

            quest = self._store.create_quest(
                bonfire_id=bonfire_id,
                creator_wallet=game.owner_wallet,
                quest_type="graph_discovery",
                prompt=ent_name,
                keyword=keyword,
                reward=reward,
                cooldown_seconds=DEFAULT_CLAIM_COOLDOWN_SECONDS,
                expires_in_seconds=None,
            )
            created_quests.append({
                "quest_id": quest.quest_id,
                "quest_type": quest.quest_type,
                "prompt": quest.prompt,
                "keyword": quest.keyword,
                "reward": quest.reward,
                "entity_uuid": str(ent.get("uuid") or ""),
                "entity_name": str(ent.get("name") or ""),
            })

        self._json_response(200, {"quests": created_quests, "count": len(created_quests)})

    def _handle_backfill_world_state(self, data: dict[str, object]) -> None:
        """Recover world state from existing episodes without burning episode quota."""
        bonfire_id = self._required_string(data, "bonfire_id")
        requested_episode_id = str(data.get("episode_id") or "").strip()

        game = self._store.get_game(bonfire_id)
        if not game:
            self._json_response(404, {"error": "game_not_found", "bonfire_id": bonfire_id})
            return

        if requested_episode_id:
            episode_payload = _fetch_episode_payload(bonfire_id, requested_episode_id)
            episode_id = requested_episode_id
        else:
            episode_payload = None
            episode_id = ""
            agent_ids = self._store.get_all_agent_ids()
            for aid in agent_ids:
                player = self._store.get_player(aid)
                if player and player.bonfire_id == bonfire_id:
                    uuids = self._get_agent_episode_uuids(aid)
                    for uuid in reversed(uuids):
                        candidate = _fetch_episode_payload(bonfire_id, uuid)
                        if candidate:
                            episode_id = uuid
                            episode_payload = candidate
                            break
                    if episode_payload:
                        break

            if not episode_payload:
                episodes = self._fetch_bonfire_episodes(bonfire_id, 10)
                for ep in reversed(episodes):
                    candidate_id = self._extract_episode_id_from_payload(ep)
                    if candidate_id:
                        episode_id = candidate_id
                        episode_payload = ep
                        break

        if not episode_id or episode_payload is None:
            self._json_response(
                404,
                {"error": "no_episodes_found", "bonfire_id": bonfire_id, "detail": "No episodes available to backfill from."},
            )
            return

        episode_summary = self._extract_episode_summary(episode_payload)

        agent_ids = self._store.get_all_agent_ids()
        target_agent_id = ""
        for aid in agent_ids:
            player = self._store.get_player(aid)
            if player and player.bonfire_id == bonfire_id:
                target_agent_id = aid
                break

        if target_agent_id:
            self._store.update_agent_context_from_episode(target_agent_id, episode_id, episode_summary)

        gm_decision = _make_gm_decision(
            self._store,
            target_agent_id or agent_ids[0] if agent_ids else "",
            episode_summary,
            episode_id,
            episode_payload,
        )
        reaction = str(gm_decision.get("reaction", "")).strip()
        world_update = str(gm_decision.get("world_state_update", "")).strip()

        world_state = self._store.update_game_world_state(
            bonfire_id=bonfire_id,
            episode_id=episode_id,
            world_state_summary=world_update,
            gm_reaction=reaction,
        )

        if target_agent_id:
            self._store.update_agent_context_with_gm_response(
                agent_id=target_agent_id,
                episode_id=episode_id,
                gm_reaction=reaction,
                world_state_update=world_update,
            )

        self._json_response(200, {
            "backfilled": True,
            "episode_id": episode_id,
            "episode_summary": episode_summary,
            "gm_decision": gm_decision,
            "world_state": world_state,
            "agent_id": target_agent_id or None,
        })

    def _handle_process_all_stacks(self) -> None:
        if not DELVE_API_KEY:
            self._json_response(503, {"error": "DELVE_API_KEY is required for stack processing"})
            return
        result = _process_all_agent_stacks(self._store)
        self._json_response(200, result)

    def do_GET(self) -> None:
        path = self._strip_path()
        if path == "/":
            self.path = "/index.html"
            super().do_GET()
            return

        parsed = urllib.parse.urlparse(self.path)
        query = urllib.parse.parse_qs(parsed.query)

        if path == "/healthz":
            self._json_response(200, {"status": "ok"})
            return
        if path == "/game/state":
            bonfire_id = (query.get("bonfire_id") or [""])[0].strip()
            if not bonfire_id:
                self._json_response(400, {"error": "bonfire_id is required"})
                return
            self._json_response(200, self._store.get_state(bonfire_id))
            return
        if path == "/game/feed":
            bonfire_id = (query.get("bonfire_id") or [""])[0].strip()
            if not bonfire_id:
                self._json_response(400, {"error": "bonfire_id is required"})
                return
            limit_raw = (query.get("limit") or ["20"])[0]
            try:
                limit = max(1, min(int(limit_raw), 200))
            except ValueError:
                limit = 20
            events = self._store.get_events(bonfire_id, limit)
            episodes = self._fetch_bonfire_episodes(bonfire_id, limit)
            self._json_response(200, {"bonfire_id": bonfire_id, "events": events, "episodes": episodes})
            return
        if path == "/game/list-active":
            games = self._store.list_active_games()
            self._json_response(200, {"games": games})
            return
        if path == "/game/details":
            bonfire_id = (query.get("bonfire_id") or [""])[0].strip()
            if not bonfire_id:
                self._json_response(400, {"error": "bonfire_id is required"})
                return
            game = self._store.get_game(bonfire_id)
            if not game or game.status != "active":
                self._json_response(404, {"error": "active game not found"})
                return
            state = self._store.get_state(bonfire_id)
            events = self._store.get_events(bonfire_id, 50)
            self._json_response(
                200,
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
                },
            )
            return
        if path == "/game/bonfire/pricing":
            bonfire_id = (query.get("bonfire_id") or [""])[0].strip()
            if not bonfire_id:
                self._json_response(400, {"error": "bonfire_id is required"})
                return
            status, payload = self._fetch_bonfire_pricing(bonfire_id)
            self._json_response(status, payload)
            return
        if path == "/game/config":
            self._json_response(
                200,
                {
                    "erc8004_registry_address": ERC8004_REGISTRY_ADDRESS,
                    "payment": {
                        "network": PAYMENT_NETWORK,
                        "source_network": PAYMENT_SOURCE_NETWORK,
                        "destination_network": PAYMENT_DESTINATION_NETWORK,
                        "token_address": PAYMENT_TOKEN_ADDRESS,
                        "chain_id": PAYMENT_CHAIN_ID,
                        "default_amount": PAYMENT_DEFAULT_AMOUNT,
                        "intermediary_address": ONCHAINFI_INTERMEDIARY_ADDRESS,
                    },
                },
            )
            return
        if path == "/game/wallet/provision-records":
            wallet_address = (query.get("wallet_address") or [""])[0].strip().lower()
            if not wallet_address:
                self._json_response(400, {"error": "wallet_address is required"})
                return
            records = self._fetch_provision_records_for_wallet(wallet_address)
            self._json_response(200, {"wallet_address": wallet_address, "records": records})
            return
        if path == "/game/wallet/bonfires":
            wallet_address = (query.get("wallet_address") or [""])[0].strip().lower()
            if not wallet_address:
                self._json_response(400, {"error": "wallet_address is required"})
                return
            bonfires = self._fetch_owned_bonfires_for_wallet(wallet_address)
            self._json_response(200, {"wallet_address": wallet_address, "bonfires": bonfires})
            return
        if path == "/game/wallet/purchased-agents":
            wallet_address = (query.get("wallet_address") or [""])[0].strip().lower()
            if not wallet_address:
                self._json_response(400, {"error": "wallet_address is required"})
                return
            bonfire_id = (query.get("bonfire_id") or [""])[0].strip()
            if not bonfire_id:
                self._json_response(400, {"error": "bonfire_id is required"})
                return
            purchased_agents = self._fetch_wallet_purchased_agents(wallet_address, bonfire_id)
            self._json_response(
                200,
                {
                    "wallet_address": wallet_address,
                    "bonfire_id": bonfire_id,
                    "agents": purchased_agents,
                },
            )
            return
        if path == "/game/stack/timer/status":
            timer = self._stack_timer
            gm = self._gm_timer
            self._json_response(
                200,
                {
                    "enabled": timer is not None,
                    "is_running": timer.is_running if timer else False,
                    "interval_seconds": STACK_PROCESS_INTERVAL_SECONDS,
                    "last_run_at": timer.last_run_at if timer else None,
                    "last_result": timer.last_result if timer else None,
                    "gm_timer": {
                        "enabled": gm is not None,
                        "is_running": gm.is_running if gm else False,
                        "interval_seconds": GM_BATCH_INTERVAL_SECONDS,
                        "last_run_at": gm.last_run_at if gm else None,
                        "last_result": gm.last_result if gm else None,
                    },
                },
            )
            return

        if path == "/game/room/chat":
            room_id = (query.get("room_id") or [""])[0].strip()
            if not room_id:
                self._json_response(400, {"error": "room_id is required"})
                return
            limit_str = (query.get("limit") or ["50"])[0].strip()
            limit = min(int(limit_str), 200) if limit_str.isdigit() else 50
            messages = self._store.get_room_messages(room_id, limit=limit)
            self._json_response(200, {"room_id": room_id, "messages": messages})
            return

        if path == "/game/map":
            bonfire_id = (query.get("bonfire_id") or [""])[0].strip()
            if not bonfire_id:
                self._json_response(400, {"error": "bonfire_id is required"})
                return
            self._json_response(200, self._store.get_room_map(bonfire_id))
            return

        if path == "/game/graph":
            bonfire_id = (query.get("bonfire_id") or [""])[0].strip()
            agent_id = (query.get("agent_id") or [""])[0].strip()
            if not bonfire_id:
                self._json_response(400, {"error": "bonfire_id is required"})
                return
            self._handle_graph_fetch(bonfire_id, agent_id)
            return

        super().do_GET()

    def do_POST(self) -> None:
        path = self._strip_path()
        try:
            data = self._read_json_body()
        except json.JSONDecodeError:
            self._json_response(400, {"error": "invalid JSON"})
            return

        try:
            if path.startswith("/game/purchase-agent/"):
                bonfire_id = path.split("/game/purchase-agent/", 1)[-1].strip()
                if not bonfire_id:
                    self._json_response(400, {"error": "bonfire_id is required"})
                    return
                self._handle_purchase_proxy(bonfire_id, data)
                return
            if path == "/game/purchased-agents/reveal-nonce":
                self._handle_reveal_nonce_proxy(data)
                return
            if path == "/game/purchased-agents/reveal-api-key":
                self._handle_reveal_api_key_proxy(data)
                return
            if path == "/game/agents/reveal-nonce-selected":
                self._handle_reveal_nonce_selected(data)
                return
            if path == "/game/agents/reveal-api-key-selected":
                self._handle_reveal_api_key_selected(data)
                return

            if path == "/game/bonfire/link":
                bonfire_id = self._required_string(data, "bonfire_id")
                erc8004_bonfire_id = self._required_int(data, "erc8004_bonfire_id")
                wallet_address = self._required_string(data, "wallet_address").lower()
                owner_wallet = self._resolve_owner_wallet(erc8004_bonfire_id).lower()
                if owner_wallet != wallet_address:
                    self._json_response(
                        403,
                        {
                            "error": "wallet does not own bonfire NFT",
                            "owner_wallet": owner_wallet,
                        },
                    )
                    return
                linked = self._store.link_bonfire(
                    bonfire_id=bonfire_id,
                    erc8004_bonfire_id=erc8004_bonfire_id,
                    owner_wallet=owner_wallet,
                )
                self._json_response(200, dict(linked))
                return

            if path == "/game/agents/register-purchase":
                self._handle_register_purchase(data)
                return

            if path == "/game/agents/register-selected":
                self._handle_register_selected_agent(data)
                return
            if path == "/game/create":
                self._handle_create_game(data)
                return
            if path == "/game/player/restore":
                self._handle_restore_players(data)
                return

            if path == "/game/agents/complete":
                self._handle_agent_completion(data)
                return

            if path == "/game/agents/end-turn":
                self._handle_end_turn(data)
                return

            if path == "/game/agents/process-stack":
                self._handle_process_stack(data)
                return

            if path == "/game/agents/gm-react":
                self._handle_trigger_gm_reaction(data)
                return

            if path == "/game/world/generate-episode":
                self._handle_generate_world_episode(data)
                return

            if path == "/game/stack/process-all":
                self._handle_process_all_stacks()
                return

            if path == "/game/admin/backfill-world-state":
                self._handle_backfill_world_state(data)
                return

            if path == "/game/turn":
                agent_id = self._required_string(data, "agent_id")
                action = self._required_string(data, "action")
                out = self._store.run_turn(agent_id=agent_id, action=action)
                self._json_response(200, out)
                return

            if path == "/game/quests/create":
                bonfire_id = self._required_string(data, "bonfire_id")
                creator_wallet = self._required_string(data, "wallet_address")
                self._assert_owner(bonfire_id, creator_wallet)
                reward = self._required_int(data, "reward")
                cooldown_raw = data.get("cooldown_seconds", DEFAULT_CLAIM_COOLDOWN_SECONDS)
                if isinstance(cooldown_raw, int):
                    cooldown = cooldown_raw
                else:
                    raise ValueError("cooldown_seconds must be an integer")
                expires_in_seconds = data.get("expires_in_seconds")
                expires_int: int | None = None
                if isinstance(expires_in_seconds, int):
                    expires_int = expires_in_seconds
                quest = self._store.create_quest(
                    bonfire_id=bonfire_id,
                    creator_wallet=creator_wallet,
                    quest_type=self._required_string(data, "quest_type"),
                    prompt=self._required_string(data, "prompt"),
                    keyword=self._required_string(data, "keyword"),
                    reward=reward,
                    cooldown_seconds=cooldown,
                    expires_in_seconds=expires_int,
                )
                self._json_response(
                    200,
                    {
                        "quest_id": quest.quest_id,
                        "quest_type": quest.quest_type,
                        "reward": quest.reward,
                        "status": quest.status,
                    },
                )
                return

            if path == "/game/quests/claim":
                quest_id = self._required_string(data, "quest_id")
                agent_id = self._required_string(data, "agent_id")
                submission = self._required_string(data, "submission")
                out = self._store.claim_quest(quest_id=quest_id, agent_id=agent_id, submission=submission)
                self._json_response(200, out)
                return

            if path == "/game/agents/recharge":
                bonfire_id = self._required_string(data, "bonfire_id")
                wallet = self._required_string(data, "wallet_address")
                self._assert_owner(bonfire_id, wallet)
                out = self._store.recharge_agent(
                    bonfire_id=bonfire_id,
                    agent_id=self._required_string(data, "agent_id"),
                    amount=self._required_int(data, "amount"),
                    reason=self._required_string(data, "reason"),
                )
                self._json_response(200, out)
                return

            if path == "/game/map/init":
                bonfire_id = self._required_string(data, "bonfire_id")
                room_id = self._store.ensure_starting_room(bonfire_id)
                if not room_id:
                    self._json_response(404, {"error": "game not found for bonfire"})
                    return
                self._json_response(200, self._store.get_room_map(bonfire_id))
                return

            if path == "/game/entity/expand":
                self._handle_entity_expand(data)
                return

            if path == "/game/quests/generate":
                self._handle_generate_quests(data)
                return

            self._json_response(404, {"error": "not found"})
        except ValueError as exc:
            self._json_response(400, {"error": str(exc)})
        except PermissionError as exc:
            detail = str(exc)
            if detail == "episode_quota_exhausted":
                self._json_response(
                    429,
                    {"error": "episode_quota_exhausted", "message": "Agent has no remaining episodes"},
                )
                return
            self._json_response(403, {"error": detail})
        except InvalidOperation as exc:
            self._json_response(400, {"error": f"invalid decimal operation: {exc}"})
        except Exception as exc:
            self._json_response(500, {"error": f"internal server error: {exc}"})

    def log_message(self, fmt: str, *args: object) -> None:
        path = str(args[0]) if args else ""
        if "/healthz" in path or "favicon" in path:
            return
        print(f"  [{self.command}] {path}")


def _handler_factory(
    store: GameStore,
    resolver: Callable[[int], str],
    stack_timer: StackTimerRunner | None = None,
    gm_timer: GmBatchTimerRunner | None = None,
):
    class Handler(QuestGameHandler):
        def __init__(self, *args, **kwargs):
            super().__init__(
                *args,
                store=store,
                resolve_owner_wallet=resolver,
                stack_timer=stack_timer,
                gm_timer=gm_timer,
                **kwargs,
            )

    return Handler


if __name__ == "__main__":
    store = GameStore(storage_path=GAME_STORE_PATH)
    timer = StackTimerRunner(store=store, interval_seconds=STACK_PROCESS_INTERVAL_SECONDS)
    timer.start()
    gm_timer = GmBatchTimerRunner(store=store, interval_seconds=GM_BATCH_INTERVAL_SECONDS)
    gm_timer.start()
    Handler = _handler_factory(store, _resolve_owner_wallet_default, stack_timer=timer, gm_timer=gm_timer)
    socketserver.ThreadingTCPServer.allow_reuse_address = True

    with socketserver.ThreadingTCPServer(("0.0.0.0", PORT), Handler) as httpd:
        print(
            f"""
  Bonfire Quest Game server
  http://localhost:{PORT}
  POST /game/bonfire/link
  POST /game/agents/register-purchase
  POST /game/agents/end-turn
  POST /game/quests/create
  POST /game/quests/claim
  POST /game/turn
  POST /game/agents/recharge
  POST /game/stack/process-all
  GET  /game/state?bonfire_id=...
  GET  /game/feed?bonfire_id=...
  GET  /game/map?bonfire_id=...
  GET  /game/stack/timer/status
"""
        )
        try:
            httpd.serve_forever()
        except KeyboardInterrupt:
            print("\n  Shutting down.")
        finally:
            timer.stop()
            gm_timer.stop()
