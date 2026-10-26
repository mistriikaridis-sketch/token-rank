"""WorkBuddy local jsonl adapter — usage-only fields, never store chat content.

Scans WORKBUDDY_PROJECTS_DIR (default ~/.workbuddy/projects) for *.jsonl.
Only parses whitelisted usage keys; message/content/prompt/response are dropped.
Large forbidden text fields cause the line to be skipped (counted), not partially ingested.
"""
from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

from app.adapters.parse_stats import ParseStats
from app.tools import normalize_tool

# Never persist these (and any nested text bodies)
_FORBIDDEN_KEYS = frozenset(
    {
        "message",
        "messages",
        "content",
        "prompt",
        "response",
        "text",
        "completion",
        "delta",
        "choices",
        "system",
        "user_message",
        "assistant_message",
        "transcript",
        "body",
        "raw_prompt",
        "raw_response",
        "chat",
        "conversation",
        "conversations",
        "tool_calls",
        "tool_results",
        "arguments",
        "input_text",
        "output_text",
    }
)

# Keys that may hold numeric token counts even if named like "prompt"/"output"
_NUMERIC_USAGE_NESTED = frozenset(
    {
        "input",
        "output",
        "prompt",
        "completion",
        "cache_read",
        "cache_write",
        "reasoning",
        "total",
        "prompt_tokens",
        "completion_tokens",
        "input_tokens",
        "output_tokens",
        "cache_read_tokens",
        "cache_write_tokens",
        "reasoning_tokens",
        "total_tokens",
    }
)

_USAGE_KEYS = frozenset(
    {
        "type",
        "event",
        "model",
        "model_id",
        "model_name",
        "modelId",
        "modelName",
        "prompt_tokens",
        "completion_tokens",
        "total_tokens",
        "input_tokens",
        "output_tokens",
        "cache_read_tokens",
        "cache_write_tokens",
        "cache_read",
        "cache_write",
        "reasoning_tokens",
        "reasoning",
        "usage",
        "token_usage",
        "tokenUsage",
        "cwd",
        "project",
        "project_id",
        "projectId",
        "project_path",
        "projectPath",
        "account",
        "account_id",
        "accountId",
        "user",
        "user_id",
        "userId",
        "userEmail",
        "user_email",
        "email",
        "owningUser",
        "participant",
        "team",
        "team_id",
        "teamId",
        "timestamp",
        "timestampMs",
        "timestamp_ms",
        "ts",
        "created_at",
        "createdAt",
        "time",
        "request_id",
        "requestId",
        "id",
        "call_id",
        "callId",
        "event_id",
        "eventId",
        "agent_id",
        "agent",
        "agent_name",
        "agentName",
        "bot_id",
        "botId",
        "automationId",
        "automation_id",
        "cloudAgentId",
        "cloud_agent_id",
    }
)

# If a forbidden key holds a long string, reject the whole event (do not strip-and-keep).
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
            for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M:%S"):
                try:
                    dt = datetime.strptime(raw, fmt).replace(tzinfo=timezone.utc)
                    return dt.isoformat()
                except ValueError:
                    continue
            return datetime.now(timezone.utc).isoformat()
    return datetime.now(timezone.utc).isoformat()


def _has_heavy_forbidden_content(raw: dict[str, Any]) -> bool:
    """True if event carries large chat/prompt bodies that must not be ingested."""
    for k, v in raw.items():
        low = str(k).lower()
        if low not in _FORBIDDEN_KEYS and not low.endswith(("_prompt", "_content", "_message", "_transcript")):
            continue
        if isinstance(v, str) and len(v.strip()) >= _CONTENT_REJECT_MIN_LEN:
            return True
        if isinstance(v, list) and v:
            return True
        if isinstance(v, dict) and v:
            # nested message objects
            return True
    # nested usage must stay numeric; if "prompt" is a long string under usage, reject
    for ukey in ("usage", "token_usage", "tokenUsage"):
        u = raw.get(ukey)
        if isinstance(u, dict):
            for nk, nv in u.items():
                nl = str(nk).lower()
                if nl in _FORBIDDEN_KEYS and isinstance(nv, str) and len(nv.strip()) >= _CONTENT_REJECT_MIN_LEN:
                    return True
    return False


def _whitelist_raw(raw: dict[str, Any]) -> dict[str, Any]:
    """Keep only usage-related keys; strip conversation bodies."""
    clean: dict[str, Any] = {}
    for k, v in raw.items():
        key = str(k)
        low = key.lower()
        if low in _FORBIDDEN_KEYS:
            continue
        if low not in {x.lower() for x in _USAGE_KEYS} and not low.endswith("_tokens"):
            # also allow camelCase variants already listed via lower compare
            camel_ok = low in {x.lower() for x in _USAGE_KEYS}
            if not camel_ok and not low.endswith("_tokens"):
                continue
        if isinstance(v, dict) and low in ("usage", "token_usage", "tokenusage"):
            nested = {}
            for nk, nv in v.items():
                nl = str(nk).lower()
                if nl in _FORBIDDEN_KEYS and not isinstance(nv, (int, float)):
                    continue
                if nl in _NUMERIC_USAGE_NESTED or nl.endswith("_tokens"):
                    if isinstance(nv, (int, float)) or (
                        isinstance(nv, str) and nv.strip().lstrip("-").replace(".", "", 1).isdigit()
                    ):
                        nested[nk] = nv
            clean["usage"] = nested
        elif isinstance(v, (str, int, float, bool)) or v is None:
            clean[key] = v
    return clean


def _project_from_path(cwd: Optional[str], fallback_dir: Optional[str] = None) -> Optional[str]:
    path = cwd or fallback_dir
    if not path:
        return None
    p = Path(str(path).rstrip("/"))
    name = p.name or str(p)
    return _as_str(name, max_len=128)


def _looks_like_usage(raw: dict[str, Any]) -> bool:
    t = str(raw.get("type") or raw.get("event") or "").lower()
    if "usage" in t or t in ("llm", "llm_usage", "token", "tokens", "token_usage"):
        return True
    if raw.get("model") or raw.get("model_id") or raw.get("model_name") or raw.get("modelName"):
        usage = raw.get("usage") or raw.get("token_usage") or raw.get("tokenUsage")
        if isinstance(usage, dict) and any(
            str(k).lower() in _NUMERIC_USAGE_NESTED or str(k).lower().endswith("_tokens")
            for k in usage
        ):
            return True
        if any(
            k in raw
            for k in (
                "prompt_tokens",
                "completion_tokens",
                "input_tokens",
                "output_tokens",
                "total_tokens",
            )
        ):
            return True
    return False


def _usage_bucket(src: dict[str, Any], raw: dict[str, Any]) -> dict[str, Any]:
    for key in ("usage", "token_usage", "tokenUsage"):
        u = src.get(key) if key in src else None
        if isinstance(u, dict):
            return u
        u = raw.get(key)
        if isinstance(u, dict):
            # numeric-only view
            out = {}
            for nk, nv in u.items():
                if isinstance(nv, (int, float)) or (
                    isinstance(nv, str) and nv.strip().lstrip("-").replace(".", "", 1).isdigit()
                ):
                    out[nk] = nv
            return out
    return {}


def map_workbuddy_event(
    raw: dict[str, Any],
    *,
    source_project_dir: Optional[str] = None,
    stats: Optional[ParseStats] = None,
) -> Optional[dict[str, Any]]:
    if not isinstance(raw, dict):
        if stats:
            stats.skipped_other += 1
        return None

    if _has_heavy_forbidden_content(raw):
        # Still allow if it also has a clean usage shape AND we strip — but product rule:
        # reject lines that carry large content/prompt so we never touch them.
        # Exception: if type is clearly llm.usage and forbidden keys are only empty/short, already handled.
        # Here content is heavy → skip entire line.
        if stats:
            stats.skipped_forbidden_content += 1
        return None

    if not _looks_like_usage(raw):
        if stats:
            stats.skipped_not_usage += 1
        return None

    clean = _whitelist_raw(raw)
    src = clean if (clean.get("model") or clean.get("model_name") or clean.get("usage")) else _whitelist_raw(raw)
    usage = _usage_bucket(src, raw)

    model = _as_str(
        src.get("model")
        or src.get("model_id")
        or src.get("model_name")
        or src.get("modelName")
        or src.get("modelId")
        or raw.get("model")
        or raw.get("model_name")
        or raw.get("modelName")
    )
    if not model:
        if stats:
            stats.skipped_missing_model += 1
        return None

    def _u(*keys: str) -> Any:
        for k in keys:
            if k in src and src[k] is not None:
                return src[k]
            if k in usage and usage[k] is not None:
                return usage[k]
            # case-insensitive in usage
            for uk, uv in usage.items():
                if str(uk).lower() == k.lower() and uv is not None:
                    return uv
        return None

    prompt = _as_int(
        _u(
            "prompt_tokens",
            "input_tokens",
            "input",
            "prompt",
        )
    )
    completion = _as_int(
        _u(
            "completion_tokens",
            "output_tokens",
            "output",
            "completion",
        )
    )
    cache_read = _as_int(_u("cache_read_tokens", "cache_read", "cacheReadTokens"))
    cache_write = _as_int(_u("cache_write_tokens", "cache_write", "cacheWriteTokens"))
    reasoning = _as_int(_u("reasoning_tokens", "reasoning", "reasoningTokens"))
    total = _as_int(_u("total_tokens", "total"))
    if total == 0:
        total = prompt + completion + cache_read + cache_write + reasoning
    if prompt == 0 and completion == 0 and total == 0:
        if stats:
            stats.skipped_missing_tokens += 1
        return None

    cwd = _as_str(
        src.get("cwd")
        or src.get("project_path")
        or src.get("projectPath")
        or raw.get("cwd")
        or raw.get("project_path"),
        max_len=512,
    )
    project_id = _as_str(
        src.get("project_id")
        or src.get("projectId")
        or src.get("project")
        or raw.get("project_id")
        or raw.get("projectId")
    ) or _project_from_path(cwd, source_project_dir)

    account_id = _as_str(
        src.get("account_id")
        or src.get("accountId")
        or src.get("account")
        or src.get("email")
        or src.get("userEmail")
        or src.get("user_email")
        or src.get("owningUser")
        or src.get("user")
        or raw.get("account_id")
        or raw.get("email")
        or raw.get("userEmail")
        or raw.get("owningUser")
    )
    participant = _as_str(
        src.get("participant")
        or src.get("user")
        or src.get("email")
        or src.get("userEmail")
        or account_id
        or "anonymous"
    )
    team = _as_str(src.get("team") or src.get("team_id") or src.get("teamId") or raw.get("team"))

    tag_parts = []
    if cache_read:
        tag_parts.append(f"cache_read:{cache_read}")
    if cache_write:
        tag_parts.append(f"cache_write:{cache_write}")
    if reasoning:
        tag_parts.append(f"reasoning:{reasoning}")
    if cwd:
        tag_parts.append(f"cwd:{Path(cwd).name}")
    tag = ",".join(tag_parts)[:128] if tag_parts else None

    ts = _parse_ts(
        src.get("timestamp")
        or src.get("timestampMs")
        or src.get("timestamp_ms")
        or src.get("ts")
        or src.get("created_at")
        or src.get("createdAt")
        or src.get("time")
        or raw.get("timestamp")
        or raw.get("createdAt")
    )

    request_id = _as_str(
        src.get("request_id")
        or src.get("requestId")
        or src.get("id")
        or src.get("call_id")
        or src.get("callId")
        or src.get("event_id")
        or src.get("eventId")
        or raw.get("request_id")
        or raw.get("requestId")
        or raw.get("id")
        or raw.get("callId"),
        max_len=256,
    )
    if not request_id:
        request_id = _as_str(
            f"workbuddy:{account_id or participant}:{ts}:{model}:{project_id or ''}",
            max_len=256,
        )

    agent_obj = raw.get("agent") if isinstance(raw.get("agent"), dict) else None
    agent_id = _as_str(
        src.get("agent_id")
        or src.get("bot_id")
        or src.get("botId")
        or src.get("automationId")
        or src.get("automation_id")
        or src.get("cloudAgentId")
        or (agent_obj.get("id") if agent_obj else None)
        or (src.get("agent") if isinstance(src.get("agent"), str) else None)
        or raw.get("agent_id")
        or raw.get("cloudAgentId")
    )
    agent_name = _as_str(
        src.get("agent_name")
        or src.get("agentName")
        or (agent_obj.get("name") if agent_obj else None)
        or raw.get("agent_name")
    )

    if stats:
        stats.parsed_ok += 1

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
        "tool": normalize_tool("workbuddy"),
        "account_id": account_id,
        "project_id": project_id,
        "agent_id": agent_id,
        "agent_name": agent_name,
        "request_id": request_id,
        "source": "workbuddy",
        "cost_usd": None,
        "cost_basis": None,
        "ts": ts,
    }


def map_workbuddy_events(
    events: list[dict[str, Any]],
    *,
    source_project_dir: Optional[str] = None,
    stats: Optional[ParseStats] = None,
) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for raw in events:
        row = map_workbuddy_event(raw, source_project_dir=source_project_dir, stats=stats)
        if row:
            out.append(row)
    return out


def extract_workbuddy_payload(payload: Any) -> list[dict[str, Any]]:
    if payload is None:
        return []
    if isinstance(payload, list):
        return [x for x in payload if isinstance(x, dict)]
    if isinstance(payload, dict):
        for key in ("events", "data", "items", "records", "lines"):
            if key in payload and isinstance(payload[key], list):
                return [x for x in payload[key] if isinstance(x, dict)]
        return [payload]
    return []


def parse_jsonl_text(
    text: str,
    *,
    source_project_dir: Optional[str] = None,
    stats: Optional[ParseStats] = None,
) -> list[dict[str, Any]]:
    local = stats or ParseStats()
    events: list[dict[str, Any]] = []
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        local.lines_total += 1
        try:
            obj = json.loads(line)
        except json.JSONDecodeError:
            local.skipped_json_error += 1
            continue
        if isinstance(obj, dict):
            events.append(obj)
        else:
            local.skipped_other += 1
    rows = map_workbuddy_events(events, source_project_dir=source_project_dir, stats=local)
    if stats is None:
        # stats was local-only; callers that ignore stats still get rows
        pass
    return rows


def parse_jsonl_text_with_stats(
    text: str, *, source_project_dir: Optional[str] = None
) -> tuple[list[dict[str, Any]], ParseStats]:
    stats = ParseStats()
    rows = parse_jsonl_text(text, source_project_dir=source_project_dir, stats=stats)
    return rows, stats


def scan_workbuddy_dir(
    root: Optional[str | Path] = None,
    *,
    stats: Optional[ParseStats] = None,
) -> list[dict[str, Any]]:
    """Scan projects dir for *.jsonl; map usage-only rows."""
    if root is None:
        root = os.environ.get("WORKBUDDY_PROJECTS_DIR") or str(
            Path.home() / ".workbuddy" / "projects"
        )
    root_path = Path(root).expanduser()
    if not root_path.is_dir():
        return []

    local = stats or ParseStats()
    rows: list[dict[str, Any]] = []
    for path in sorted(root_path.rglob("*.jsonl")):
        local.files_scanned += 1
        try:
            rel = path.relative_to(root_path)
            project_hint = rel.parts[0] if rel.parts else path.parent.name
        except ValueError:
            project_hint = path.parent.name
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            local.skipped_other += 1
            local.notes.append(f"unreadable:{path}")
            continue
        rows.extend(parse_jsonl_text(text, source_project_dir=project_hint, stats=local))
    return rows


def scan_workbuddy_dir_with_stats(
    root: Optional[str | Path] = None,
) -> tuple[list[dict[str, Any]], ParseStats]:
    stats = ParseStats()
    rows = scan_workbuddy_dir(root, stats=stats)
    return rows, stats
