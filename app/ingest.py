"""Map LiteLLM SpendLogs / OpenAI-compatible usage into internal usage rows."""
from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Any, Optional

from app.tools import normalize_tool


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


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
        return _utc_now_iso()
    if isinstance(value, (int, float)):
        # LiteLLM sometimes uses unix seconds or ms
        ts = float(value)
        if ts > 1e12:
            ts = ts / 1000.0
        try:
            return datetime.fromtimestamp(ts, tz=timezone.utc).isoformat()
        except (OverflowError, OSError, ValueError):
            return _utc_now_iso()
    if isinstance(value, str):
        raw = value.strip()
        if not raw:
            return _utc_now_iso()
        # numeric string
        try:
            return _parse_ts(float(raw))
        except ValueError:
            pass
        try:
            datetime.fromisoformat(raw.replace("Z", "+00:00"))
            return raw.replace("Z", "+00:00") if raw.endswith("Z") else raw
        except ValueError:
            # LiteLLM UI often uses "YYYY-MM-DD HH:MM:SS"
            for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M:%S"):
                try:
                    dt = datetime.strptime(raw, fmt).replace(tzinfo=timezone.utc)
                    return dt.isoformat()
                except ValueError:
                    continue
            return _utc_now_iso()
    return _utc_now_iso()


def _maybe_json(value: Any) -> Any:
    if isinstance(value, str):
        s = value.strip()
        if not s:
            return None
        if s[0] in "{[":
            try:
                return json.loads(s)
            except json.JSONDecodeError:
                return value
    return value


def _tool_from_tags(tags: Any) -> Optional[str]:
    tags = _maybe_json(tags)
    if isinstance(tags, str):
        return _as_str(tags)
    if not isinstance(tags, list):
        return None
    for t in tags:
        if not isinstance(t, str):
            continue
        if t.lower().startswith("tool:"):
            return _as_str(t.split(":", 1)[1])
        if t.lower().startswith("app:"):
            return _as_str(t.split(":", 1)[1])
    # first non-empty tag as soft signal
    for t in tags:
        if isinstance(t, str) and t.strip():
            return _as_str(t.strip())
    return None


def _tag_value(tags: Any, *prefixes: str) -> Optional[str]:
    tags = _maybe_json(tags)
    if isinstance(tags, str):
        for p in prefixes:
            if tags.lower().startswith(p.lower()):
                return _as_str(tags.split(":", 1)[1] if ":" in tags else tags)
        return None
    if not isinstance(tags, list):
        return None
    for t in tags:
        if not isinstance(t, str):
            continue
        low = t.lower()
        for p in prefixes:
            pl = p.lower()
            if low.startswith(pl):
                return _as_str(t.split(":", 1)[1] if ":" in t else t)
    return None


def _first(*values: Any, max_len: int = 128) -> Optional[str]:
    for v in values:
        s = _as_str(v, max_len=max_len)
        if s:
            return s
    return None


def normalize_usage_row(
    raw: dict[str, Any], *, default_source: Optional[str] = None
) -> Optional[dict[str, Any]]:
    """Normalize a loose dict into insertable usage fields. Returns None if no model."""
    if not isinstance(raw, dict):
        return None

    # Nested OpenAI chat.completion style
    usage = raw.get("usage") if isinstance(raw.get("usage"), dict) else {}
    meta = raw.get("metadata")
    meta = _maybe_json(meta)
    if not isinstance(meta, dict):
        meta = {}

    model = _first(
        raw.get("model"),
        raw.get("model_group"),
        raw.get("model_id"),
        meta.get("model"),
    )
    if not model:
        return None

    prompt = _as_int(
        raw.get("prompt_tokens", usage.get("prompt_tokens", meta.get("prompt_tokens")))
    )
    completion = _as_int(
        raw.get(
            "completion_tokens",
            usage.get("completion_tokens", meta.get("completion_tokens")),
        )
    )
    total = raw.get("total_tokens", usage.get("total_tokens"))
    total_i = _as_int(total) if total is not None else prompt + completion
    if total is None:
        total_i = prompt + completion

    participant = _first(
        raw.get("participant"),
        raw.get("end_user"),
        raw.get("user"),
        raw.get("user_id"),
        meta.get("user"),
        meta.get("user_api_key_user_id"),
        meta.get("end_user"),
        meta.get("participant"),
    )
    team = _first(
        raw.get("team"),
        raw.get("team_id"),
        raw.get("organization_id"),
        meta.get("team"),
        meta.get("user_api_key_team_id"),
        meta.get("department"),
    )
    api_key = _first(
        raw.get("key"),
        raw.get("api_key"),
        meta.get("user_api_key"),
        meta.get("user_api_key_hash"),
        max_len=256,
    )
    tags = raw.get("request_tags", raw.get("tags", meta.get("tags")))
    tool_raw = _first(
        raw.get("tool"),
        meta.get("tool"),
        meta.get("app"),
        _tool_from_tags(tags),
    )
    tool = normalize_tool(tool_raw) if tool_raw else None
    tag = _first(raw.get("tag"), meta.get("tag"))
    if not tag and isinstance(_maybe_json(tags), list):
        parsed = _maybe_json(tags)
        if parsed:
            tag = _as_str(",".join(str(x) for x in parsed[:5]))

    ts = _parse_ts(
        raw.get("timestamp")
        or raw.get("startTime")
        or raw.get("start_time")
        or raw.get("endTime")
        or raw.get("end_time")
        or raw.get("created_at")
        or meta.get("timestamp")
    )

    account_id = _first(
        raw.get("account_id"),
        raw.get("account"),
        meta.get("account_id"),
        meta.get("account"),
        _tag_value(tags, "account:", "account_id:"),
    )
    project_id = _first(
        raw.get("project_id"),
        raw.get("project"),
        meta.get("project_id"),
        meta.get("project"),
        _tag_value(tags, "project:", "project_id:"),
    )

    agent_id = _first(
        raw.get("agent_id"),
        raw.get("bot_id"),
        raw.get("automationId"),
        raw.get("automation_id"),
        raw.get("cloudAgentId"),
        raw.get("cloud_agent_id"),
        meta.get("agent_id"),
        meta.get("bot_id"),
        meta.get("automationId"),
        meta.get("automation_id"),
        meta.get("cloudAgentId"),
        meta.get("cloud_agent_id"),
        # bare "agent" key often holds id; prefer after explicit *_id keys
        raw.get("agent") if not isinstance(raw.get("agent"), dict) else None,
        meta.get("agent") if not isinstance(meta.get("agent"), dict) else None,
        _tag_value(
            tags,
            "agent_id:",
            "agent:",
            "bot_id:",
            "automationId:",
            "automation_id:",
            "cloudAgentId:",
            "cloud_agent_id:",
        ),
    )
    agent_name = _first(
        raw.get("agent_name"),
        raw.get("agentName"),
        meta.get("agent_name"),
        meta.get("agentName"),
        _tag_value(tags, "agent_name:", "agentName:"),
    )
    # If metadata.agent is {"id":..., "name":...}
    agent_obj = raw.get("agent") if isinstance(raw.get("agent"), dict) else None
    if agent_obj is None and isinstance(meta.get("agent"), dict):
        agent_obj = meta.get("agent")
    if isinstance(agent_obj, dict):
        if not agent_id:
            agent_id = _first(
                agent_obj.get("id"),
                agent_obj.get("agent_id"),
                agent_obj.get("cloudAgentId"),
            )
        if not agent_name:
            agent_name = _first(
                agent_obj.get("name"),
                agent_obj.get("agent_name"),
                agent_obj.get("agentName"),
            )

    request_id = _first(
        raw.get("request_id"),
        raw.get("id"),
        raw.get("call_id"),
        meta.get("request_id"),
        max_len=256,
    )

    source = _first(raw.get("source"), meta.get("source"), default_source)

    # Optional billed / reported spend (LiteLLM "spend" is USD)
    cost_usd = None
    cost_basis = None
    spend = raw.get("spend", meta.get("spend"))
    if spend is not None:
        try:
            cost_usd = float(spend)
            cost_basis = "litellm_spend"
        except (TypeError, ValueError):
            pass

    if not participant and api_key:
        participant = api_key

    if not request_id:
        # Stable synthetic key so re-push of same usage does not stack
        request_id = _as_str(
            f"{source or 'usage'}:{account_id or participant or 'anon'}:"
            f"{ts}:{model}:{project_id or ''}:{prompt}:{completion}",
            max_len=256,
        )

    return {
        "model": model,
        "prompt_tokens": prompt,
        "completion_tokens": completion,
        "total_tokens": total_i,
        "team": team,
        "api_key": api_key,
        "tag": tag,
        "participant": participant,
        "tool": tool,
        "account_id": account_id,
        "project_id": project_id,
        "agent_id": agent_id,
        "agent_name": agent_name,
        "request_id": request_id,
        "source": source,
        "cost_usd": cost_usd,
        "cost_basis": cost_basis,
        "ts": ts,
    }


def extract_records(payload: Any) -> list[dict[str, Any]]:
    """Accept single object, list, or {data|logs|items|results: [...]}."""
    if payload is None:
        return []
    if isinstance(payload, list):
        return [x for x in payload if isinstance(x, dict)]
    if not isinstance(payload, dict):
        return []
    for key in ("data", "logs", "items", "results", "spend_logs", "records"):
        if key in payload and isinstance(payload[key], list):
            return [x for x in payload[key] if isinstance(x, dict)]
    # single spend log / openai response
    return [payload]


def map_records(
    payload: Any, *, default_source: Optional[str] = None
) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for raw in extract_records(payload):
        row = normalize_usage_row(raw, default_source=default_source)
        if row:
            out.append(row)
    return out
