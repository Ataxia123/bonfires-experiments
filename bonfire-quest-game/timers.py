"""Background timer runners for periodic stack processing."""

from __future__ import annotations

import threading
from datetime import UTC, datetime

import stack_processing
from game_store import GameStore


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
                self.last_result = stack_processing._process_all_agent_stacks(self._store)
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
                self.last_result = stack_processing._process_gm_stacks(self._store)
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
