#!/usr/bin/env python3
"""Shared-bonfire quest game demo server.

This demo integrates with existing Delve endpoints for purchased-agent reveal
flow and resolves ERC-8004 ownership to gate Game Master actions.

Modules:
    game_config      - Environment variables and constants
    models           - Dataclass definitions (PlayerState, GameState, etc.)
    http_client      - Low-level HTTP request helpers
    game_store       - GameStore class (persistence + business rules)
    gm_engine        - Game Master decision logic
    stack_processing - Episode / stack processing utilities
    timers           - Background timer runners
    handler          - FastAPI APIRouter with all game routes
    app              - FastAPI application factory
"""

from __future__ import annotations

import asyncio
import importlib
import sys
from pathlib import Path
from typing import Callable

import uvicorn

import game_config as config
from app import create_app
from game_store import GameStore

from timers import GmBatchTimerRunner, StackTimerRunner


def _resolve_owner_wallet_default(erc8004_bonfire_id: int) -> str:
    """Resolve owner wallet via existing EthereumRpcService."""
    repo_root: Path | None = None
    for candidate in [config.GAME_DIR, *config.GAME_DIR.parents]:
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


def _handler_factory(
    store: GameStore,
    resolver: Callable[[int], str],
    stack_timer: StackTimerRunner | None = None,
    gm_timer: GmBatchTimerRunner | None = None,
):
    """Compatibility shim used by tests to create a preconfigured app."""
    return create_app(
        store=store,
        resolve_owner_wallet=resolver,
        stack_timer=stack_timer,
        gm_timer=gm_timer,
    )


if __name__ == "__main__":
    app = create_app(resolve_owner_wallet=_resolve_owner_wallet_default)
    print(
        f"""
  Bonfire Quest Game server
  http://localhost:{config.PORT}
  POST /game/bonfire/link
  POST /game/agents/register-purchase
  POST /game/agents/end-turn
  POST /game/quests/create
  POST /game/quests/claim
  POST /game/agents/recharge
  POST /game/stack/process-all
  GET  /game/state?bonfire_id=...
  GET  /game/feed?bonfire_id=...
  GET  /game/map?bonfire_id=...
  GET  /game/stack/timer/status
"""
    )
    uvicorn.run(app, host="0.0.0.0", port=config.PORT)
