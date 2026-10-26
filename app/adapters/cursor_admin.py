"""Cursor Teams Admin API adapter — filtered-usage-events → unified usage rows.

Docs: POST https://api.cursor.com/teams/filtered-usage-events
Auth: Basic (API key as username, empty password). Env: CURSOR_ADMIN_API_KEY.

Also accepts offline export JSON (fixtures / local ~/.cursor/exports).
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Optional

from app.adapters.parse_stats import ParseStats
from app.tools import normalize_tool

_FORBIDDEN_TOP = frozenset(
    {
        "prompt",
        "content",
        "message",
        "messages",
        "transcript",
        "completion",
        "response",
        "chat",
        "conversation_text",
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
            return _parse_ts(float(raw))
        except ValueError:
            pass
        try:
            datetime.fromisoformat(raw.replace("Z", "+00:00"))
            return raw.replace("Z", "+00:00") if raw.endswith("Z") else raw
        except ValueError:
            return datetime.now(timezone.utc).isoformat()
    return datetime.now(timezone.utc).isoformat()


def _project_from_conversation(conversation_id: Optional[str]) -> Optional[str]:
    if not conversation_id:
        return None
    cid = conversation_id.strip()
    if not cid:
        return None
    if "-" in cid:
        return f"conv:{cid.split('-', 1)[0][:12]}"
    return f"conv:{cid[:12]}"


def _has_heavy_forbidden(raw: dict[str, Any]) -> bool:
    for k, v in raw.items():
        if str(k).lower() not in _FORBIDDEN_TOP:
            continue
        if isinstance(v, str) and len(v.strip()) >= _CONTENT_REJECT_MIN_LEN:
            return True
        if isinstance(v, (list, dict)) and v:
            return True
    return False


def _token_usage(raw: dict[str, Any]) -> dict[str, Any]:
    for key in ("tokenUsage", "token_usage", "usage"):
        u = raw.get(key)
        if isinstance(u, dict):
            return u
    return {}


def _classify_skip(raw: Any) -> str:
    if not isinstance(raw, dict):
        return "other"
    if _has_heavy_forbidden(raw):
        return "forbidden_content"
    model = _as_str(raw.get("model") or raw.get("modelName") or raw.get("model_name"))
    if not model:
        return "missing_model"
    usage = _token_usage(raw)
    prompt = _as_int(
        usage.get("inputTokens")
        or usage.get("input_tokens")
        or usage.get("prompt_tokens")
        or raw.get("inputTokens")
        or raw.get("prompt_tokens")
    )
    completion = _as_int(
        usage.get("outputTokens")
        or usage.get("output_tokens")
        or usage.get("completion_tokens")
        or raw.get("outputTokens")
        or raw.get("completion_tokens")
    )
    charged = raw.get("chargedCents")
    if charged is None:
        charged = raw.get("charged_cents")
    if prompt == 0 and completion == 0 and not raw.get("kind") and charged is None:
        return "missing_tokens"
    return "ok"


def map_cursor_event(raw: dict[str, Any]) -> Optional[dict[str, Any]]:
    """Map one Cursor usageEvents[] item to an insertable usage row."""
    if _classify_skip(raw) != "ok":
        return None
    assert isinstance(raw, dict)

    model = _as_str(raw.get("model") or raw.get("modelName") or raw.get("model_name"))
    assert model
    usage = _token_usage(raw)

    def _u(*keys: str) -> Any:
        for k in keys:
            if k in usage and usage[k] is not None:
                return usage[k]
            if k in raw and raw[k] is not None:
                return raw[k]
            for uk, uv in usage.items():
                if str(uk).lower() == k.lower() and uv is not None:
                    return uv
        return None

    prompt = _as_int(_u("inputTokens", "input_tokens", "prompt_tokens", "input", "promptTokens"))
    completion = _as_int(
        _u("outputTokens", "output_tokens", "completion_tokens", "output", "outputTokens")
    )
    cache_read = _as_int(_u("cacheReadTokens", "cache_read_tokens", "cache_read", "cacheRead"))
    cache_write = _as_int(_u("cacheWriteTokens", "cache_write_tokens", "cache_write", "cacheWrite"))
    reasoning = _as_int(_u("reasoningTokens", "reasoning_tokens", "reasoning"))
    total = prompt + completion + cache_read + cache_write + reasoning

    email = _as_str(
        raw.get("userEmail")
        or raw.get("email")
        or raw.get("owningUser")
        or raw.get("owning_user")
        or raw.get("user_email"),
        max_len=128,
    )
    user_id = _as_str(
        raw.get("userId")
        or raw.get("user_id")
        or raw.get("serviceAccountId")
        or raw.get("service_account_id"),
        max_len=128,
    )
    account_id = email or user_id
    participant = email or user_id or "anonymous"

    conversation_id = _as_str(
        raw.get("conversationId")
        or raw.get("conversation_id")
        or raw.get("composerId")
        or raw.get("composer_id"),
        max_len=128,
    )
    project_id = _project_from_conversation(conversation_id)
    if raw.get("project_id") or raw.get("projectId"):
        project_id = _as_str(raw.get("project_id") or raw.get("projectId"))

    team = _as_str(raw.get("team") or raw.get("team_id") or raw.get("teamId"))

    agent_obj = raw.get("agent") if isinstance(raw.get("agent"), dict) else None
    agent_id = _as_str(
        raw.get("agent_id")
        or raw.get("cloudAgentId")
        or raw.get("cloud_agent_id")
        or raw.get("bot_id")
        or raw.get("automationId")
        or raw.get("automation_id")
        or (agent_obj.get("id") if agent_obj else None)
        or (raw.get("agent") if isinstance(raw.get("agent"), str) else None)
    )
    agent_name = _as_str(
        raw.get("agent_name")
        or raw.get("agentName")
        or (agent_obj.get("name") if agent_obj else None)
    )

    tag_parts = []
    if cache_read:
        tag_parts.append(f"cache_read:{cache_read}")
    if cache_write:
        tag_parts.append(f"cache_write:{cache_write}")
    if reasoning:
        tag_parts.append(f"reasoning:{reasoning}")
    if raw.get("kind"):
        tag_parts.append(f"kind:{raw['kind']}")
    if conversation_id:
        tag_parts.append(f"conversation:{conversation_id[:36]}")
    if agent_id:
        tag_parts.append(f"agent:{agent_id[:36]}")
    tag = ",".join(tag_parts)[:128] if tag_parts else None

    ts = _parse_ts(
        raw.get("timestamp")
        or raw.get("timestampMs")
        or raw.get("timestamp_ms")
        or raw.get("ts")
        or raw.get("createdAt")
    )

    request_id = _as_str(
        raw.get("request_id")
        or raw.get("requestId")
        or raw.get("id")
        or raw.get("eventId")
        or raw.get("event_id"),
        max_len=256,
    )
    if not request_id:
        request_id = _as_str(
            f"cursor:{account_id or 'anon'}:{raw.get('timestamp') or raw.get('timestampMs') or ts}:"
            f"{model}:{conversation_id or ''}",
            max_len=256,
        )

    cost_usd = None
    cost_basis = None
    charged = raw.get("chargedCents")
    if charged is None:
        charged = raw.get("charged_cents")
    if charged is None and isinstance(usage, dict):
        charged = usage.get("totalCents") or usage.get("total_cents")
    if charged is not None:
        try:
            cost_usd = float(charged) / 100.0
            cost_basis = "cursor_charged"
        except (TypeError, ValueError):
            pass

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
        "tag": tag,
        "participant": participant,
        "tool": normalize_tool("cursor"),
        "account_id": account_id,
        "project_id": project_id,
        "agent_id": agent_id,
        "agent_name": agent_name,
        "request_id": request_id,
        "source": "cursor",
        "cost_usd": cost_usd,
        "cost_basis": cost_basis,
        "ts": ts,
    }


def extract_cursor_events(payload: Any) -> list[dict[str, Any]]:
    if payload is None:
        return []
    if isinstance(payload, list):
        return [x for x in payload if isinstance(x, dict)]
    if not isinstance(payload, dict):
        return []
    for key in ("usageEvents", "usage_events", "events", "data", "items"):
        if key in payload and isinstance(payload[key], list):
            return [x for x in payload[key] if isinstance(x, dict)]
    return [payload]


def map_cursor_events(events: list[dict[str, Any]]) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for raw in events:
        row = map_cursor_event(raw)
        if row:
            out.append(row)
    return out


def map_cursor_events_with_stats(
    events: list[dict[str, Any]],
) -> tuple[list[dict[str, Any]], ParseStats]:
    stats = ParseStats()
    out: list[dict[str, Any]] = []
    for raw in events:
        stats.lines_total += 1
        reason = _classify_skip(raw)
        if reason != "ok":
            if reason == "forbidden_content":
                stats.skipped_forbidden_content += 1
            elif reason == "missing_model":
                stats.skipped_missing_model += 1
            elif reason == "missing_tokens":
                stats.skipped_missing_tokens += 1
            else:
                stats.skipped_other += 1
            continue
        row = map_cursor_event(raw)
        if row:
            stats.parsed_ok += 1
            out.append(row)
        else:
            stats.skipped_other += 1
    return out, stats


def map_cursor_payload(payload: Any) -> list[dict[str, Any]]:
    return map_cursor_events(extract_cursor_events(payload))


def map_cursor_payload_with_stats(payload: Any) -> tuple[list[dict[str, Any]], ParseStats]:
    return map_cursor_events_with_stats(extract_cursor_events(payload))
