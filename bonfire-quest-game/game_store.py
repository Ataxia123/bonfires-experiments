"""In-memory game store and business rules."""

from __future__ import annotations

import json
import threading
import uuid
from dataclasses import asdict
from datetime import UTC, datetime, timedelta
from pathlib import Path

import game_config as config
from models import (
    AttemptState,
    GameState,
    NpcState,
    ObjectState,
    PlayerState,
    QuestState,
    RoomState,
)


class GameStore:
    """In-memory game store and business rules."""

    def __init__(self, storage_path: Path | None = None) -> None:
        self._lock = threading.Lock()
        self._storage_path = Path(storage_path or config.GAME_STORE_PATH)
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
        self.npcs_by_game: dict[str, dict[str, NpcState]] = {}
        self.objects_by_game: dict[str, dict[str, ObjectState]] = {}
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
            "npcs_by_game": {
                bid: {nid: asdict(npc) for nid, npc in npcs.items()}
                for bid, npcs in self.npcs_by_game.items()
            },
            "objects_by_game": {
                bid: {oid: asdict(obj) for oid, obj in objs.items()}
                for bid, objs in self.objects_by_game.items()
            },
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

        npcs_obj = payload.get("npcs_by_game")
        if isinstance(npcs_obj, dict):
            for bid, npc_map in npcs_obj.items():
                if not isinstance(npc_map, dict):
                    continue
                loaded: dict[str, NpcState] = {}
                for nid, npc_data in npc_map.items():
                    if not isinstance(npc_data, dict):
                        continue
                    try:
                        loaded[str(nid)] = NpcState(**npc_data)
                    except TypeError:
                        continue
                self.npcs_by_game[str(bid)] = loaded

        objects_obj = payload.get("objects_by_game")
        if isinstance(objects_obj, dict):
            for bid, obj_map in objects_obj.items():
                if not isinstance(obj_map, dict):
                    continue
                loaded_objs: dict[str, ObjectState] = {}
                for oid, obj_data in obj_map.items():
                    if not isinstance(obj_data, dict):
                        continue
                    try:
                        loaded_objs[str(oid)] = ObjectState(**obj_data)
                    except TypeError:
                        continue
                self.objects_by_game[str(bid)] = loaded_objs

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
            npcs_by_room: dict[str, list[dict[str, object]]] = {}
            for npc in self.npcs_by_game.get(bonfire_id, {}).values():
                if not npc.is_active:
                    continue
                npcs_by_room.setdefault(npc.room_id, []).append({
                    "npc_id": npc.npc_id, "name": npc.name,
                    "description": npc.description, "personality": npc.personality,
                })
            objects_by_room: dict[str, list[dict[str, object]]] = {}
            for obj in self.objects_by_game.get(bonfire_id, {}).values():
                if obj.is_consumed:
                    continue
                if obj.properties.get("location_type") == "room":
                    rid = str(obj.properties.get("location_id", ""))
                    if rid:
                        objects_by_room.setdefault(rid, []).append({
                            "object_id": obj.object_id, "name": obj.name,
                            "obj_type": obj.obj_type, "description": obj.description,
                        })
            return {
                "rooms": rooms, "players": players,
                "npcs_by_room": npcs_by_room, "objects_by_room": objects_by_room,
            }

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

    # ── NPC management ──

    def create_npc(
        self,
        bonfire_id: str,
        name: str,
        room_id: str,
        personality: str,
        description: str = "",
        dialogue_style: str = "",
        graph_entity_uuid: str = "",
    ) -> NpcState:
        with self._lock:
            game = self.games_by_bonfire.get(bonfire_id)
            if not game:
                raise ValueError(f"No game for bonfire {bonfire_id}")
            npc = NpcState(
                npc_id=str(uuid.uuid4()),
                name=name,
                room_id=room_id,
                personality=personality,
                description=description,
                dialogue_style=dialogue_style,
                graph_entity_uuid=graph_entity_uuid,
            )
            self.npcs_by_game.setdefault(bonfire_id, {})[npc.npc_id] = npc
            self._persist_locked()
            return npc

    def get_npc(self, bonfire_id: str, npc_id: str) -> NpcState | None:
        with self._lock:
            return self.npcs_by_game.get(bonfire_id, {}).get(npc_id)

    def get_npcs_in_room(self, bonfire_id: str, room_id: str) -> list[NpcState]:
        with self._lock:
            return [
                npc for npc in self.npcs_by_game.get(bonfire_id, {}).values()
                if npc.room_id == room_id and npc.is_active
            ]

    def update_npc(
        self,
        bonfire_id: str,
        npc_id: str,
        room_id: str | None = None,
        personality: str | None = None,
        description: str | None = None,
    ) -> bool:
        with self._lock:
            npc = self.npcs_by_game.get(bonfire_id, {}).get(npc_id)
            if not npc:
                return False
            if room_id is not None:
                npc.room_id = room_id
            if personality is not None:
                npc.personality = personality
            if description is not None:
                npc.description = description
            self._persist_locked()
            return True

    def remove_npc(self, bonfire_id: str, npc_id: str) -> bool:
        with self._lock:
            npc = self.npcs_by_game.get(bonfire_id, {}).get(npc_id)
            if not npc:
                return False
            npc.is_active = False
            self._persist_locked()
            return True

    # ── Object / Inventory management ──

    def create_object(
        self,
        bonfire_id: str,
        name: str,
        description: str,
        obj_type: str = "artifact",
        properties: dict[str, str] | None = None,
        graph_entity_uuid: str = "",
    ) -> ObjectState:
        with self._lock:
            game = self.games_by_bonfire.get(bonfire_id)
            if not game:
                raise ValueError(f"No game for bonfire {bonfire_id}")
            obj = ObjectState(
                object_id=str(uuid.uuid4()),
                name=name,
                description=description,
                obj_type=obj_type,
                properties=properties or {},
                graph_entity_uuid=graph_entity_uuid,
            )
            self.objects_by_game.setdefault(bonfire_id, {})[obj.object_id] = obj
            self._persist_locked()
            return obj

    def get_object(self, bonfire_id: str, object_id: str) -> ObjectState | None:
        with self._lock:
            return self.objects_by_game.get(bonfire_id, {}).get(object_id)

    def get_objects_in_room(self, bonfire_id: str, room_id: str) -> list[ObjectState]:
        """Return non-consumed objects located in a room (stored in room properties)."""
        with self._lock:
            room_objects: list[ObjectState] = []
            for obj in self.objects_by_game.get(bonfire_id, {}).values():
                if obj.is_consumed:
                    continue
                if obj.properties.get("location_type") == "room" and obj.properties.get("location_id") == room_id:
                    room_objects.append(obj)
            return room_objects

    def grant_object_to_player(self, bonfire_id: str, agent_id: str, object_id: str) -> bool:
        with self._lock:
            player = self.players_by_agent.get(agent_id)
            obj = self.objects_by_game.get(bonfire_id, {}).get(object_id)
            if not player or not obj or obj.is_consumed:
                return False
            obj.properties["location_type"] = "player"
            obj.properties["location_id"] = agent_id
            if object_id not in player.inventory:
                player.inventory.append(object_id)
            self._persist_locked()
            return True

    def grant_object_to_npc(self, bonfire_id: str, npc_id: str, object_id: str) -> bool:
        with self._lock:
            npc = self.npcs_by_game.get(bonfire_id, {}).get(npc_id)
            obj = self.objects_by_game.get(bonfire_id, {}).get(object_id)
            if not npc or not obj or obj.is_consumed:
                return False
            obj.properties["location_type"] = "npc"
            obj.properties["location_id"] = npc_id
            if object_id not in npc.inventory:
                npc.inventory.append(object_id)
            self._persist_locked()
            return True

    def drop_object_in_room(self, bonfire_id: str, room_id: str, object_id: str) -> bool:
        with self._lock:
            obj = self.objects_by_game.get(bonfire_id, {}).get(object_id)
            if not obj or obj.is_consumed:
                return False
            prev_loc_type = obj.properties.get("location_type", "")
            prev_loc_id = obj.properties.get("location_id", "")
            if prev_loc_type == "player":
                player = self.players_by_agent.get(prev_loc_id)
                if player and object_id in player.inventory:
                    player.inventory.remove(object_id)
            elif prev_loc_type == "npc":
                npc = self.npcs_by_game.get(bonfire_id, {}).get(prev_loc_id)
                if npc and object_id in npc.inventory:
                    npc.inventory.remove(object_id)
            obj.properties["location_type"] = "room"
            obj.properties["location_id"] = room_id
            self._persist_locked()
            return True

    def use_object(self, bonfire_id: str, agent_id: str, object_id: str) -> dict[str, object]:
        """Apply an object's effects and return a result dict."""
        with self._lock:
            player = self.players_by_agent.get(agent_id)
            obj = self.objects_by_game.get(bonfire_id, {}).get(object_id)
            if not player or not obj or obj.is_consumed:
                return {"success": False, "error": "object_not_found_or_consumed"}
            if object_id not in player.inventory:
                return {"success": False, "error": "not_in_inventory"}

            effects: list[str] = []
            unlocks_room = obj.properties.get("unlocks_room")
            if unlocks_room:
                game = self.games_by_bonfire.get(bonfire_id)
                if game and player.current_room:
                    for room in game.rooms:
                        if isinstance(room, dict) and room.get("room_id") == player.current_room:
                            conns = room.setdefault("connections", [])
                            if isinstance(conns, list) and unlocks_room not in conns:
                                conns.append(unlocks_room)
                                effects.append(f"Unlocked passage to {unlocks_room}")
                            break

            reveals_entity = obj.properties.get("reveals_entity")
            if reveals_entity:
                effects.append(f"Revealed entity {reveals_entity}")

            if obj.obj_type == "consumable":
                obj.is_consumed = True
                player.inventory.remove(object_id)
                effects.append("Item consumed")
            self._persist_locked()
            return {"success": True, "effects": effects, "object": asdict(obj)}

    def get_player_inventory(self, bonfire_id: str, agent_id: str) -> list[dict[str, object]]:
        with self._lock:
            player = self.players_by_agent.get(agent_id)
            if not player:
                return []
            result: list[dict[str, object]] = []
            for oid in player.inventory:
                obj = self.objects_by_game.get(bonfire_id, {}).get(oid)
                if obj and not obj.is_consumed:
                    result.append(asdict(obj))
            return result

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
