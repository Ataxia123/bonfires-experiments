"""Game-local configuration constants and environment variables."""

from __future__ import annotations

import os
from pathlib import Path

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
ROOM_HTN_TEMPLATE_ID = os.environ.get("ROOM_HTN_TEMPLATE_ID", "").strip()

ROOM_HTN_TEMPLATE_BODY: dict[str, object] = {
    "name": "Bonfire Quest Room Narrative",
    "template_type": "card",
    "system_prompt": (
        "You are a narrator in a dark fantasy RPG adventure game. "
        "Write evocative, atmospheric descriptions of locations based on what happened there. "
        "Generate rich visual imagery suitable for an image generation prompt. "
        "Keep descriptions concise, visceral, and full of sensory detail."
    ),
    "user_prompt_template": (
        "Location: {dataroom_description}\n\n"
        "World context: {dataroom_system_prompt}\n\n"
        "Recent events in this location:\n{formatted_context}\n\n"
        "Query: {user_query}\n\n"
        "Generate a {blog_length} atmospheric narrative for this location. "
        "Include an image_prompt field capturing the visual mood."
    ),
    "node_count_config": {
        "short": {"max_nodes": 1, "max_words": 120, "description": "Compact room snapshot"},
        "medium": {"max_nodes": 2, "max_words": 250, "description": "Room narrative"},
        "long": {"max_nodes": 3, "max_words": 400, "description": "Full room chronicle"},
    },
}
