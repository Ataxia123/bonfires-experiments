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

ENV_CANDIDATES = [
    Path(__file__).parent.parent / ".env",
    Path(__file__).resolve().parents[4] / ".env",
]
for _env_path in ENV_CANDIDATES:
    if _env_path.exists():
        load_dotenv(_env_path)
        break

MONGO_URI = os.environ["MONGO_URI"]
DB_NAME = "bonfires_dan"

client = pymongo.MongoClient(MONGO_URI)
db = client[DB_NAME]

app = FastAPI(title="Eval Dashboard")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["GET"],
    allow_headers=["*"],
)


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
    docs = list(
        db["agentdocuments"].find({"metadata.isActive": True}).sort("updatedAt", pymongo.DESCENDING)
    )
    return JSONResponse([_serialize(d) for d in docs])
