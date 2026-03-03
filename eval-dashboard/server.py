#!/usr/bin/env python3
"""Local dev server — mounts the API app and serves index.html.

On Vercel, api/index.py is served as a serverless function and
index.html is served as a static asset. This file is only for local dev.
"""

import os
from pathlib import Path

import uvicorn
from fastapi.responses import FileResponse

from api.index import app

STATIC_DIR = Path(__file__).parent
PORT = int(os.environ.get("EVAL_DASHBOARD_PORT", "9990"))


@app.get("/")
def serve_index() -> FileResponse:
    return FileResponse(STATIC_DIR / "index.html", media_type="text/html")


if __name__ == "__main__":
    print(f"\n  Eval Dashboard running on http://localhost:{PORT}")
    print(f"  GET /api/reviews  — deduplicated project reviews")
    print(f"  GET /api/rubrics  — active judging rubrics\n")
    uvicorn.run(app, host="0.0.0.0", port=PORT)
