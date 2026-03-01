"""Low-level HTTP request helpers for Delve API calls."""

from __future__ import annotations

import json
import urllib.error
import urllib.request

import game_config as config


def _json_request(method: str, url: str, body: dict[str, object] | None = None) -> tuple[int, dict[str, object]]:
    payload = None
    headers: dict[str, str] = {"Content-Type": "application/json"}
    if config.DELVE_API_KEY:
        headers["Authorization"] = f"Bearer {config.DELVE_API_KEY}"
    if body is not None:
        payload = json.dumps(body).encode("utf-8")
    request = urllib.request.Request(url=url, data=payload, headers=headers, method=method)
    try:
        with urllib.request.urlopen(request, timeout=20) as response:
            raw = response.read().decode("utf-8")
            decoded = json.loads(raw) if raw else {}
            if isinstance(decoded, dict):
                return response.status, decoded
            return response.status, {"data": decoded}
    except urllib.error.HTTPError as exc:
        raw = exc.read().decode("utf-8", errors="replace")
        try:
            decoded = json.loads(raw)
        except json.JSONDecodeError:
            decoded = {"error": raw}
        if isinstance(decoded, dict):
            return exc.code, {str(k): v for k, v in decoded.items()}
        return exc.code, {"error": decoded}
    except urllib.error.URLError as exc:
        return 503, {"error": f"Backend request failed: {exc}"}


def _agent_json_request(
    method: str,
    url: str,
    api_key: str,
    body: dict[str, object] | None = None,
) -> tuple[int, dict[str, object]]:
    if not api_key.strip():
        return 503, {"error": "Agent API key is not configured"}
    payload = None
    headers: dict[str, str] = {
        "Content-Type": "application/json",
        "Authorization": f"Bearer {api_key}",
    }
    if body is not None:
        payload = json.dumps(body).encode("utf-8")
    request = urllib.request.Request(url=url, data=payload, headers=headers, method=method)
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            raw = response.read().decode("utf-8")
            decoded = json.loads(raw) if raw else {}
            if isinstance(decoded, dict):
                return response.status, decoded
            return response.status, {"data": decoded}
    except urllib.error.HTTPError as exc:
        raw = exc.read().decode("utf-8", errors="replace")
        try:
            decoded = json.loads(raw)
        except json.JSONDecodeError:
            decoded = {"error": raw}
        if isinstance(decoded, dict):
            return exc.code, {str(k): v for k, v in decoded.items()}
        return exc.code, {"error": decoded}
    except urllib.error.URLError as exc:
        return 503, {"error": f"Backend request failed: {exc}"}
