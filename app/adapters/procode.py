"""ProCode / Canoryn usage adapter.

Canoryn's public docs describe conversations and memory as encrypted SQLite,
API keys in the macOS Keychain, and a ``canoryn`` helper that talks to the
running app (and may launch it). There is no documented local usage API.

This module does not decrypt those stores, does not open SQLite, does not read
the keychain, and does not launch ``canoryn``. It accepts unified usage events
(tool forced to procode) from json/jsonl dropped under known directories, and
from ``POST /api/v1/ingest/procode``. Health stays ``placeholder`` until a
public usage schema exists.
"""
from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
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


def _pick(raw: dict[str, Any], usage: dict[str, Any], *keys: str) -> Any:
    """First present value. Zero is present; missing keys are not."""
    for source in (raw, usage):
        if not isinstance(source, dict):
            continue
        for key in keys:
            if key in source and source[key] not in (None, ""):
                return source[key]
    return None


def extract_procode_payload(payload: Any) -> list[dict[str, Any]]:
    if payload is None:
        return []
    if isinstance(payload, list):
        return [x for x in payload if isinstance(x, dict)]
    if isinstance(payload, dict):
        for key in ("events", "data", "items", "records", "usage", "usageEvents", "usage_events"):
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
    model = _as_str(_pick(raw, usage, "model", "model_name", "modelName", "model_id"))
    if not model:
        return None

    prompt = _as_int(_pick(raw, usage, "prompt_tokens", "input_tokens", "promptTokens", "inputTokens", "input"))
    completion = _as_int(
        _pick(
            raw,
            usage,
            "completion_tokens",
            "output_tokens",
            "completionTokens",
            "outputTokens",
            "output",
        )
    )
    cache_read = _as_int(
        _pick(raw, usage, "cache_read_tokens", "cacheReadTokens", "cache_read", "cacheRead")
    )
    cache_write = _as_int(
        _pick(raw, usage, "cache_write_tokens", "cacheWriteTokens", "cache_write", "cacheWrite")
    )
    reasoning = _as_int(_pick(raw, usage, "reasoning_tokens", "reasoningTokens", "reasoning"))
    total = _as_int(_pick(raw, usage, "total_tokens", "totalTokens"))
    if total == 0:
        total = prompt + completion + cache_read + cache_write + reasoning
    if prompt == 0 and completion == 0 and total == 0:
        return None

    account_id = _as_str(
        _pick(raw, usage, "account_id", "account", "email", "user", "userEmail", "user_email")
    )
    participant = _as_str(
        _pick(raw, usage, "participant") or account_id or raw.get("email") or "anonymous"
    )
    project_id = _directory_name(
        _pick(
            raw,
            usage,
            "project_id",
            "project",
            "projectId",
            "projectPath",
            "cwd",
            "workspace",
        )
    )
    team = _as_str(_pick(raw, usage, "team", "team_id", "teamId"))
    agent_id = _as_str(
        _pick(raw, usage, "agent_id", "agentId", "bot_id", "automationId", "cloudAgentId")
    )
    agent_name = _as_str(_pick(raw, usage, "agent_name", "agentName"))
    ts = _parse_ts(_pick(raw, usage, "timestamp", "ts", "created_at", "createdAt"))
    request_id = _as_str(
        _pick(raw, usage, "request_id", "requestId", "id", "event_id", "eventId"),
        max_len=256,
    )
    if not request_id:
        request_id = _as_str(
            f"procode:{account_id or participant}:{ts}:{model}:{project_id or ''}",
            max_len=256,
        )

    cost_usd = None
    cost_basis = None
    cost_raw = _pick(raw, usage, "cost_usd", "costUsd")
    if cost_raw is not None:
        try:
            cost_usd = float(cost_raw)
            cost_basis = _as_str(_pick(raw, usage, "cost_basis", "costBasis")) or "procode_reported"
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


# Relative to a home directory. Linux and Windows entries are operator drop-in
# folders for usage json/jsonl. Canoryn itself is a macOS app; its chat DB is
# not one of these files.
PROCODE_HOME_RELATIVE = (
    ".procode",
    ".canoryn",
    ".config/procode",
    ".config/canoryn",
    ".config/ProCode",
    ".config/Canoryn",
    ".local/share/procode",
    ".local/share/canoryn",
    ".local/share/ProCode",
    ".local/share/Canoryn",
    "Library/Application Support/ProCode",
    "Library/Application Support/Canoryn",
    "Library/Application Support/procode",
    "Library/Application Support/canoryn",
    "AppData/Roaming/ProCode",
    "AppData/Roaming/Canoryn",
    "AppData/Roaming/procode",
    "AppData/Roaming/canoryn",
    "AppData/Local/ProCode",
    "AppData/Local/Canoryn",
    "AppData/Local/procode",
    "AppData/Local/canoryn",
)

BLOCKED_ZH = (
    "不解密聊天库，不打开 sqlite，不读钥匙串，不启动 canoryn。"
    "公开用量 schema 出来之前，适配状态保持 placeholder。"
)
WORKS_TODAY_ZH = (
    "今天只读 usage 形状的 json/jsonl（含 token-usage、usageEvents、驼峰 token 字段），"
    "或 POST /api/v1/ingest/procode。discussions 与 blueprints 不是用量。"
    "Linux / Windows 上的目录是给操作者放导出文件的；Canoryn 应用本身在 macOS。"
)

ADAPTER_META = {
    "id": "procode",
    "display": "ProCode",
    "status": "placeholder",
    "aliases": ["procode", "canoryn", "pro code"],
    "collects": "usage_json",
    "blocked": ["encrypted_sqlite", "keychain", "canoryn_cli"],
    "notes": (
        "状态仍是 placeholder。Canoryn 没有公开的本机用量接口："
        "聊天和记忆在加密 SQLite，本适配器不解密、不打开数据库、不读钥匙串、不启动 canoryn。"
        "今天只收 usage json/jsonl（~/.procode、~/.canoryn、各系统应用数据目录，或 PROCODE_DATA_DIR），"
        "以及 POST /api/v1/ingest/procode。蓝图和 discussions 不入库。"
        "未知模型保留原名，估价用 default 档。"
    ),
}

_MAX_USAGE_BYTES = 32 * 1024 * 1024
_IGNORE_PARTS = frozenset(
    {
        "node_modules",
        "cache",
        "code cache",
        "gpucache",
        "blob_storage",
        "shadercache",
    }
)
_SECRET_PARTS = frozenset({"keychain", "keychains", "cookies"})
_SKIP_FILE_NAMES = frozenset({"readme.json", "package.json", "package-lock.json"})
_CREDENTIAL_PARTS = (
    "secret",
    "cookie",
    "credential",
    "password",
    "keychain",
    "apikey",
    "api_key",
    "api-key",
    "access_token",
    "access-token",
    "refresh_token",
    "refresh-token",
    "id_token",
    "id-token",
)
_DB_SUFFIXES = frozenset({".sqlite", ".sqlite3", ".db", ".cryn", ".crynpack"})
_GENERIC_PARENTS = frozenset({"usage", "logs", "exports", "events", "data"})
_DIAGNOSTICS: dict[str, "_Counts"] = {}


def _has_part(path: Path, names: frozenset[str]) -> bool:
    return any(part.lower() in names for part in path.parts)


def _credential_file_name(name: str) -> bool:
    lowered = name.lower()
    if lowered in {"auth.json", "auth.jsonl", "credentials.json", "credentials.jsonl"}:
        return True
    return any(part in lowered for part in _CREDENTIAL_PARTS)


@dataclass
class _Counts:
    files_read: int = 0
    files_skipped_credential_name: int = 0
    files_skipped_oversize: int = 0
    unopened_db_seen: int = 0
    unopened_db_basenames: list[str] = field(default_factory=list)
    discussions_skipped: int = 0
    blueprints_skipped: int = 0
    parsed_rows: int = 0
    parse: ParseStats = field(default_factory=ParseStats)

    def merge(self, other: "_Counts") -> "_Counts":
        self.files_read += other.files_read
        self.files_skipped_credential_name += other.files_skipped_credential_name
        self.files_skipped_oversize += other.files_skipped_oversize
        self.unopened_db_seen += other.unopened_db_seen
        for name in other.unopened_db_basenames:
            if name not in self.unopened_db_basenames and len(self.unopened_db_basenames) < 8:
                self.unopened_db_basenames.append(name)
        self.discussions_skipped += other.discussions_skipped
        self.blueprints_skipped += other.blueprints_skipped
        self.parsed_rows += other.parsed_rows
        self.parse.merge(other.parse)
        return self


def _note_zh(counts: _Counts, *, roots_found: bool) -> str:
    if not roots_found:
        return "没有找到 ProCode/Canoryn 目录。" + WORKS_TODAY_ZH + BLOCKED_ZH
    parts: list[str] = []
    if counts.parsed_rows:
        parts.append(f"从 usage json/jsonl 读到 {counts.parsed_rows} 行。")
    elif counts.files_read:
        parts.append("读了 usage 文件，但没有可入库的行（缺模型、缺 token，或含大段对话）。")
    elif counts.unopened_db_seen:
        parts.append(f"看到 {counts.unopened_db_seen} 个数据库或打包文件，没有打开。")
        parts.append("目录里还没有可解析的 usage json/jsonl。")
    else:
        parts.append("目录里还没有可解析的 usage json/jsonl。")
    if counts.discussions_skipped or counts.blueprints_skipped:
        parts.append("discussions 与 blueprints 已跳过，那些不是用量。")
    parts.append(BLOCKED_ZH)
    return "".join(parts)


def _public_diagnostics(counts: _Counts, *, roots_found: bool) -> dict[str, Any]:
    return {
        "adapter_status": "placeholder",
        "collects": "usage_json",
        "blocked": ["encrypted_sqlite", "keychain", "canoryn_cli"],
        "blocked_zh": BLOCKED_ZH,
        "works_today_zh": WORKS_TODAY_ZH,
        "note_zh": _note_zh(counts, roots_found=roots_found),
        "files_read": counts.files_read,
        "files_skipped_credential_name": counts.files_skipped_credential_name,
        "files_skipped_oversize": counts.files_skipped_oversize,
        "unopened_db_seen": counts.unopened_db_seen,
        "unopened_db_basenames": list(counts.unopened_db_basenames),
        "discussions_skipped": counts.discussions_skipped,
        "blueprints_skipped": counts.blueprints_skipped,
        "parsed_rows": counts.parsed_rows,
        "parse": counts.parse.to_dict(),
    }


def empty_procode_diagnostics() -> dict[str, Any]:
    """Honest placeholder when no ProCode/Canoryn directory was found."""
    return _public_diagnostics(_Counts(), roots_found=False)


def diagnostics_for_roots(roots: list[str]) -> dict[str, Any]:
    """Aggregate diagnostics recorded by the latest ``scan_procode_dir`` calls."""
    merged = _Counts()
    found = False
    for root in roots:
        key = str(Path(root).expanduser())
        item = _DIAGNOSTICS.get(key)
        if item is None:
            continue
        found = True
        merged.merge(item)
    if not found and not roots:
        return empty_procode_diagnostics()
    return _public_diagnostics(merged, roots_found=bool(roots))


def _project_hint(root: Path, path: Path) -> Optional[str]:
    parent = path.parent
    if parent == root:
        return None
    name = parent.name
    if name.lower() in _GENERIC_PARENTS:
        upper = parent.parent
        if upper != root and upper.name.lower() not in _GENERIC_PARENTS:
            return _as_str(upper.name)
        return None
    return _as_str(name)


def _events_in_text(text: str, *, jsonl: bool) -> tuple[list[dict[str, Any]], int]:
    if jsonl:
        events: list[dict[str, Any]] = []
        bad = 0
        for line in text.splitlines():
            raw_line = line.strip()
            if not raw_line or raw_line.startswith("#"):
                continue
            try:
                obj = json.loads(raw_line)
            except json.JSONDecodeError:
                bad += 1
                continue
            events.extend(extract_procode_payload(obj))
        return events, bad
    try:
        payload = json.loads(text)
    except json.JSONDecodeError:
        return [], 1
    return extract_procode_payload(payload), 0


def _dedupe_procode(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
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


def _note_db(counts: _Counts, path: Path) -> None:
    counts.unopened_db_seen += 1
    name = path.name
    if name not in counts.unopened_db_basenames and len(counts.unopened_db_basenames) < 8:
        counts.unopened_db_basenames.append(name)


def scan_procode_dir(root: Optional[str | Path] = None) -> list[dict[str, Any]]:
    """Scan one ProCode/Canoryn root for usage json/jsonl.

    SQLite (including encrypted ``canoryn.sqlite``), ``.db``, and ``.cryn``
    packages are counted and not opened. ``PROCODE_DATA_DIR`` is the default
    when ``root`` is omitted. Diagnostics from this call are kept for
    ``diagnostics_for_roots``.
    """
    if root is None:
        root = os.environ.get("PROCODE_DATA_DIR") or str(Path.home() / ".procode")
    root_path = Path(root).expanduser()
    counts = _Counts()
    _DIAGNOSTICS[str(root_path)] = counts
    if not root_path.is_dir():
        return []

    rows: list[dict[str, Any]] = []
    files = sorted(path for path in root_path.rglob("*") if path.is_file())
    for path in files:
        if _has_part(path, _IGNORE_PARTS):
            continue
        if _has_part(path, _SECRET_PARTS):
            counts.files_skipped_credential_name += 1
            continue
        suffix = path.suffix.lower()
        if suffix in _DB_SUFFIXES:
            _note_db(counts, path)
            continue
        parts = {part.lower() for part in path.parts}
        if "discussions" in parts:
            counts.discussions_skipped += 1
            continue
        if "blueprints" in parts:
            counts.blueprints_skipped += 1
            continue
        if suffix not in {".json", ".jsonl"}:
            continue
        if path.name.lower() in _SKIP_FILE_NAMES:
            continue
        if _credential_file_name(path.name):
            counts.files_skipped_credential_name += 1
            continue
        try:
            size = path.stat().st_size
        except OSError:
            continue
        if size <= 0:
            continue
        if size > _MAX_USAGE_BYTES:
            counts.files_skipped_oversize += 1
            continue
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        counts.files_read += 1
        events, bad = _events_in_text(text, jsonl=suffix == ".jsonl")
        file_stats = ParseStats(files_scanned=1, skipped_json_error=bad)
        mapped, part = map_procode_events_with_stats(events)
        file_stats.merge(part)
        counts.parse.merge(file_stats)
        hint = _project_hint(root_path, path)
        if hint:
            for row in mapped:
                if not row.get("project_id"):
                    row["project_id"] = hint
        rows.extend(mapped)
    rows = _dedupe_procode(rows)
    counts.parsed_rows = len(rows)
    return rows
