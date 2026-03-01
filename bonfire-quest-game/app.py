"""FastAPI application factory for the bonfire quest game."""

from __future__ import annotations

from contextlib import asynccontextmanager
from decimal import InvalidOperation
from typing import AsyncGenerator, Callable

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles

import game_config as config
from game_store import GameStore
from timers import GmBatchTimerRunner, StackTimerRunner


def _register_exception_handlers(app: FastAPI) -> None:
    @app.exception_handler(ValueError)
    async def _value_error(request: Request, exc: ValueError) -> JSONResponse:
        return JSONResponse(status_code=400, content={"error": str(exc)})

    @app.exception_handler(PermissionError)
    async def _permission_error(request: Request, exc: PermissionError) -> JSONResponse:
        detail = str(exc)
        if detail == "episode_quota_exhausted":
            return JSONResponse(
                status_code=429,
                content={"error": "episode_quota_exhausted", "message": "Agent has no remaining episodes"},
            )
        return JSONResponse(status_code=403, content={"error": detail})

    @app.exception_handler(InvalidOperation)
    async def _invalid_op(request: Request, exc: InvalidOperation) -> JSONResponse:
        return JSONResponse(status_code=400, content={"error": f"invalid decimal operation: {exc}"})

    @app.exception_handler(Exception)
    async def _generic(request: Request, exc: Exception) -> JSONResponse:
        return JSONResponse(status_code=500, content={"error": f"internal server error: {exc}"})


def create_app(
    store: GameStore | None = None,
    resolve_owner_wallet: Callable[[int], str] | None = None,
    stack_timer: StackTimerRunner | None = None,
    gm_timer: GmBatchTimerRunner | None = None,
) -> FastAPI:
    """Create and configure the FastAPI application.

    When called without arguments (production), the store and timers are
    created inside the lifespan context.  When called with explicit arguments
    (tests), those objects are used directly and no timers are managed.
    """
    _provided_store = store
    _provided_resolve = resolve_owner_wallet
    _provided_stack_timer = stack_timer
    _provided_gm_timer = gm_timer

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncGenerator[None, None]:
        if _provided_store is not None:
            # State already set synchronously below — just run.
            yield
            return

        _store = GameStore(storage_path=config.GAME_STORE_PATH)
        _stack_timer = StackTimerRunner(
            store=_store, interval_seconds=config.STACK_PROCESS_INTERVAL_SECONDS
        )
        _gm_timer = GmBatchTimerRunner(
            store=_store, interval_seconds=config.GM_BATCH_INTERVAL_SECONDS
        )
        _stack_timer.start()
        _gm_timer.start()
        app.state.store = _store
        app.state.resolve_owner_wallet = _noop_resolver
        app.state.stack_timer = _stack_timer
        app.state.gm_timer = _gm_timer
        yield
        _stack_timer.stop()
        _gm_timer.stop()

    app = FastAPI(title="Bonfire Quest Game", lifespan=lifespan)

    # When explicit dependencies are provided (e.g. tests), set state immediately
    # so the app works without triggering the lifespan context.
    if _provided_store is not None:
        app.state.store = _provided_store
        app.state.resolve_owner_wallet = _provided_resolve or _noop_resolver
        app.state.stack_timer = _provided_stack_timer
        app.state.gm_timer = _provided_gm_timer

    app.add_middleware(
        CORSMiddleware,
        allow_origins=["*"],
        allow_methods=["*"],
        allow_headers=["*"],
        max_age=600,
    )

    _register_exception_handlers(app)

    from handler import router  # noqa: PLC0415 — avoids circular import at module load
    app.include_router(router)

    app.mount("/", StaticFiles(directory=str(config.GAME_DIR), html=True), name="static")

    return app


def _noop_resolver(erc8004_bonfire_id: int) -> str:  # pragma: no cover
    raise RuntimeError("resolve_owner_wallet not configured")
