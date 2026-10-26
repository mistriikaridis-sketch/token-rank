"""Claude Code local jsonl adapter — usage-only from assistant message.usage.

Scans CLAUDE_PROJECTS_DIR (default ~/.claude/projects) for *.jsonl.
Only parses type=assistant rows with message.usage; never stores content/text.
Dedupes streaming duplicates by message.id (keep max output_tokens).
"""
from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

from app.tools import normalize_tool

# Conversation / body keys — never persist
_FORBIDDEN_KEYS = frozenset(
    {
        "content",
        "messages",
        "prompt",
        "response",
        "text",
        "delta",
        "choices",
        "system",
        "transcript",
        "body",
        "thinking",
        "tool_use",
        "tool_result",
        "input",
        "output",
    }
)


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


def _project_from_cwd(cwd: Optional[str], fallback: Optional[str] = None) -> Optional[str]:
    path = cwd or fallback
    if not path:
        return None
    # Claude stores dir as sanitized cwd like -Users-alice-work-chatbot-core
    p = Path(str(path).rstrip("/"))
    name = p.name or str(p)
    # If sanitized slug, take last segment after final hyphen group that looks like project
    if name.startswith("-") and "/" not in name:
        # e.g. -Users-alice-work-chatbot-core → chatbot-core (last 2 parts if plausible)
        parts = [x for x in name.split("-") if x]
        if len(parts) >= 1:
            name = parts[-1] if len(parts) == 1 else "-".join(parts[-2:]) if parts[-2] not in (
                "Users",
                "home",
                "tmp",
                "var",
                "opt",
            ) else parts[-1]
            # Prefer last path component of original cwd when available
    if cwd and ("/" in cwd or cwd.startswith("~")):
        name = Path(cwd.rstrip("/")).name or name
    return _as_str(name, max_len=128)


def _usage_from_message(message: dict[str, Any]) -> Optional[dict[str, int]]:
    usage = message.get("usage")
    if not isinstance(usage, dict) or not usage:
        return None
    # Only numeric token buckets — ignore nested cache_creation breakdown for counting
    # (flat cache_creation_input_tokens already totals ephemeral halves)
    input_tokens = _as_int(usage.get("input_tokens"))
    output_tokens = _as_int(usage.get("output_tokens"))
    cache_read = _as_int(
        usage.get(
            "cache_read_input_tokens",
            usage.get("cache_read_tokens", usage.get("cache_read")),
        )
    )
    cache_write = _as_int(
        usage.get(
            "cache_creation_input_tokens",
            usage.get("cache_write_tokens", usage.get("cache_write")),
        )
    )
    # Must have at least some token signal
    if input_tokens + output_tokens + cache_read + cache_write <= 0:
        return None
    return {
        "input_tokens": input_tokens,
        "output_tokens": output_tokens,
        "cache_read_tokens": cache_read,
        "cache_write_tokens": cache_write,
    }


def _strip_content(obj: Any) -> Any:
    """Recursively drop forbidden text bodies (defense in depth for push path)."""
    if isinstance(obj, dict):
        out: dict[str, Any] = {}
        for k, v in obj.items():
            low = str(k).lower()
            # message is special: keep usage/id/model only (not in blanket drop)
            if low == "message" and isinstance(v, dict):
                msg: dict[str, Any] = {}
                if "id" in v and isinstance(v["id"], (str, int)):
                    msg["id"] = v["id"]
                if "model" in v and isinstance(v["model"], str):
                    msg["model"] = v["model"]
                if "role" in v and isinstance(v["role"], str):
                    msg["role"] = v["role"]
                if "stop_reason" in v and isinstance(v["stop_reason"], (str, type(None))):
                    msg["stop_reason"] = v["stop_reason"]
                usage = v.get("usage")
                if isinstance(usage, dict):
                    clean_usage: dict[str, Any] = {}
                    for uk, uv in usage.items():
                        if isinstance(uv, (int, float)):
                            clean_usage[uk] = uv
                        elif isinstance(uv, dict) and all(
                            isinstance(x, (int, float)) for x in uv.values()
                        ):
                            # cache_creation / server_tool_use numeric nests — keep ints only
                            clean_usage[uk] = {
                                nk: nv for nk, nv in uv.items() if isinstance(nv, (int, float))
                            }
                    msg["usage"] = clean_usage
                out["message"] = msg
                continue
            if low in _FORBIDDEN_KEYS:
                continue
            if isinstance(v, (dict, list)):
                out[k] = _strip_content(v)
            elif isinstance(v, (str, int, float, bool)) or v is None:
                out[k] = v
        return out
    if isinstance(obj, list):
        return [_strip_content(x) for x in obj]
    return obj


def map_claude_code_event(
    raw: dict[str, Any], *, source_project_dir: Optional[str] = None
) -> Optional[dict[str, Any]]:
    """Map one Claude Code jsonl object → usage row, or None if not assistant+usage."""
    if not isinstance(raw, dict):
        return None

    # Prefer assistant type; also accept already-stripped push with message.usage
    etype = str(raw.get("type") or "").lower()
    message = raw.get("message")
    if not isinstance(message, dict):
        # Allow pre-normalized push: top-level usage + model
        if etype and etype not in ("assistant", "claude_code", "llm.usage", "usage"):
            return None
        message = {
            "id": raw.get("message_id") or raw.get("id"),
            "model": raw.get("model"),
            "usage": raw.get("usage") if isinstance(raw.get("usage"), dict) else None,
        }
        if not isinstance(message.get("usage"), dict):
            return None
    elif etype and etype not in ("assistant", "", "claude_code"):
        # user / system / attachment etc. — skip
        if etype != "assistant":
            return None

    usage_parts = _usage_from_message(message)
    if not usage_parts:
        return None

    model = _as_str(message.get("model") or raw.get("model"))
    if not model:
        return None

    message_id = _as_str(message.get("id") or raw.get("message_id"), max_len=256)
    request_id_top = _as_str(raw.get("requestId") or raw.get("request_id"), max_len=256)
    # Spec: request_id := message.id for charge-level dedup
    request_id = message_id or request_id_top
    if not request_id:
        # Last resort synthetic — still better than stacking blanks
        uuid = _as_str(raw.get("uuid"), max_len=256)
        request_id = uuid

    prompt = usage_parts["input_tokens"]
    completion = usage_parts["output_tokens"]
    cache_read = usage_parts["cache_read_tokens"]
    cache_write = usage_parts["cache_write_tokens"]
    total = prompt + completion + cache_read + cache_write

    cwd = _as_str(raw.get("cwd") or raw.get("project_path"), max_len=512)
    project_id = _as_str(raw.get("project_id") or raw.get("project")) or _project_from_cwd(
        cwd, source_project_dir
    )

    account_id = _as_str(
        raw.get("account_id")
        or raw.get("account")
        or raw.get("email")
        or raw.get("gitUserEmail")
        or os.environ.get("CLAUDE_ACCOUNT_ID")
    )
    participant = _as_str(
        raw.get("participant")
        or raw.get("user")
        or account_id
        or "claude-code-user"
    )
    team = _as_str(raw.get("team") or raw.get("team_id"))

    tag_parts = []
    if cache_read:
        tag_parts.append(f"cache_read:{cache_read}")
    if cache_write:
        tag_parts.append(f"cache_write:{cache_write}")
    if cwd:
        tag_parts.append(f"cwd:{Path(cwd).name}")
    session = _as_str(raw.get("sessionId") or raw.get("session_id"), max_len=64)
    if session:
        tag_parts.append(f"session:{session[:12]}")
    tag = ",".join(tag_parts)[:128] if tag_parts else None

    ts = _parse_ts(raw.get("timestamp") or raw.get("ts") or raw.get("created_at"))

    return {
        "model": model,
        "prompt_tokens": prompt,
        "completion_tokens": completion,
        "total_tokens": total,
        "cache_read_tokens": cache_read,
        "cache_write_tokens": cache_write,
        "reasoning_tokens": 0,
        "team": team,
        "api_key": None,
        "tag": tag,
        "participant": participant,
        "tool": normalize_tool("claude_code"),
        "account_id": account_id,
        "project_id": project_id,
        "request_id": request_id,
        "source": "claude_code",
        "cost_usd": None,
        "cost_basis": None,
        "ts": ts,
        # Internal: for in-file streaming dedup before insert
        "_output_tokens": completion,
        "_message_id": message_id,
    }


def _dedupe_by_message_id(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """One API call → many jsonl lines; keep row with largest output_tokens per message.id."""
    by_key: dict[str, dict[str, Any]] = {}
    passthrough: list[dict[str, Any]] = []
    for row in rows:
        mid = row.get("_message_id") or row.get("request_id")
        if not mid:
            clean = {k: v for k, v in row.items() if not str(k).startswith("_")}
            passthrough.append(clean)
            continue
        key = str(mid)
        prev = by_key.get(key)
        if prev is None or int(row.get("_output_tokens") or 0) >= int(
            prev.get("_output_tokens") or 0
        ):
            by_key[key] = row
    out: list[dict[str, Any]] = []
    for row in by_key.values():
        out.append({k: v for k, v in row.items() if not str(k).startswith("_")})
    out.extend(passthrough)
    return out


def map_claude_code_events(
    events: list[dict[str, Any]], *, source_project_dir: Optional[str] = None
) -> list[dict[str, Any]]:
    mapped: list[dict[str, Any]] = []
    for raw in events:
        # Strip bodies before mapping (push path may include content)
        safe = _strip_content(raw) if isinstance(raw, dict) else raw
        if not isinstance(safe, dict):
            continue
        row = map_claude_code_event(safe, source_project_dir=source_project_dir)
        if row:
            mapped.append(row)
    return _dedupe_by_message_id(mapped)


def extract_claude_code_payload(payload: Any) -> list[dict[str, Any]]:
    if payload is None:
        return []
    if isinstance(payload, list):
        return [x for x in payload if isinstance(x, dict)]
    if isinstance(payload, dict):
        for key in ("events", "data", "items", "records", "lines", "messages"):
            if key in payload and isinstance(payload[key], list):
                return [x for x in payload[key] if isinstance(x, dict)]
        return [payload]
    return []


def parse_jsonl_text(
    text: str, *, source_project_dir: Optional[str] = None
) -> list[dict[str, Any]]:
    events: list[dict[str, Any]] = []
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        try:
            obj = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(obj, dict):
            events.append(obj)
    return map_claude_code_events(events, source_project_dir=source_project_dir)


def scan_claude_code_dir(root: Optional[str | Path] = None) -> list[dict[str, Any]]:
    """Scan ~/.claude/projects (or CLAUDE_PROJECTS_DIR) for *.jsonl usage rows."""
    if root is None:
        root = os.environ.get("CLAUDE_PROJECTS_DIR") or str(
            Path.home() / ".claude" / "projects"
        )
    root_path = Path(root).expanduser()
    if not root_path.is_dir():
        return []

    rows: list[dict[str, Any]] = []
    for path in sorted(root_path.rglob("*.jsonl")):
        try:
            rel = path.relative_to(root_path)
            project_hint = rel.parts[0] if rel.parts else path.parent.name
        except ValueError:
            project_hint = path.parent.name
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        rows.extend(parse_jsonl_text(text, source_project_dir=str(project_hint)))
    # Cross-file dedupe by request_id / message.id (session resume can re-log)
    return _dedupe_by_message_id(
        [
            {**r, "_message_id": r.get("request_id"), "_output_tokens": r.get("completion_tokens", 0)}
            for r in rows
        ]
    )
