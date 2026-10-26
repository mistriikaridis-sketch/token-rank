"""ProCode / Canoryn placeholder adapter.

Public usage schema is insufficiently documented. Accept unified usage events
(tool forced to procode) so teams can tag traffic via LiteLLM/gateway headers
and still land on the leaderboard.
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Optional

from app.adapters.parse_stats import ParseStats
from app.tools import normalize_tool

_FORBIDDEN = frozenset(
    {
        "prompt",
        "content",
        "message",
        "messages",
        "transcript",
        "response",
        "completion",
        "chat",
        "body",
    }
)
_CONTENT_REJECT_MIN_LEN = 24


def _as_int(value: Any, default: int = 0) -> int:
    if value is None:
        return default
    try:
        return max(int(value), 0)
    except (TypeError, ValueError):
        return default


def _as_str(value: Any, max_len: int = 128) -> Optional[str]:
    if value is None:
        return None
    s = str(value).strip()
    if not s:
        return None
    return s[:max_len]


def _parse_ts(value: Any) -> str:
    if value is None or value == "":
        return datetime.now(timezone.utc).isoformat()
    if isinstance(value, (int, float)):
        ts = float(value)
        if ts > 1e12:
            ts = ts / 1000.0
        try:
            return datetime.fromtimestamp(ts, tz=timezone.utc).isoformat()
        except (OverflowError, OSError, ValueError):
            return datetime.now(timezone.utc).isoformat()
    if isinstance(value, str):
        raw = value.strip()
        if not raw:
            return datetime.now(timezone.utc).isoformat()
        if raw.isdigit():
            return _parse_ts(int(raw))
        try:
            datetime.fromisoformat(raw.replace("Z", "+00:00"))
            return raw.replace("Z", "+00:00") if raw.endswith("Z") else raw
        except ValueError:
            return datetime.now(timezone.utc).isoformat()
    return datetime.now(timezone.utc).isoformat()


def _has_heavy_forbidden(raw: dict[str, Any]) -> bool:
    for k, v in raw.items():
        if str(k).lower() not in _FORBIDDEN:
            continue
        if isinstance(v, str) and len(v.strip()) >= _CONTENT_REJECT_MIN_LEN:
            return True
        if isinstance(v, (list, dict)) and v:
            return True
    return False


def extract_procode_payload(payload: Any) -> list[dict[str, Any]]:
    if payload is None:
        return []
    if isinstance(payload, list):
        return [x for x in payload if isinstance(x, dict)]
    if isinstance(payload, dict):
        for key in ("events", "data", "items", "records", "usage"):
            if key in payload and isinstance(payload[key], list):
                return [x for x in payload[key] if isinstance(x, dict)]
        return [payload]
    return []


def map_procode_event(raw: dict[str, Any]) -> Optional[dict[str, Any]]:
    if not isinstance(raw, dict):
        return None
    if _has_heavy_forbidden(raw):
        return None

    usage = raw.get("usage") if isinstance(raw.get("usage"), dict) else {}
    model = _as_str(
        raw.get("model") or raw.get("model_name") or raw.get("modelName") or usage.get("model")
    )
    if not model:
        return None

    prompt = _as_int(
        raw.get(
            "prompt_tokens",
            raw.get("input_tokens", usage.get("prompt_tokens", usage.get("input_tokens", usage.get("input")))),
        )
    )
    completion = _as_int(
        raw.get(
            "completion_tokens",
            raw.get(
                "output_tokens",
                usage.get("completion_tokens", usage.get("output_tokens", usage.get("output"))),
            ),
        )
    )
    cache_read = _as_int(raw.get("cache_read_tokens", usage.get("cache_read_tokens", usage.get("cache_read"))))
    cache_write = _as_int(
        raw.get("cache_write_tokens", usage.get("cache_write_tokens", usage.get("cache_write")))
    )
    reasoning = _as_int(raw.get("reasoning_tokens", usage.get("reasoning_tokens", usage.get("reasoning"))))
    total = _as_int(raw.get("total_tokens", usage.get("total_tokens")))
    if total == 0:
        total = prompt + completion + cache_read + cache_write + reasoning
    if prompt == 0 and completion == 0 and total == 0:
        return None

    account_id = _as_str(
        raw.get("account_id")
        or raw.get("account")
        or raw.get("email")
        or raw.get("user")
        or raw.get("userEmail")
    )
    participant = _as_str(raw.get("participant") or account_id or raw.get("email") or "anonymous")
    project_id = _as_str(raw.get("project_id") or raw.get("project") or raw.get("projectId"))
    team = _as_str(raw.get("team") or raw.get("team_id"))
    agent_id = _as_str(
        raw.get("agent_id") or raw.get("bot_id") or raw.get("automationId") or raw.get("cloudAgentId")
    )
    agent_name = _as_str(raw.get("agent_name") or raw.get("agentName"))
    ts = _parse_ts(raw.get("timestamp") or raw.get("ts") or raw.get("created_at") or raw.get("createdAt"))
    request_id = _as_str(
        raw.get("request_id") or raw.get("requestId") or raw.get("id") or raw.get("event_id"),
        max_len=256,
    )
    if not request_id:
        request_id = _as_str(
            f"procode:{account_id or participant}:{ts}:{model}:{project_id or ''}",
            max_len=256,
        )

    cost_usd = None
    cost_basis = None
    if raw.get("cost_usd") is not None:
        try:
            cost_usd = float(raw["cost_usd"])
            cost_basis = _as_str(raw.get("cost_basis")) or "procode_reported"
        except (TypeError, ValueError):
            pass

    # Drop any leftover forbidden keys — never forward metadata blobs with content
    return {
        "model": model,
        "prompt_tokens": prompt,
        "completion_tokens": completion,
        "total_tokens": total,
        "cache_read_tokens": cache_read,
        "cache_write_tokens": cache_write,
        "reasoning_tokens": reasoning,
        "team": team,
        "api_key": None,
        "tag": _as_str(raw.get("tag")),
        "participant": participant,
        "tool": normalize_tool("procode"),
        "account_id": account_id,
        "project_id": project_id,
        "agent_id": agent_id,
        "agent_name": agent_name,
        "request_id": request_id,
        "source": "procode",
        "cost_usd": cost_usd,
        "cost_basis": cost_basis,
        "ts": ts,
    }


def map_procode_events(events: list[dict[str, Any]]) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for raw in events:
        row = map_procode_event(raw)
        if row:
            out.append(row)
    return out


def map_procode_events_with_stats(
    events: list[dict[str, Any]],
) -> tuple[list[dict[str, Any]], ParseStats]:
    stats = ParseStats()
    out: list[dict[str, Any]] = []
    for raw in events:
        stats.lines_total += 1
        if not isinstance(raw, dict):
            stats.skipped_other += 1
            continue
        if _has_heavy_forbidden(raw):
            stats.skipped_forbidden_content += 1
            continue
        row = map_procode_event(raw)
        if row is None:
            model = _as_str(raw.get("model") or raw.get("model_name"))
            if not model:
                stats.skipped_missing_model += 1
            else:
                stats.skipped_missing_tokens += 1
            continue
        stats.parsed_ok += 1
        out.append(row)
    return out, stats


def map_procode_payload(payload: Any) -> list[dict[str, Any]]:
    return map_procode_events(extract_procode_payload(payload))


ADAPTER_META = {
    "id": "procode",
    "display": "ProCode",
    "status": "placeholder",
    "aliases": ["procode", "canoryn", "pro code"],
    "notes": (
        "公开 schema 不足；请经 LiteLLM/网关打 header（如 x-tool=procode / tags tool:procode）"
        "后用统一 usage schema 推送到 POST /api/v1/ingest/procode。"
    ),
}
