"""Continue.dev tokensGenerated adapter — usage-only.

Supports:
  1) HTTP destination envelope from config.yaml data[]:
       { "name": "tokensGenerated", "data": {...}, "schema": "0.2.0", "level": "...", "profileId": "..." }
  2) Flat local jsonl lines under ~/.continue/dev_data/**/tokensGenerated.jsonl
       { eventName, schema, timestamp, userId, model, provider, promptTokens, generatedTokens, ... }
  3) Minimal / pre-normalized push with promptTokens+generatedTokens (or snake_case).

Never stores prompt / completion / code bodies. tool=continue; source=continue.
"""
from __future__ import annotations

import hashlib
import json
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

from app.tools import normalize_tool

_FORBIDDEN_KEYS = frozenset(
    {
        "content",
        "messages",
        "prompt",
        "completion",
        "response",
        "text",
        "delta",
        "choices",
        "system",
        "transcript",
        "body",
        "thinking",
        "prefix",
        "suffix",
        "filepath",
        "fileContents",
        "file_contents",
        "code",
        "diff",
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


def _strip_content(obj: Any) -> Any:
    """Drop prompt/code bodies (defense in depth for push path)."""
    if isinstance(obj, dict):
        out: dict[str, Any] = {}
        for k, v in obj.items():
            low = str(k).lower()
            if low in _FORBIDDEN_KEYS:
                continue
            if isinstance(v, (dict, list)):
                out[k] = _strip_content(v)
            elif isinstance(v, (str, int, float, bool)) or v is None:
                if isinstance(v, str) and len(v) > 512 and low not in (
                    "model",
                    "provider",
                    "userId",
                    "userid",
                    "user_id",
                    "timestamp",
                    "eventname",
                    "event_name",
                    "schema",
                    "selectedprofileid",
                    "selected_profile_id",
                    "profileid",
                    "profile_id",
                    "useragent",
                    "user_agent",
                    "account_id",
                    "project_id",
                    "agent_id",
                    "agent_name",
                    "request_id",
                    "name",
                    "level",
                ):
                    continue
                out[k] = v
        return out
    if isinstance(obj, list):
        return [_strip_content(x) for x in obj]
    return obj


def _unwrap_envelope(raw: dict[str, Any]) -> dict[str, Any]:
    """Flatten Continue HTTP destination envelope into event fields.

    Continue POSTs:
      { name, data: <parsed event>, schema, level, profileId }
    Local jsonl already has flat fields (+ optional eventName).
    """
    data = raw.get("data") if isinstance(raw.get("data"), dict) else None
    name = _as_str(raw.get("name") or raw.get("eventName") or raw.get("event_name"))

    if data is not None and (
        name == "tokensGenerated"
        or "promptTokens" in data
        or "generatedTokens" in data
        or "prompt_tokens" in data
        or "generated_tokens" in data
        or str(data.get("eventName") or "") == "tokensGenerated"
    ):
        merged = dict(data)
        # Envelope metadata as fallbacks
        if "eventName" not in merged and name:
            merged["eventName"] = name
        if "schema" not in merged and raw.get("schema") is not None:
            merged["schema"] = raw.get("schema")
        if "selectedProfileId" not in merged and raw.get("profileId"):
            merged["selectedProfileId"] = raw.get("profileId")
        # Allow callers to pass account/project/agent on the envelope
        for k in (
            "account_id",
            "account",
            "email",
            "team",
            "team_id",
            "participant",
            "user",
            "project_id",
            "project",
            "agent_id",
            "agent_name",
            "request_id",
        ):
            if k not in merged and k in raw:
                merged[k] = raw[k]
        return merged
    return raw


def _is_tokens_generated(raw: dict[str, Any]) -> bool:
    name = str(
        raw.get("eventName") or raw.get("event_name") or raw.get("name") or ""
    ).strip()
    if name and name not in ("tokensGenerated", "tokens_generated", "continue", ""):
        # Explicit other event types — skip
        if name in (
            "autocomplete",
            "chatInteraction",
            "editInteraction",
            "editOutcome",
            "nextEditOutcome",
            "chatFeedback",
            "toolUsage",
            "quickEdit",
        ):
            return False
    # Must have model + some token signal
    has_tokens = any(
        k in raw
        for k in (
            "promptTokens",
            "generatedTokens",
            "prompt_tokens",
            "generated_tokens",
            "completion_tokens",
            "completionTokens",
        )
    )
    return bool(raw.get("model")) and has_tokens


def _synthetic_request_id(
    *,
    ts: str,
    account_id: Optional[str],
    model: str,
    provider: Optional[str],
    prompt: int,
    completion: int,
    profile: Optional[str],
) -> str:
    basis = (
        f"{ts}|{account_id or ''}|{model}|{provider or ''}|"
        f"{prompt}|{completion}|{profile or ''}"
    )
    digest = hashlib.sha256(basis.encode("utf-8")).hexdigest()[:20]
    return f"continue:{digest}"


def map_continue_event(raw: dict[str, Any]) -> Optional[dict[str, Any]]:
    """Map one Continue tokensGenerated event / envelope → usage row."""
    if not isinstance(raw, dict):
        return None
    flat = _unwrap_envelope(raw)
    if not _is_tokens_generated(flat):
        return None

    model = _as_str(flat.get("model"))
    if not model:
        return None

    prompt = _as_int(
        flat.get(
            "promptTokens",
            flat.get("prompt_tokens", flat.get("input_tokens", flat.get("inputTokens"))),
        )
    )
    completion = _as_int(
        flat.get(
            "generatedTokens",
            flat.get(
                "generated_tokens",
                flat.get("completion_tokens", flat.get("completionTokens")),
            ),
        )
    )
    if prompt + completion <= 0:
        return None

    provider = _as_str(flat.get("provider") or flat.get("providerName"))
    ts = _parse_ts(flat.get("timestamp") or flat.get("ts") or flat.get("created_at"))

    account_id = _as_str(
        flat.get("account_id")
        or flat.get("account")
        or flat.get("email")
        or flat.get("userId")
        or flat.get("user_id")
        or os.environ.get("CONTINUE_ACCOUNT_ID")
    )
    participant = _as_str(
        flat.get("participant")
        or flat.get("user")
        or account_id
        or "continue-user"
    )
    team = _as_str(flat.get("team") or flat.get("team_id"))

    profile = _as_str(
        flat.get("selectedProfileId")
        or flat.get("selected_profile_id")
        or flat.get("profileId")
        or flat.get("profile_id")
    )
    project_id = _as_str(
        flat.get("project_id") or flat.get("project") or profile
    )

    agent_id = _as_str(
        flat.get("agent_id")
        or flat.get("bot_id")
        or flat.get("automationId")
        or flat.get("cloudAgentId")
    )
    agent_name = _as_str(flat.get("agent_name") or flat.get("agentName"))

    request_id = _as_str(
        flat.get("request_id") or flat.get("id") or flat.get("eventId"),
        max_len=256,
    )
    if not request_id:
        request_id = _synthetic_request_id(
            ts=ts,
            account_id=account_id,
            model=model,
            provider=provider,
            prompt=prompt,
            completion=completion,
            profile=profile,
        )

    tag_parts = []
    if provider:
        tag_parts.append(f"provider:{provider}")
    if profile:
        tag_parts.append(f"profile:{profile[:24]}")
    schema_ver = _as_str(flat.get("schema"), max_len=16)
    if schema_ver:
        tag_parts.append(f"schema:{schema_ver}")
    tag = ",".join(tag_parts)[:128] if tag_parts else None

    total = prompt + completion

    return {
        "model": model,
        "prompt_tokens": prompt,
        "completion_tokens": completion,
        "total_tokens": total,
        "cache_read_tokens": 0,
        "cache_write_tokens": 0,
        "reasoning_tokens": 0,
        "team": team,
        "api_key": None,
        "tag": tag,
        "participant": participant,
        "tool": normalize_tool("continue"),
        "account_id": account_id,
        "project_id": project_id,
        "agent_id": agent_id,
        "agent_name": agent_name,
        "request_id": request_id,
        "source": "continue",
        "cost_usd": None,
        "cost_basis": None,
        "ts": ts,
    }


def map_continue_events(events: list[dict[str, Any]]) -> list[dict[str, Any]]:
    mapped: list[dict[str, Any]] = []
    seen: set[str] = set()
    for raw in events:
        safe = _strip_content(raw) if isinstance(raw, dict) else raw
        if not isinstance(safe, dict):
            continue
        row = map_continue_event(safe)
        if not row:
            continue
        rid = row.get("request_id")
        if rid:
            if rid in seen:
                continue
            seen.add(str(rid))
        mapped.append(row)
    return mapped


def extract_continue_payload(payload: Any) -> list[dict[str, Any]]:
    """Normalize HTTP body into a list of event dicts.

    Accepts:
      - Continue native: { name, data, schema, level, profileId }
      - { events|data|items|records: [...] }
      - bare list
      - single flat event
    """
    if payload is None:
        return []
    if isinstance(payload, list):
        return [x for x in payload if isinstance(x, dict)]
    if isinstance(payload, dict):
        # Native HTTP destination envelope (single event)
        if (
            isinstance(payload.get("data"), dict)
            and (
                payload.get("name") == "tokensGenerated"
                or "promptTokens" in payload["data"]
                or "generatedTokens" in payload["data"]
            )
        ):
            return [payload]
        for key in ("events", "items", "records", "lines"):
            if key in payload and isinstance(payload[key], list):
                return [x for x in payload[key] if isinstance(x, dict)]
        # { data: [ ... ] } batch without name
        if isinstance(payload.get("data"), list):
            return [x for x in payload["data"] if isinstance(x, dict)]
        return [payload]
    return []


def parse_jsonl_text(text: str) -> list[dict[str, Any]]:
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
    return map_continue_events(events)


def scan_continue_dir(root: Optional[str | Path] = None) -> list[dict[str, Any]]:
    """Scan ~/.continue/dev_data (or CONTINUE_DATA_DIR) for **/tokensGenerated.jsonl."""
    if root is None:
        root = os.environ.get("CONTINUE_DATA_DIR") or str(
            Path.home() / ".continue" / "dev_data"
        )
    root_path = Path(root).expanduser()
    if not root_path.is_dir():
        return []

    rows: list[dict[str, Any]] = []
    # Default layout: {version}/tokensGenerated.jsonl ; also accept nested copies
    for path in sorted(root_path.rglob("tokensGenerated.jsonl")):
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        rows.extend(parse_jsonl_text(text))
    # Also accept tokens_generated.jsonl (snake) if present
    for path in sorted(root_path.rglob("tokens_generated.jsonl")):
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        rows.extend(parse_jsonl_text(text))

    # Cross-file dedupe by request_id
    by_id: dict[str, dict[str, Any]] = {}
    passthrough: list[dict[str, Any]] = []
    for r in rows:
        rid = r.get("request_id")
        if not rid:
            passthrough.append(r)
            continue
        if str(rid) not in by_id:
            by_id[str(rid)] = r
    return list(by_id.values()) + passthrough
