"""Background polling worker for Project Forge.

Runs as a daemon thread started by server.py on boot.
Polls the Bonfires KG every POLL_INTERVAL seconds, computes a change score,
and regenerates projects + mockups when changes are significant.
"""

import asyncio
import hashlib
import json
import os
import tempfile
import threading
import time
import traceback
from datetime import datetime, timezone
from pathlib import Path

FORGE_DIR = Path(__file__).parent
STATE_FILE = FORGE_DIR / "forge_state.json"
MOCKUPS_DIR = FORGE_DIR / "mockups"

POLL_INTERVAL = int(os.environ.get("POLL_INTERVAL", 6 * 60 * 60))  # 6 hours
CHANGE_THRESHOLD = float(os.environ.get("CHANGE_THRESHOLD", 0.3))
MAX_VERSIONS = int(os.environ.get("MAX_VERSIONS", 10))
MAX_POLL_LOG = 50

# Weights for change score
W_EPISODE = float(os.environ.get("EPISODE_WEIGHT", 0.5))
W_ENTITY = float(os.environ.get("ENTITY_WEIGHT", 0.3))
W_EDGE = float(os.environ.get("EDGE_WEIGHT", 0.2))


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _slugify(name: str) -> str:
    return name.lower().replace(" ", "-").replace("/", "-").replace("'", "")[:60]


def compute_change_score(old_snapshot: dict, new_snapshot: dict) -> tuple[float, str]:
    """Deterministic diff between two KG snapshots. Returns (score, reason)."""
    old_eps = set(old_snapshot.get("episode_hashes", []))
    new_eps = set(new_snapshot.get("episode_hashes", []))
    old_ents = set(old_snapshot.get("entity_uuids", []))
    new_ents = set(new_snapshot.get("entity_uuids", []))
    old_edges = set(old_snapshot.get("edge_fingerprints", []))
    new_edges = set(new_snapshot.get("edge_fingerprints", []))

    added_eps = new_eps - old_eps
    added_ents = new_ents - old_ents
    added_edges = new_edges - old_edges

    # Weighted scoring — 5 new episodes = max episode component, etc.
    ep_score = min(len(added_eps) / 5.0, 1.0)
    ent_score = min(len(added_ents) / 10.0, 1.0)
    edge_score = min(len(added_edges) / 15.0, 1.0)

    score = (ep_score * W_EPISODE) + (ent_score * W_ENTITY) + (edge_score * W_EDGE)

    reasons = []
    if added_eps:
        reasons.append(f"{len(added_eps)} new episodes")
    if added_ents:
        reasons.append(f"{len(added_ents)} new entities")
    if added_edges:
        reasons.append(f"{len(added_edges)} new edges")

    return round(score, 3), ", ".join(reasons) if reasons else "no changes"


def _build_snapshot(themes_data: dict) -> dict:
    """Build a diffable snapshot from raw themes data."""
    return {
        "polled_at": _now_iso(),
        "episode_count": len(themes_data.get("episodes", [])),
        "entity_count": len(themes_data.get("entities", [])),
        "edge_count": len(themes_data.get("edges", [])),
        "episode_hashes": sorted(set(
            hashlib.md5(ep["name"].encode()).hexdigest()[:12]
            for ep in themes_data.get("episodes", [])
        )),
        "entity_uuids": sorted(set(
            ent.get("uuid", ent["name"])
            for ent in themes_data.get("entities", [])
        )),
        "edge_fingerprints": sorted(set(
            f"{e.get('source_uuid', '')}|{e.get('target_uuid', '')}|{e.get('name', '')}"
            for e in themes_data.get("edges", [])
        )),
    }


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
        self.thread = None
        self.status = "idle"  # idle | polling | generating | error
        self.last_error = None

    def load_state(self) -> dict:
        with self.lock:
            if STATE_FILE.exists():
                try:
                    return json.loads(STATE_FILE.read_text())
                except (json.JSONDecodeError, OSError):
                    return _default_state()
            return _default_state()

    def save_state(self, state: dict):
        with self.lock:
            FORGE_DIR.mkdir(parents=True, exist_ok=True)
            tmp = tempfile.NamedTemporaryFile(
                mode="w", dir=str(FORGE_DIR), suffix=".tmp", delete=False
            )
            try:
                json.dump(state, tmp, indent=2)
                tmp.close()
                os.rename(tmp.name, str(STATE_FILE))
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
        self.running = True
        self.thread = threading.Thread(target=self._poll_loop, daemon=True)
        self.thread.start()
        print("  [worker] Background polling started")

    def stop(self):
        self.running = False

    def trigger_now(self):
        """Force a poll cycle immediately (admin endpoint)."""
        threading.Thread(target=self._do_poll_cycle, daemon=True).start()

    def get_status(self) -> dict:
        state = self.load_state()
        return {
            "status": self.status,
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
        # On first boot, run immediately if no projects exist
        state = self.load_state()
        if not state.get("projects"):
            print("  [worker] First boot — running initial generation")
            self._do_poll_cycle()

        while self.running:
            time.sleep(POLL_INTERVAL)
            if self.running:
                self._do_poll_cycle()

    def _do_poll_cycle(self):
        try:
            self.status = "polling"
            print(f"  [worker] Polling KG at {_now_iso()}")
            state = self.load_state()

            # Step 1: Extract fresh KG data
            from forge import extract_themes
            new_themes_data = extract_themes()
            new_snapshot = _build_snapshot(new_themes_data)

            print(f"  [worker] KG: {new_snapshot['episode_count']} episodes, "
                  f"{new_snapshot['entity_count']} entities, {new_snapshot['edge_count']} edges")

            # Step 2: Compare with existing snapshot
            old_snapshot = state.get("kg_snapshot", {})
            score, reason = compute_change_score(old_snapshot, new_snapshot)

            # Step 3: Record the poll
            poll_entry = {
                "polled_at": _now_iso(),
                "episode_count": new_snapshot["episode_count"],
                "entity_count": new_snapshot["entity_count"],
                "edge_count": new_snapshot["edge_count"],
                "new_episodes": len(set(new_snapshot.get("episode_hashes", [])) - set(old_snapshot.get("episode_hashes", []))),
                "new_entities": len(set(new_snapshot.get("entity_uuids", [])) - set(old_snapshot.get("entity_uuids", []))),
                "new_edges": len(set(new_snapshot.get("edge_fingerprints", [])) - set(old_snapshot.get("edge_fingerprints", []))),
                "change_score": score,
                "decision": "skip",
                "reason": reason,
            }

            # Step 4: Always update snapshot (so next diff is against latest)
            new_snapshot["raw_themes_data"] = new_themes_data
            state["kg_snapshot"] = new_snapshot
            state["last_poll_at"] = _now_iso()
            state["poll_count"] = state.get("poll_count", 0) + 1

            # Step 5: Decide whether to regenerate
            is_first_run = len(state.get("projects", [])) == 0
            if is_first_run or score >= CHANGE_THRESHOLD:
                poll_entry["decision"] = "regenerate"
                print(f"  [worker] Change score {score} >= {CHANGE_THRESHOLD} — regenerating")
                self.status = "generating"
                self._regenerate(state, new_themes_data, reason, is_first_run)
                state["last_generation_at"] = _now_iso()
                state["generation_count"] = state.get("generation_count", 0) + 1
            else:
                print(f"  [worker] Change score {score} < {CHANGE_THRESHOLD} — skipping")

            # Step 6: Append poll log (capped)
            state.setdefault("poll_log", []).append(poll_entry)
            state["poll_log"] = state["poll_log"][-MAX_POLL_LOG:]

            self.save_state(state)
            self.status = "idle"
            self.last_error = None
            print(f"  [worker] Poll cycle complete. {len(state.get('projects', []))} projects.")

        except Exception as e:
            self.status = "error"
            self.last_error = str(e)
            print(f"  [worker] ERROR: {e}")
            traceback.print_exc()

    def _regenerate(self, state: dict, themes_data: dict, change_summary: str, is_first_run: bool):
        """Run Claude synthesis + mockup generation."""
        from forge import synthesize_projects, synthesize_projects_with_existing, generate_multi_mockup

        # Gather existing project data for Claude context
        existing_projects = []
        for p in state.get("projects", []):
            if p.get("versions"):
                existing_projects.append(p["versions"][-1]["project_data"])

        # Call Claude
        if is_first_run or not existing_projects:
            print("  [worker] Generating initial project batch...")
            result = asyncio.run(synthesize_projects(themes_data))
            # On first run, all projects are "new"
            for proj in result.get("projects", []):
                proj["status"] = "new"
        else:
            print(f"  [worker] Updating with context of {len(existing_projects)} existing projects...")
            result = asyncio.run(
                synthesize_projects_with_existing(themes_data, existing_projects, change_summary)
            )

        # Process each project result
        for proj_result in result.get("projects", []):
            status = proj_result.get("status", "new")
            proj_id = _slugify(proj_result.get("name", "unnamed"))

            if status == "unchanged":
                continue

            if status == "retired":
                existing = _find_project(state, proj_id)
                if existing:
                    existing["retired_at"] = _now_iso()
                continue

            # "new" or "updated" — generate mockup and store version
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

            # Clean project data (remove the status field)
            project_data = {k: v for k, v in proj_result.items() if k != "status"}

            # Generate multi-file mockup
            mockup_rel_dir = f"mockups/{proj_id}/v{new_ver}"
            mockup_abs_dir = str(FORGE_DIR / mockup_rel_dir)
            print(f"  [worker] Generating mockup for '{proj_result.get('name', '?')}' v{new_ver}...")

            try:
                mockup_result = asyncio.run(generate_multi_mockup(project_data, mockup_abs_dir))
                mockup_files = mockup_result.get("files", [])
            except Exception as e:
                print(f"  [worker] Mockup generation failed: {e}")
                mockup_files = []

            # Store version
            version_entry = {
                "version": new_ver,
                "generated_at": _now_iso(),
                "trigger": "initial_generation" if is_first_run else "kg_change",
                "change_summary": change_summary if not is_first_run else "first generation",
                "kg_snapshot_summary": {
                    "episode_count": state["kg_snapshot"].get("episode_count", 0),
                    "entity_count": state["kg_snapshot"].get("entity_count", 0),
                    "edge_count": state["kg_snapshot"].get("edge_count", 0),
                },
                "project_data": project_data,
                "mockup_dir": mockup_rel_dir,
                "mockup_files": mockup_files,
            }
            existing["versions"].append(version_entry)
            existing["current_version"] = new_ver
            existing["updated_at"] = _now_iso()

            # Prune old versions if needed
            if len(existing["versions"]) > MAX_VERSIONS:
                existing["versions"] = existing["versions"][-MAX_VERSIONS:]
