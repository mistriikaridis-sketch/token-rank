"""Cursor usage adapter — Admin API events and local usage-only reads.

Docs: POST https://api.cursor.com/teams/filtered-usage-events
Auth: Basic (API key as username, empty password). Env: CURSOR_ADMIN_API_KEY.

Also accepts offline export JSON (fixtures / local ~/.cursor/exports).

Local scan (``scan_cursor_dir``) reads two things and nothing else:

* ``state.vscdb`` ``cursorDiskKV`` rows keyed ``bubbleId:*``. SQL extracts
  token counts and the model name only. Prompt text and auth tokens are
  not selected.
* JSON/JSONL usage exports (dashboard ``filtered-usage-events`` shape).

Bubbles whose token counts are all zero are skipped. Current Cursor builds
often persist 0/0 locally; a usage export is the fallback. This module does
not read session cookies and does not call the dashboard.
"""
from __future__ import annotations

import json
import os
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

from app.adapters.parse_stats import ParseStats
from app.tools import normalize_tool

# Usage exports only. Do not open transcripts, argv, or MCP config.
_MAX_USAGE_BYTES = 32 * 1024 * 1024
_SKIP_EXPORT_NAMES = frozenset(
    {
        "argv.json",
        "mcp.json",
        "package.json",
        "package-lock.json",
        "settings.json",
        "ide_state.json",
    }
)
_EMAIL_ITEM_KEYS = ("cursorAuth/cachedEmail", "cursorAuth/email")

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


def _directory_name(value: Any) -> Optional[str]:
    """Keep a project label. Full paths become the last path segment."""
    if value is None:
        return None
    text = str(value).strip()
    if not text:
        return None
    if "/" in text or "\\" in text or text.startswith("~"):
        normalized = text.replace("\\", "/").rstrip("/")
        text = normalized.rsplit("/", 1)[-1]
    if not text or text == "~":
        return None
    return _as_str(text)


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
    # Collector payloads already carry the resolved company account. Prefer
    # that over the log email so a second map (ingest) does not drop it.
    explicit_account = _as_str(raw.get("account_id"))
    explicit_participant = _as_str(raw.get("participant"))
    account_id = explicit_account or email or user_id
    participant = explicit_participant or email or user_id or account_id or "anonymous"

    conversation_id = _as_str(
        raw.get("conversationId")
        or raw.get("conversation_id")
        or raw.get("composerId")
        or raw.get("composer_id"),
        max_len=128,
    )
    project_id = _project_from_conversation(conversation_id)
    explicit_project = _directory_name(raw.get("project_id") or raw.get("projectId") or raw.get("project"))
    if explicit_project:
        project_id = explicit_project

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


_SKIP_WALK = frozenset(
    {
        "node_modules",
        "cache",
        "code cache",
        "gpucache",
        "blob_storage",
        "shadercache",
        "backup",
        "backups",
    }
)
_BUBBLE_SQL = """
SELECT
  key,
  COALESCE(json_extract(value, '$.tokenCount.inputTokens'),
           json_extract(value, '$.tokenUsage.inputTokens'), 0),
  COALESCE(json_extract(value, '$.tokenCount.outputTokens'),
           json_extract(value, '$.tokenUsage.outputTokens'), 0),
  COALESCE(json_extract(value, '$.tokenCount.cacheReadTokens'),
           json_extract(value, '$.tokenUsage.cacheReadTokens'), 0),
  COALESCE(json_extract(value, '$.tokenCount.cacheWriteTokens'),
           json_extract(value, '$.tokenUsage.cacheWriteTokens'), 0),
  COALESCE(json_extract(value, '$.tokenCount.reasoningTokens'),
           json_extract(value, '$.tokenUsage.reasoningTokens'), 0),
  COALESCE(json_extract(value, '$.modelInfo.modelName'),
           json_extract(value, '$.modelName'),
           json_extract(value, '$.model')),
  COALESCE(CAST(json_extract(value, '$.createdAt') AS TEXT),
           CAST(json_extract(value, '$.timestamp') AS TEXT),
           CAST(json_extract(value, '$.timestampMs') AS TEXT))
FROM cursorDiskKV
WHERE key LIKE 'bubbleId:%'
"""


def _skipped_walk(path: Path) -> bool:
    return any(part.lower() in _SKIP_WALK for part in path.parts)


def _is_usage_export(path: Path) -> bool:
    if not path.is_file() or _skipped_walk(path):
        return False
    if path.suffix.lower() not in {".json", ".jsonl"}:
        return False
    name = path.name.lower()
    if name in _SKIP_EXPORT_NAMES:
        return False
    if any(part in name for part in ("secret", "cookie", "credential", "password", "token")):
        return False
    if "usage" in name:
        return True
    return path.parent.name.lower() == "exports"


def _plain_email(value: Any) -> Optional[str]:
    if value is None:
        return None
    if isinstance(value, bytes):
        value = value.decode("utf-8", errors="replace")
    text = str(value).strip()
    if len(text) >= 2 and text[0] == text[-1] and text[0] in {'"', "'"}:
        text = text[1:-1].strip()
    if not text or len(text) > 128 or "@" not in text or any(ch.isspace() for ch in text):
        return None
    local, _, domain = text.partition("@")
    if not local or "." not in domain:
        return None
    return text


def _logged_in_email(conn: sqlite3.Connection) -> Optional[str]:
    """Email only. Never select access tokens, refresh tokens, or cookies."""
    try:
        rows = conn.execute(
            "SELECT key, value FROM ItemTable WHERE key IN (?, ?)",
            _EMAIL_ITEM_KEYS,
        ).fetchall()
    except sqlite3.Error:
        return None
    by_key = {str(key): value for key, value in rows}
    for key in _EMAIL_ITEM_KEYS:
        email = _plain_email(by_key.get(key))
        if email:
            return email
    return None


def _bubble_ids(key: str) -> tuple[str, str]:
    rest = key[len("bubbleId:") :] if key.startswith("bubbleId:") else key
    conv, sep, bubble = rest.partition(":")
    if sep and conv and bubble:
        return conv, bubble
    return "", rest or key


def _as_count(value: Any) -> int:
    if value is None:
        return 0
    try:
        return max(int(float(value)), 0)
    except (TypeError, ValueError):
        return 0


def _rows_from_state_db(path: Path) -> list[dict[str, Any]]:
    uri = f"{path.resolve().as_uri()}?mode=ro"
    try:
        conn = sqlite3.connect(uri, uri=True)
    except sqlite3.Error:
        return []
    try:
        conn.execute("PRAGMA query_only=ON")
        email = _logged_in_email(conn)
        try:
            fetched = conn.execute(_BUBBLE_SQL).fetchall()
        except sqlite3.Error:
            return []
    finally:
        conn.close()

    rows: list[dict[str, Any]] = []
    for key, prompt, completion, cache_read, cache_write, reasoning, model, created in fetched:
        prompt_n = _as_count(prompt)
        completion_n = _as_count(completion)
        cache_read_n = _as_count(cache_read)
        cache_write_n = _as_count(cache_write)
        reasoning_n = _as_count(reasoning)
        # Current Cursor builds often store 0/0. Do not invent a count.
        if prompt_n == 0 and completion_n == 0 and cache_read_n == 0 and cache_write_n == 0 and reasoning_n == 0:
            continue
        model_name = str(model).strip() if model is not None else ""
        if not model_name:
            continue
        conv, bubble = _bubble_ids(str(key))
        raw: dict[str, Any] = {
            "model": model_name,
            "timestamp": created,
            "conversationId": conv or None,
            "request_id": f"cursor:bubble:{conv}:{bubble}"[:256],
            "tokenUsage": {
                "inputTokens": prompt_n,
                "outputTokens": completion_n,
                "cacheReadTokens": cache_read_n,
                "cacheWriteTokens": cache_write_n,
                "reasoningTokens": reasoning_n,
            },
        }
        if email:
            raw["userEmail"] = email
        row = map_cursor_event(raw)
        if row:
            rows.append(row)
    return rows


def _state_dbs(root: Path) -> list[Path]:
    found: list[Path] = []
    for path in root.rglob("state.vscdb"):
        if not path.is_file() or path.name != "state.vscdb" or _skipped_walk(path):
            continue
        found.append(path)
    global_dbs = [path for path in found if "globalstorage" in {part.lower() for part in path.parts}]
    return global_dbs or found


def _rows_from_export(path: Path) -> list[dict[str, Any]]:
    try:
        size = path.stat().st_size
    except OSError:
        return []
    if size <= 0 or size > _MAX_USAGE_BYTES:
        return []
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return []
    if path.suffix.lower() == ".jsonl":
        events: list[dict[str, Any]] = []
        for line in text.splitlines():
            raw_line = line.strip()
            if not raw_line or raw_line.startswith("#"):
                continue
            try:
                obj = json.loads(raw_line)
            except json.JSONDecodeError:
                continue
            events.extend(extract_cursor_events(obj))
        return map_cursor_events(events)
    try:
        payload = json.loads(text)
    except json.JSONDecodeError:
        return []
    return map_cursor_payload(payload)


def _dedupe_cursor(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    seen: set[str] = set()
    out: list[dict[str, Any]] = []
    for row in rows:
        rid = row.get("request_id")
        if rid:
            key = str(rid)
            if key in seen:
                continue
            seen.add(key)
        out.append(row)
    return out


def scan_cursor_dir(root: Optional[str | Path] = None) -> list[dict[str, Any]]:
    """Scan one Cursor root for usage exports and globalStorage state.vscdb.

    ``CURSOR_DATA_DIR`` is the default when ``root`` is omitted. The collector
    passes each discovered directory itself (``~/.cursor``, Application Support,
    ``%APPDATA%\\Cursor``, and so on).
    """
    if root is None:
        root = os.environ.get("CURSOR_DATA_DIR") or str(Path.home() / ".cursor")
    root_path = Path(root).expanduser()
    if not root_path.is_dir():
        return []

    rows: list[dict[str, Any]] = []
    for path in _state_dbs(root_path):
        rows.extend(_rows_from_state_db(path))
    exports = sorted(path for path in root_path.rglob("*") if _is_usage_export(path))
    for path in exports:
        rows.extend(_rows_from_export(path))
    return _dedupe_cursor(rows)
