"""Background polling worker for Project Forge.

Runs as a daemon thread started by server.py on boot.
Invokes the LangGraph forge pipeline on each poll cycle and persists results.
"""

import asyncio
import json
import os
import tempfile
import threading
import time
import traceback
from datetime import datetime, timezone
from pathlib import Path

from forge_graph import forge_graph

FORGE_DIR = Path(__file__).parent
MOCKUPS_DIR = FORGE_DIR / "mockups"

POLL_INTERVAL = int(os.environ.get("POLL_INTERVAL", 6 * 60 * 60))  # 6 hours
CHANGE_THRESHOLD = float(os.environ.get("CHANGE_THRESHOLD", 0.3))
MAX_VERSIONS = int(os.environ.get("MAX_VERSIONS", 10))
MAX_POLL_LOG = 50


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _slugify(name: str) -> str:
    return name.lower().replace(" ", "-").replace("/", "-").replace("'", "")[:60]


def _find_project(state: dict, project_id: str) -> dict | None:
    for p in state.get("projects", []):
        if p["id"] == project_id:
            return p
    return None


def _default_state() -> dict:
    return {
        "version": 1,
        "last_poll_at": None,
        "last_generation_at": None,
        "poll_count": 0,
        "generation_count": 0,
        "kg_snapshot": {},
        "projects": [],
        "poll_log": [],
    }


class ForgeWorker:
    def __init__(self):
        self.lock = threading.Lock()
        self.running = False
        self.thread: threading.Thread | None = None
        self.status = "idle"  # idle | polling | generating | error
        self.last_error: str | None = None
        self.current_bonfire_id: str | None = None

    def _state_file(self, bonfire_id: str | None = None) -> Path:
        """Return the state file path for a given bonfire."""
        bid = bonfire_id or self.current_bonfire_id
        if not bid:
            return FORGE_DIR / "forge_state.json"
        return FORGE_DIR / f"forge_state_{bid}.json"

    def _restore_current_bonfire(self):
        """Restore current_bonfire_id from the most recently modified state file."""
        best_mtime = 0.0
        best_bid: str | None = None
        for f in FORGE_DIR.glob("forge_state_*.json"):
            stem = f.stem  # e.g. forge_state_abc123
            bid = stem[len("forge_state_"):]
            if bid and f.stat().st_mtime > best_mtime:
                best_mtime = f.stat().st_mtime
                best_bid = bid
        if best_bid:
            self.current_bonfire_id = best_bid
            print(f"  [worker] Restored current_bonfire_id={best_bid}")

    def set_current_bonfire(self, bonfire_id: str):
        """Update the current bonfire and log the switch."""
        if bonfire_id != self.current_bonfire_id:
            print(f"  [worker] Switching bonfire: {self.current_bonfire_id} → {bonfire_id}")
            self.current_bonfire_id = bonfire_id

    def load_state(self, bonfire_id: str | None = None) -> dict:
        state_path = self._state_file(bonfire_id)
        with self.lock:
            if state_path.exists():
                try:
                    return json.loads(state_path.read_text())
                except (json.JSONDecodeError, OSError):
                    return _default_state()
            return _default_state()

    def save_state(self, state: dict, bonfire_id: str | None = None):
        state_path = self._state_file(bonfire_id)
        bid = bonfire_id or self.current_bonfire_id
        if bid:
            state["bonfire_id"] = bid
        with self.lock:
            FORGE_DIR.mkdir(parents=True, exist_ok=True)
            tmp = tempfile.NamedTemporaryFile(
                mode="w", dir=str(FORGE_DIR), suffix=".tmp", delete=False
            )
            try:
                json.dump(state, tmp, indent=2)
                tmp.close()
                os.rename(tmp.name, str(state_path))
            except Exception:
                tmp.close()
                try:
                    os.unlink(tmp.name)
                except OSError:
                    pass
                raise

    def start(self):
        if self.running:
            return
        self._restore_current_bonfire()
        self.running = True
        self.thread = threading.Thread(target=self._poll_loop, daemon=True)
        self.thread.start()
        print("  [worker] Background polling started")

    def stop(self):
        self.running = False

    def trigger_now(self, bonfire_id: str | None = None):
        """Force a poll cycle immediately (admin endpoint)."""
        if bonfire_id:
            self.set_current_bonfire(bonfire_id)
        threading.Thread(target=self._do_poll_cycle, daemon=True).start()

    def get_status(self, bonfire_id: str | None = None) -> dict:
        state = self.load_state(bonfire_id)
        return {
            "status": self.status,
            "current_bonfire_id": self.current_bonfire_id,
            "last_error": self.last_error,
            "last_poll_at": state.get("last_poll_at"),
            "last_generation_at": state.get("last_generation_at"),
            "poll_count": state.get("poll_count", 0),
            "generation_count": state.get("generation_count", 0),
            "project_count": len(state.get("projects", [])),
            "poll_interval_seconds": POLL_INTERVAL,
            "change_threshold": CHANGE_THRESHOLD,
            "poll_log": state.get("poll_log", [])[-10:],
        }

    # -- Internal --

    def _poll_loop(self):
        if not self.current_bonfire_id:
            self._restore_current_bonfire()

        if self.current_bonfire_id:
            state = self.load_state()
            if not state.get("projects"):
                print("  [worker] First boot — running initial generation")
                self._do_poll_cycle()
        else:
            print("  [worker] No current bonfire set — waiting for first request")

        while self.running:
            time.sleep(POLL_INTERVAL)
            if self.running and self.current_bonfire_id:
                self._do_poll_cycle()

    def _do_poll_cycle(self):
        if not self.current_bonfire_id:
            print("  [worker] Skipping poll — no current bonfire set")
            return

        bonfire_id = self.current_bonfire_id
        try:
            self.status = "polling"
            print(f"  [worker] Polling KG for bonfire={bonfire_id} at {_now_iso()}")
            state = self.load_state(bonfire_id)

            # 1. Extract inputs from state file
            old_kg_snapshot = state.get("kg_snapshot", {})
            existing_projects: list[dict] = []
            project_versions: dict[str, int] = {}
            for p in state.get("projects", []):
                if p.get("versions"):
                    existing_projects.append(p["versions"][-1]["project_data"])
                project_versions[p["id"]] = p.get("current_version", 0)

            is_first_run = len(state.get("projects", [])) == 0

            # 2. Build ForgeState input for the graph
            initial_state = {
                "bonfire_id": bonfire_id,
                "is_first_run": is_first_run,
                "existing_projects": existing_projects,
                "old_kg_snapshot": old_kg_snapshot,
                "change_threshold": CHANGE_THRESHOLD,
                "project_versions": project_versions,
            }

            # 3. Invoke the LangGraph pipeline
            self.status = "generating"
            print(f"  [worker] Invoking forge graph (first_run={is_first_run})...")
            result = asyncio.run(forge_graph.ainvoke(initial_state))

            # 4. Read outputs from graph result
            new_kg_snapshot: dict = result.get("new_kg_snapshot", {})
            change_score: float = result.get("change_score", 0.0)
            change_summary: str = result.get("change_summary", "no changes")
            synthesized_projects: list[dict] = result.get("synthesized_projects", [])
            mockup_results: list[dict] = result.get("mockup_results", [])

            print(f"  [worker] Graph complete: score={change_score}, "
                  f"{len(synthesized_projects)} synthesized, {len(mockup_results)} mockups")

            # 5. Update state with new snapshot
            state["kg_snapshot"] = new_kg_snapshot
            state["last_poll_at"] = _now_iso()
            state["poll_count"] = state.get("poll_count", 0) + 1

            # 6. Build poll log entry
            poll_entry = {
                "polled_at": _now_iso(),
                "bonfire_id": bonfire_id,
                "episode_count": new_kg_snapshot.get("episode_count", 0),
                "entity_count": new_kg_snapshot.get("entity_count", 0),
                "edge_count": new_kg_snapshot.get("edge_count", 0),
                "new_episodes": len(
                    set(new_kg_snapshot.get("episode_hashes", []))
                    - set(old_kg_snapshot.get("episode_hashes", []))
                ),
                "new_entities": len(
                    set(new_kg_snapshot.get("entity_uuids", []))
                    - set(old_kg_snapshot.get("entity_uuids", []))
                ),
                "new_edges": len(
                    set(new_kg_snapshot.get("edge_fingerprints", []))
                    - set(old_kg_snapshot.get("edge_fingerprints", []))
                ),
                "change_score": change_score,
                "decision": "skip",
                "reason": change_summary,
            }

            # 7. Merge mockup_results into state file as versioned project entries
            if mockup_results:
                poll_entry["decision"] = "regenerate"
                for mr in mockup_results:
                    proj_id: str = mr["project_id"]
                    project_data: dict = mr["project_data"]
                    mockup_dir: str = mr["mockup_dir"]
                    mockup_files: list[dict] = mr.get("mockup_files", [])

                    existing = _find_project(state, proj_id)
                    if existing:
                        new_ver = existing["current_version"] + 1
                    else:
                        new_ver = 1
                        existing = {
                            "id": proj_id,
                            "current_version": 0,
                            "created_at": _now_iso(),
                            "updated_at": _now_iso(),
                            "versions": [],
                        }
                        state["projects"].append(existing)

                    version_entry = {
                        "version": new_ver,
                        "generated_at": _now_iso(),
                        "trigger": "initial_generation" if is_first_run else "kg_change",
                        "change_summary": "first generation" if is_first_run else change_summary,
                        "kg_snapshot_summary": {
                            "episode_count": new_kg_snapshot.get("episode_count", 0),
                            "entity_count": new_kg_snapshot.get("entity_count", 0),
                            "edge_count": new_kg_snapshot.get("edge_count", 0),
                        },
                        "project_data": project_data,
                        "mockup_dir": mockup_dir,
                        "mockup_files": mockup_files,
                    }
                    existing["versions"].append(version_entry)
                    existing["current_version"] = new_ver
                    existing["updated_at"] = _now_iso()

                    if len(existing["versions"]) > MAX_VERSIONS:
                        existing["versions"] = existing["versions"][-MAX_VERSIONS:]

                state["last_generation_at"] = _now_iso()
                state["generation_count"] = state.get("generation_count", 0) + 1

            # Handle retired projects from synthesized output
            for proj in synthesized_projects:
                if proj.get("status") == "retired":
                    proj_id = _slugify(proj.get("name", "unnamed"))
                    existing = _find_project(state, proj_id)
                    if existing:
                        existing["retired_at"] = _now_iso()

            # 8. Append poll log and save state
            state.setdefault("poll_log", []).append(poll_entry)
            state["poll_log"] = state["poll_log"][-MAX_POLL_LOG:]

            self.save_state(state, bonfire_id)
            self.status = "idle"
            self.last_error = None
            print(f"  [worker] Poll cycle complete. {len(state.get('projects', []))} projects.")

        except Exception as e:
            self.status = "error"
            self.last_error = str(e)
            print(f"  [worker] ERROR: {e}")
            traceback.print_exc()
