"""Eval dashboard API — Vercel serverless function + local dev.

Exports a FastAPI `app` that Vercel's @vercel/python builder picks up.
Locally, server.py imports this and runs it with uvicorn.
"""

import os
from pathlib import Path
from typing import Any

import pymongo
from dotenv import load_dotenv
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

_resolved = Path(__file__).resolve()
_env_candidates = [_resolved.parent.parent / ".env"]
if len(_resolved.parents) > 4:
    _env_candidates.append(_resolved.parents[4] / ".env")
for _env_path in _env_candidates:
    if _env_path.exists():
        load_dotenv(_env_path)
        break

DB_NAME = "bonfires_staging"

app = FastAPI(title="Eval Dashboard")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["GET"],
    allow_headers=["*"],
)

_client: pymongo.MongoClient[dict[str, Any]] | None = None


def _get_db() -> pymongo.database.Database[dict[str, Any]]:
    global _client
    if _client is None:
        _client = pymongo.MongoClient(os.environ["MONGO_URI"])
    return _client[DB_NAME]


def _serialize(doc: dict[str, Any]) -> dict[str, Any]:
    """Convert MongoDB doc to JSON-safe dict."""
    doc["_id"] = str(doc["_id"])
    for key, val in doc.items():
        if hasattr(val, "isoformat"):
            doc[key] = val.isoformat()
    return doc


@app.get("/api/reviews")
def get_reviews() -> JSONResponse:
    """Return deduplicated reviews — latest per repoUrl."""
    db = _get_db()
    docs = list(db["reviewtrackers"].find().sort("updatedAt", pymongo.DESCENDING))

    latest_by_repo: dict[str, dict[str, Any]] = {}
    for doc in docs:
        repo = doc.get("repoUrl", "unknown")
        if repo not in latest_by_repo:
            latest_by_repo[repo] = _serialize(doc)

    return JSONResponse(list(latest_by_repo.values()))


@app.get("/api/rubrics")
def get_rubrics() -> JSONResponse:
    """Return all active agentdocuments (judging rubrics)."""
    db = _get_db()
    docs = list(
        db["agentdocuments"].find({"metadata.isActive": True}).sort("updatedAt", pymongo.DESCENDING)
    )
    return JSONResponse([_serialize(d) for d in docs])
