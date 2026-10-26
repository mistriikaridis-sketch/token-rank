"""Codex CLI local rollout adapter — usage-only from ~/.codex/sessions.

Scans CODEX_SESSIONS_DIR (default ~/.codex/sessions) for rollout-*.jsonl.
Prefer type=token_usage_record (request_id=response_id). Fall back to
event_msg/token_count only when a file has no records (older logs).

Critical: cached_input_tokens is already included inside input_tokens.
Store fresh prompt = input - cached; cache_read = cached. Never store
response_item conversation bodies.
"""
from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

from app.tools import normalize_tool

# Conversation / body keys — never persist on push path
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
        "reasoning",
        "encrypted_content",
        "input",
        "output",
        "function_call",
        "custom_tool_call",
        "agent_message",
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


def _project_from_cwd(cwd: Optional[str]) -> Optional[str]:
    if not cwd:
        return None
    # Internal Codex harness dirs are not user projects
    expanded = str(Path(cwd).expanduser())
    codex_home = str((Path.home() / ".codex").resolve())
    try:
        if expanded.startswith(codex_home + os.sep) or expanded == codex_home:
            return _as_str("codex-internal", max_len=128)
    except Exception:  # noqa: BLE001
        pass
    name = Path(str(cwd).rstrip("/")).name or str(cwd)
    return _as_str(name, max_len=128)


def _split_input_cache(usage: dict[str, Any]) -> dict[str, int]:
    """Split Codex usage where cached ⊆ input (do NOT add them).

    fresh_prompt = input_tokens - cached_input_tokens
    cache_read   = cached_input_tokens
    """
    input_tokens = _as_int(
        usage.get(
            "input_tokens",
            usage.get("prompt_tokens", usage.get("input")),
        )
    )
    cached = _as_int(
        usage.get(
            "cached_input_tokens",
            usage.get(
                "cache_read_input_tokens",
                usage.get("cache_read_tokens", usage.get("cache_read")),
            ),
        )
    )
    if cached > input_tokens:
        cached = input_tokens
    fresh = max(input_tokens - cached, 0)
    output_tokens = _as_int(
        usage.get(
            "output_tokens",
            usage.get("completion_tokens", usage.get("output")),
        )
    )
    cache_write = _as_int(
        usage.get(
            "cache_write_input_tokens",
            usage.get("cache_write_tokens", usage.get("cache_write")),
        )
    )
    reasoning = _as_int(
        usage.get(
            "reasoning_output_tokens",
            usage.get("reasoning_tokens", usage.get("reasoning")),
        )
    )
    # Prefer Codex total (input+output); else recompute without double-counting cache
    total = _as_int(usage.get("total_tokens"))
    if total <= 0:
        total = input_tokens + output_tokens + cache_write
    return {
        "prompt_tokens": fresh,
        "completion_tokens": output_tokens,
        "cache_read_tokens": cached,
        "cache_write_tokens": cache_write,
        "reasoning_tokens": reasoning,
        "total_tokens": total,
        "_raw_input_tokens": input_tokens,
    }


def _usage_dict(obj: Any) -> Optional[dict[str, Any]]:
    if not isinstance(obj, dict) or not obj:
        return None
    # Must have some token signal
    if not any(
        k in obj
        for k in (
            "input_tokens",
            "output_tokens",
            "prompt_tokens",
            "completion_tokens",
            "total_tokens",
            "cached_input_tokens",
        )
    ):
        return None
    return obj


def _strip_content(obj: Any) -> Any:
    """Drop conversation bodies (defense in depth for push path)."""
    if isinstance(obj, dict):
        out: dict[str, Any] = {}
        for k, v in obj.items():
            low = str(k).lower()
            if low in _FORBIDDEN_KEYS:
                continue
            # Keep usage nests (numeric) and thin envelopes
            if low == "usage" and isinstance(v, dict):
                clean_usage: dict[str, Any] = {}
                for uk, uv in v.items():
                    if isinstance(uv, (int, float)):
                        clean_usage[uk] = uv
                out["usage"] = clean_usage
                continue
            if low == "payload" and isinstance(v, dict):
                out["payload"] = _strip_content(v)
                continue
            if low == "info" and isinstance(v, dict):
                # token_count info — keep only usage snapshots
                info: dict[str, Any] = {}
                for ik in ("last_token_usage", "total_token_usage"):
                    u = v.get(ik)
                    if isinstance(u, dict):
                        info[ik] = {
                            nk: nv
                            for nk, nv in u.items()
                            if isinstance(nv, (int, float))
                        }
                if "type" in v and isinstance(v["type"], str):
                    info["type"] = v["type"]
                out["info"] = info
                continue
            if isinstance(v, (dict, list)):
                out[k] = _strip_content(v)
            elif isinstance(v, (str, int, float, bool)) or v is None:
                # Drop long free-text that looks like chat (keep ids/paths/models)
                if isinstance(v, str) and len(v) > 512 and low not in (
                    "cwd",
                    "model",
                    "response_id",
                    "session_id",
                    "thread_id",
                    "turn_id",
                    "id",
                    "timestamp",
                ):
                    continue
                out[k] = v
        return out
    if isinstance(obj, list):
        return [_strip_content(x) for x in obj]
    return obj


def _row_from_usage(
    usage: dict[str, Any],
    *,
    response_id: Optional[str],
    model: Optional[str],
    cwd: Optional[str],
    ts: Any,
    session_id: Optional[str] = None,
    account_id: Optional[str] = None,
    team: Optional[str] = None,
    participant: Optional[str] = None,
    project_id: Optional[str] = None,
) -> Optional[dict[str, Any]]:
    parts = _split_input_cache(usage)
    if (
        parts["prompt_tokens"]
        + parts["completion_tokens"]
        + parts["cache_read_tokens"]
        + parts["cache_write_tokens"]
        <= 0
    ):
        return None

    model_s = _as_str(model)
    if not model_s:
        model_s = "unknown"

    rid = _as_str(response_id, max_len=256)
    if not rid:
        # Fallback synthetic (older token_count has no response_id)
        rid = _as_str(
            f"codex:{session_id or 'nosess'}:{_parse_ts(ts)}:"
            f"{model_s}:{parts['_raw_input_tokens']}:{parts['completion_tokens']}",
            max_len=256,
        )

    proj = _as_str(project_id) or _project_from_cwd(cwd)
    acct = _as_str(
        account_id
        or os.environ.get("CODEX_ACCOUNT_ID")
        or os.environ.get("OPENAI_ACCOUNT_ID")
    )
    part = _as_str(participant or acct or "codex-user")
    team_s = _as_str(team)

    tag_parts = []
    if parts["cache_read_tokens"]:
        tag_parts.append(f"cache_read:{parts['cache_read_tokens']}")
    if parts["cache_write_tokens"]:
        tag_parts.append(f"cache_write:{parts['cache_write_tokens']}")
    if parts["reasoning_tokens"]:
        tag_parts.append(f"reasoning:{parts['reasoning_tokens']}")
    if cwd:
        tag_parts.append(f"cwd:{Path(str(cwd)).name}")
    if session_id:
        tag_parts.append(f"session:{str(session_id)[:12]}")
    tag = ",".join(tag_parts)[:128] if tag_parts else None

    return {
        "model": model_s,
        "prompt_tokens": parts["prompt_tokens"],
        "completion_tokens": parts["completion_tokens"],
        "total_tokens": parts["total_tokens"],
        "cache_read_tokens": parts["cache_read_tokens"],
        "cache_write_tokens": parts["cache_write_tokens"],
        "reasoning_tokens": parts["reasoning_tokens"],
        "team": team_s,
        "api_key": None,
        "tag": tag,
        "participant": part,
        "tool": normalize_tool("codex"),
        "account_id": acct,
        "project_id": proj,
        "request_id": rid,
        "source": "codex",
        "cost_usd": None,
        "cost_basis": None,
        "ts": _parse_ts(ts),
    }


def map_codex_event(
    raw: dict[str, Any],
    *,
    default_cwd: Optional[str] = None,
    default_model: Optional[str] = None,
    default_session_id: Optional[str] = None,
) -> Optional[dict[str, Any]]:
    """Map one Codex rollout line or pre-normalized push object → usage row."""
    if not isinstance(raw, dict):
        return None

    etype = str(raw.get("type") or "").lower()
    payload = raw.get("payload") if isinstance(raw.get("payload"), dict) else None

    # --- rollout envelope: token_usage_record ---
    if etype == "token_usage_record" and payload is not None:
        usage = _usage_dict(payload.get("usage"))
        if not usage:
            return None
        return _row_from_usage(
            usage,
            response_id=_as_str(
                payload.get("response_id") or raw.get("response_id"), max_len=256
            ),
            model=default_model or payload.get("model") or raw.get("model"),
            cwd=default_cwd
            or payload.get("cwd")
            or raw.get("cwd"),
            ts=raw.get("timestamp") or payload.get("timestamp"),
            session_id=_as_str(
                payload.get("session_id")
                or payload.get("thread_id")
                or default_session_id
            ),
            account_id=raw.get("account_id") or raw.get("account") or raw.get("email"),
            team=raw.get("team") or raw.get("team_id"),
            participant=raw.get("participant") or raw.get("user"),
            project_id=raw.get("project_id") or raw.get("project"),
        )

    # --- rollout envelope: event_msg / token_count (fallback only at file level) ---
    if etype == "event_msg" and payload is not None:
        inner = str(payload.get("type") or "").lower()
        if inner != "token_count":
            return None
        info = payload.get("info") if isinstance(payload.get("info"), dict) else {}
        usage = _usage_dict(info.get("last_token_usage")) or _usage_dict(
            info.get("total_token_usage")
        )
        if not usage:
            return None
        return _row_from_usage(
            usage,
            response_id=_as_str(
                payload.get("response_id") or raw.get("response_id"), max_len=256
            ),
            model=default_model or payload.get("model") or raw.get("model"),
            cwd=default_cwd or payload.get("cwd") or raw.get("cwd"),
            ts=raw.get("timestamp") or payload.get("timestamp"),
            session_id=_as_str(
                payload.get("session_id") or default_session_id
            ),
            account_id=raw.get("account_id") or raw.get("account"),
            team=raw.get("team"),
            participant=raw.get("participant"),
            project_id=raw.get("project_id"),
        )

    # --- pre-normalized push: top-level usage + response_id / model ---
    usage = _usage_dict(raw.get("usage")) if isinstance(raw.get("usage"), dict) else None
    if usage is None and any(
        k in raw for k in ("input_tokens", "output_tokens", "prompt_tokens")
    ):
        usage = raw
    if usage is None:
        return None
    # Skip obvious non-usage types
    if etype in (
        "session_meta",
        "turn_context",
        "response_item",
        "world_state",
        "user",
        "assistant",
    ):
        return None

    return _row_from_usage(
        usage,
        response_id=_as_str(
            raw.get("response_id") or raw.get("request_id") or raw.get("id"),
            max_len=256,
        ),
        model=raw.get("model") or default_model,
        cwd=raw.get("cwd") or default_cwd,
        ts=raw.get("timestamp") or raw.get("ts") or raw.get("created_at"),
        session_id=_as_str(raw.get("session_id") or default_session_id),
        account_id=raw.get("account_id") or raw.get("account") or raw.get("email"),
        team=raw.get("team") or raw.get("team_id"),
        participant=raw.get("participant") or raw.get("user"),
        project_id=raw.get("project_id") or raw.get("project"),
    )


def _dedupe_by_request_id(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Keep first occurrence per request_id (response_id); resume replays same id."""
    seen: dict[str, dict[str, Any]] = {}
    passthrough: list[dict[str, Any]] = []
    for row in rows:
        rid = row.get("request_id")
        if not rid:
            passthrough.append(row)
            continue
        key = str(rid)
        if key not in seen:
            seen[key] = row
    return list(seen.values()) + passthrough


def parse_rollout_text(text: str) -> list[dict[str, Any]]:
    """Parse one rollout-*.jsonl body → usage rows (prefer token_usage_record)."""
    cwd: Optional[str] = None
    model: Optional[str] = None
    session_id: Optional[str] = None
    records: list[dict[str, Any]] = []
    fallbacks: list[dict[str, Any]] = []

    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        try:
            obj = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not isinstance(obj, dict):
            continue

        etype = str(obj.get("type") or "").lower()
        payload = obj.get("payload") if isinstance(obj.get("payload"), dict) else {}

        if etype == "session_meta":
            cwd = _as_str(payload.get("cwd"), max_len=512) or cwd
            session_id = _as_str(
                payload.get("session_id") or payload.get("id"), max_len=128
            ) or session_id
            continue
        if etype == "turn_context":
            model = _as_str(payload.get("model")) or model
            cwd = _as_str(payload.get("cwd"), max_len=512) or cwd
            continue
        if etype == "token_usage_record":
            row = map_codex_event(
                obj,
                default_cwd=cwd,
                default_model=model,
                default_session_id=session_id,
            )
            if row:
                records.append(row)
            continue
        if etype == "event_msg" and str(payload.get("type") or "").lower() == "token_count":
            row = map_codex_event(
                obj,
                default_cwd=cwd,
                default_model=model,
                default_session_id=session_id,
            )
            if row:
                fallbacks.append(row)
            continue
        # response_item / world_state / etc. — ignore (no conversation stored)

    if records:
        return _dedupe_by_request_id(records)
    return _dedupe_by_request_id(fallbacks)


def map_codex_events(events: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Map a push payload list. If any token_usage_record present, ignore token_count."""
    stripped = [
        _strip_content(e) if isinstance(e, dict) else e for e in events if isinstance(e, dict)
    ]

    # Detect whether this looks like one or more rollout streams
    has_record = any(
        str(e.get("type") or "").lower() == "token_usage_record" for e in stripped
    )
    # If mixed with session_meta / turn_context, parse as a synthetic rollout
    has_meta = any(
        str(e.get("type") or "").lower() in ("session_meta", "turn_context")
        for e in stripped
    )
    if has_meta or (
        has_record
        and any(isinstance(e.get("payload"), dict) for e in stripped)
    ):
        # Reconstruct jsonl text so file-level prefer-record logic applies
        text = "\n".join(json.dumps(e, ensure_ascii=False) for e in stripped)
        return parse_rollout_text(text)

    mapped: list[dict[str, Any]] = []
    for e in stripped:
        etype = str(e.get("type") or "").lower()
        if has_record and etype == "event_msg":
            # Prefer records globally in this batch
            continue
        row = map_codex_event(e)
        if row:
            mapped.append(row)
    return _dedupe_by_request_id(mapped)


def extract_codex_payload(payload: Any) -> list[dict[str, Any]]:
    if payload is None:
        return []
    if isinstance(payload, list):
        return [x for x in payload if isinstance(x, dict)]
    if isinstance(payload, dict):
        for key in ("events", "data", "items", "records", "lines", "rollout"):
            if key in payload and isinstance(payload[key], list):
                return [x for x in payload[key] if isinstance(x, dict)]
        return [payload]
    return []


def scan_codex_dir(root: Optional[str | Path] = None) -> list[dict[str, Any]]:
    """Scan ~/.codex/sessions (or CODEX_SESSIONS_DIR) for rollout-*.jsonl."""
    if root is None:
        root = os.environ.get("CODEX_SESSIONS_DIR") or str(
            Path.home() / ".codex" / "sessions"
        )
    root_path = Path(root).expanduser()
    if not root_path.is_dir():
        return []

    rows: list[dict[str, Any]] = []
    # Prefer plain .jsonl; skip .jsonl.zst for MVP (no zstd dependency)
    patterns = ("rollout-*.jsonl", "**/rollout-*.jsonl")
    seen_paths: set[Path] = set()
    for pattern in patterns:
        for path in sorted(root_path.glob(pattern)):
            if path in seen_paths:
                continue
            if path.name.endswith(".zst"):
                continue
            seen_paths.add(path)
            try:
                text = path.read_text(encoding="utf-8", errors="replace")
            except OSError:
                continue
            rows.extend(parse_rollout_text(text))
    # Also rglob in case glob didn't recurse on some platforms
    for path in sorted(root_path.rglob("rollout-*.jsonl")):
        if path in seen_paths or path.name.endswith(".zst"):
            continue
        seen_paths.add(path)
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        rows.extend(parse_rollout_text(text))

    # Cross-file dedupe: resume writes a new rollout replaying earlier response_ids
    return _dedupe_by_request_id(rows)
