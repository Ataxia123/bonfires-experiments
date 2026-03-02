"""Game state dataclass definitions."""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime


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
    inventory: list[str] = field(default_factory=list)

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
    dataroom_id: str = ""
    image_url: str = ""
    latest_hyperblog_id: str = ""
    latest_summary: str = ""


@dataclass
class NpcState:
    npc_id: str
    name: str
    room_id: str
    personality: str
    description: str = ""
    dialogue_style: str = ""
    graph_entity_uuid: str = ""
    inventory: list[str] = field(default_factory=list)
    is_active: bool = True


@dataclass
class ObjectState:
    object_id: str
    name: str
    description: str
    obj_type: str = "artifact"
    properties: dict[str, str] = field(default_factory=dict)
    graph_entity_uuid: str = ""
    is_consumed: bool = False


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
