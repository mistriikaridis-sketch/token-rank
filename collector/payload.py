"""Turn adapter rows into the ingest bodies the server already accepts.

Only whitelisted usage fields are copied. Prompts, completions, tool
arguments, and source paths are not reconstructed.
"""
from __future__ import annotations

from pathlib import Path
from typing import Any

from app.pricing import estimate_cost, normalize_model_name

from collector.sources import ToolScan

# Keys that may appear anywhere in an upload body.
ALLOWED_KEYS = frozenset(
    {
        "type",
        "eventName",
        "model",
        "prompt_tokens",
        "completion_tokens",
        "cache_read_tokens",
        "cache_write_tokens",
        "reasoning_tokens",
        "total_tokens",
        "project_id",
        "account_id",
        "participant",
        "team",
        "timestamp",
        "request_id",
        "agent_id",
        "agent_name",
        "message",
        "id",
        "usage",
        "input_tokens",
        "output_tokens",
        "cache_read_input_tokens",
        "cache_creation_input_tokens",
        "response_id",
        "cached_input_tokens",
        "reasoning_output_tokens",
        "cache_write_input_tokens",
        "promptTokens",
        "generatedTokens",
        "userId",
        "provider",
        "chargedCents",
    }
)

COST_NOTE_ZH = (
    "费用是公开列表价估算（cost_basis=estimate），不是发票或云账单实扣。"
    "缓存 Token 会入库，但估算只按 prompt 与 completion 计。"
)

MODEL_SUPPORT_ZH = (
    "日志里的模型名都会入库。国内外主流模型按 pricing.json 的列表价估算；"
    "不认识的名字原样保留，计价用 default 档，事件不会因此被丢掉。"
)

SENT_FIELDS_ZH = [
    "model",
    "prompt_tokens / completion_tokens / total_tokens",
    "cache_read_tokens / cache_write_tokens / reasoning_tokens（适配器有则带上）",
    "account_id / participant / team",
    "project_id（目录名，不是完整路径，也不是源文件名）",
    "agent_id / agent_name（可选）",
    "request_id / timestamp / tool / source",
]


def _model(row: dict[str, Any]) -> str:
    return normalize_model_name(str(row.get("model") or ""))


def _common(row: dict[str, Any]) -> dict[str, Any]:
    body: dict[str, Any] = {
        "model": _model(row),
        "project_id": row.get("project_id"),
        "account_id": row.get("account_id"),
        "participant": row.get("participant"),
        "team": row.get("team"),
        "timestamp": row.get("ts"),
        "request_id": row.get("request_id"),
    }
    if row.get("agent_id"):
        body["agent_id"] = row.get("agent_id")
    if row.get("agent_name"):
        body["agent_name"] = row.get("agent_name")
    return body


def event_for_workbuddy(row: dict[str, Any]) -> dict[str, Any]:
    body = _common(row)
    body.update(
        {
            "type": "llm.usage",
            "prompt_tokens": int(row.get("prompt_tokens") or 0),
            "completion_tokens": int(row.get("completion_tokens") or 0),
            "cache_read_tokens": int(row.get("cache_read_tokens") or 0),
            "cache_write_tokens": int(row.get("cache_write_tokens") or 0),
            "reasoning_tokens": int(row.get("reasoning_tokens") or 0),
            "total_tokens": int(row.get("total_tokens") or 0),
        }
    )
    return body


def event_for_claude_code(row: dict[str, Any]) -> dict[str, Any]:
    common = _common(row)
    return {
        "type": "assistant",
        "timestamp": common["timestamp"],
        "account_id": common["account_id"],
        "participant": common["participant"],
        "team": common["team"],
        "project_id": common["project_id"],
        "message": {
            "id": common["request_id"],
            "model": common["model"],
            "usage": {
                "input_tokens": int(row.get("prompt_tokens") or 0),
                "output_tokens": int(row.get("completion_tokens") or 0),
                "cache_read_input_tokens": int(row.get("cache_read_tokens") or 0),
                "cache_creation_input_tokens": int(row.get("cache_write_tokens") or 0),
            },
        },
    }


def event_for_codex(row: dict[str, Any]) -> dict[str, Any]:
    prompt = int(row.get("prompt_tokens") or 0)
    cached = int(row.get("cache_read_tokens") or 0)
    common = _common(row)
    return {
        "model": common["model"],
        "project_id": common["project_id"],
        "account_id": common["account_id"],
        "participant": common["participant"],
        "team": common["team"],
        "response_id": common["request_id"],
        "timestamp": common["timestamp"],
        "usage": {
            "input_tokens": prompt + cached,
            "cached_input_tokens": cached,
            "output_tokens": int(row.get("completion_tokens") or 0),
            "reasoning_output_tokens": int(row.get("reasoning_tokens") or 0),
            "cache_write_input_tokens": int(row.get("cache_write_tokens") or 0),
            "total_tokens": int(row.get("total_tokens") or 0),
        },
    }


def _provider_from_tag(tag: Any) -> str | None:
    if not isinstance(tag, str) or not tag.startswith("provider:"):
        return None
    provider = tag.split(",", 1)[0].removeprefix("provider:").strip()
    if not provider or len(provider) > 64:
        return None
    return provider


def event_for_continue(row: dict[str, Any]) -> dict[str, Any]:
    common = _common(row)
    body: dict[str, Any] = {
        "eventName": "tokensGenerated",
        "timestamp": common["timestamp"],
        "model": common["model"],
        "promptTokens": int(row.get("prompt_tokens") or 0),
        "generatedTokens": int(row.get("completion_tokens") or 0),
        "account_id": common["account_id"],
        "userId": common["account_id"],
        "team": common["team"],
        "participant": common["participant"],
        "project_id": common["project_id"],
        "request_id": common["request_id"],
    }
    if common.get("agent_id"):
        body["agent_id"] = common["agent_id"]
    if common.get("agent_name"):
        body["agent_name"] = common["agent_name"]
    provider = _provider_from_tag(row.get("tag"))
    if provider:
        body["provider"] = provider
    return body


def event_for_cursor(row: dict[str, Any]) -> dict[str, Any]:
    body = _common(row)
    body.update(
        {
            "prompt_tokens": int(row.get("prompt_tokens") or 0),
            "completion_tokens": int(row.get("completion_tokens") or 0),
            "cache_read_tokens": int(row.get("cache_read_tokens") or 0),
            "cache_write_tokens": int(row.get("cache_write_tokens") or 0),
            "reasoning_tokens": int(row.get("reasoning_tokens") or 0),
            "total_tokens": int(row.get("total_tokens") or 0),
        }
    )
    if row.get("cost_basis") == "cursor_charged" and row.get("cost_usd") is not None:
        try:
            body["chargedCents"] = round(float(row["cost_usd"]) * 100.0, 6)
        except (TypeError, ValueError):
            pass
    return body


def event_for_procode(row: dict[str, Any]) -> dict[str, Any]:
    body = _common(row)
    body.update(
        {
            "prompt_tokens": int(row.get("prompt_tokens") or 0),
            "completion_tokens": int(row.get("completion_tokens") or 0),
            "cache_read_tokens": int(row.get("cache_read_tokens") or 0),
            "cache_write_tokens": int(row.get("cache_write_tokens") or 0),
            "reasoning_tokens": int(row.get("reasoning_tokens") or 0),
            "total_tokens": int(row.get("total_tokens") or 0),
        }
    )
    return body


_BUILDERS = {
    "workbuddy": event_for_workbuddy,
    "claude_code": event_for_claude_code,
    "codex": event_for_codex,
    "continue": event_for_continue,
    "cursor": event_for_cursor,
    "procode": event_for_procode,
}


def events_for_tool(tool: str, rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    build = _BUILDERS[tool]
    return [build(row) for row in rows]


def iter_keys(value: Any) -> list[str]:
    found: list[str] = []
    if isinstance(value, dict):
        for key, item in value.items():
            found.append(str(key))
            found.extend(iter_keys(item))
    elif isinstance(value, list):
        for item in value:
            found.extend(iter_keys(item))
    return found


def assert_allowed(payload: Any) -> None:
    unknown = sorted({k for k in iter_keys(payload) if k not in ALLOWED_KEYS})
    if unknown:
        raise ValueError("payload has keys outside the usage allowlist: " + ", ".join(unknown))


def redact_home(path: str) -> str:
    """Show a scan root without the absolute home directory."""
    if not path:
        return path
    try:
        home = Path.home()
    except RuntimeError:
        return path
    candidate = Path(path).expanduser()
    try:
        rel = candidate.relative_to(home)
    except ValueError:
        return path
    if not rel.parts:
        return "~"
    return "~/" + rel.as_posix()


def build_payloads(report: dict[str, ToolScan]) -> dict[str, list[dict[str, Any]]]:
    payloads: dict[str, list[dict[str, Any]]] = {}
    for tool, scan in report.items():
        events = events_for_tool(tool, scan.rows)
        assert_allowed(events)
        payloads[tool] = events
    return payloads


def summarize(report: dict[str, ToolScan], payloads: dict[str, list[dict[str, Any]]]) -> dict[str, Any]:
    tools: dict[str, Any] = {}
    totals = {
        "rows": 0,
        "prompt_tokens": 0,
        "completion_tokens": 0,
        "total_tokens": 0,
        "estimated_cost_usd": 0.0,
    }
    for tool, scan in report.items():
        prompt = sum(int(r.get("prompt_tokens") or 0) for r in scan.rows)
        completion = sum(int(r.get("completion_tokens") or 0) for r in scan.rows)
        total = sum(int(r.get("total_tokens") or 0) for r in scan.rows)
        cost = sum(
            estimate_cost(
                str(r.get("model") or ""),
                int(r.get("prompt_tokens") or 0),
                int(r.get("completion_tokens") or 0),
            )
            for r in scan.rows
        )
        tools[tool] = {
            "roots": [redact_home(root) for root in scan.roots],
            "missing_roots": [redact_home(root) for root in scan.missing_roots],
            "rows": len(scan.rows),
            "prompt_tokens": prompt,
            "completion_tokens": completion,
            "total_tokens": total,
            "estimated_cost_usd": round(cost, 6),
            "events": payloads.get(tool, []),
        }
        totals["rows"] += len(scan.rows)
        totals["prompt_tokens"] += prompt
        totals["completion_tokens"] += completion
        totals["total_tokens"] += total
        totals["estimated_cost_usd"] += cost
    totals["estimated_cost_usd"] = round(totals["estimated_cost_usd"], 6)
    return {
        "tools": tools,
        "totals": {
            **totals,
            "cost_basis": "estimate",
            "cost_note_zh": COST_NOTE_ZH,
        },
        "privacy": {
            "uploads_prompts": False,
            "uploads_chat": False,
            "uploads_source_code": False,
            "uploads_filenames": False,
            "sent_fields_zh": SENT_FIELDS_ZH,
            "model_support_zh": MODEL_SUPPORT_ZH,
        },
    }
