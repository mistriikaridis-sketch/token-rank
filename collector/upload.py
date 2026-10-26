"""POST usage-only batches to the existing ingest endpoints."""
from __future__ import annotations

import json
import urllib.error
import urllib.request
from typing import Any, Callable, Optional

from collector.config import CollectorConfig

INGEST_PATH = {
    "workbuddy": "/api/v1/ingest/workbuddy",
    "claude_code": "/api/v1/ingest/claude_code",
    "codex": "/api/v1/ingest/codex",
    "continue": "/api/v1/ingest/continue",
    "cursor": "/api/v1/ingest/cursor",
    "procode": "/api/v1/ingest/procode",
}

CHUNK = 200

Transport = Callable[[str, dict[str, Any], CollectorConfig], dict[str, Any]]


class UploadError(RuntimeError):
    def __init__(self, message: str, *, status: Optional[int] = None, body: str = ""):
        super().__init__(message)
        self.status = status
        self.body = body


def _auth_headers(cfg: CollectorConfig) -> dict[str, str]:
    key = (cfg.api_key or "").strip()
    if not key:
        raise UploadError(
            "缺少 TOKEN_RANKINGS_API_KEY。这是排行榜的写入密钥，不是网页上贴的用户密钥。"
        )
    return {
        "Authorization": f"Bearer {key}",
        "X-API-Key": key,
        "Content-Type": "application/json",
        "Accept": "application/json",
    }


def urllib_transport(path: str, body: dict[str, Any], cfg: CollectorConfig) -> dict[str, Any]:
    url = cfg.rankings_url.rstrip("/") + path
    data = json.dumps(body, ensure_ascii=False).encode("utf-8")
    req = urllib.request.Request(url, data=data, headers=_auth_headers(cfg), method="POST")
    try:
        with urllib.request.urlopen(req, timeout=120) as resp:
            raw = resp.read().decode("utf-8")
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")
        raise UploadError(
            f"POST {path} 失败：HTTP {exc.code}",
            status=exc.code,
            body=detail[:500],
        ) from exc
    except urllib.error.URLError as exc:
        raise UploadError(f"连不上 {url}：{exc.reason}") from exc
    try:
        parsed = json.loads(raw) if raw else {}
    except json.JSONDecodeError as exc:
        raise UploadError(f"POST {path} 返回的不是 JSON") from exc
    if not isinstance(parsed, dict):
        raise UploadError(f"POST {path} 返回了意外的 JSON")
    return parsed


def _chunks(events: list[dict[str, Any]]) -> list[list[dict[str, Any]]]:
    if not events:
        return []
    return [events[i : i + CHUNK] for i in range(0, len(events), CHUNK)]


def upload_payloads(
    payloads: dict[str, list[dict[str, Any]]],
    cfg: CollectorConfig,
    *,
    transport: Optional[Transport] = None,
) -> dict[str, Any]:
    """Upload each tool to its ingest route. Empty tools are skipped."""
    send = transport or urllib_transport
    results: dict[str, Any] = {}
    for tool, events in payloads.items():
        path = INGEST_PATH.get(tool)
        if path is None:
            continue
        if not events:
            results[tool] = {"accepted": 0, "skipped": 0, "batches": 0, "empty": True}
            continue
        accepted = 0
        skipped = 0
        ids: list[Any] = []
        for batch in _chunks(events):
            result = send(path, {"events": batch}, cfg)
            accepted += int(result.get("accepted") or 0)
            skipped += int(result.get("skipped") or 0)
            got = result.get("ids")
            if isinstance(got, list):
                ids.extend(got)
        results[tool] = {
            "accepted": accepted,
            "skipped": skipped,
            "batches": len(_chunks(events)),
            "ids": ids,
            "source": tool,
        }
    return results
