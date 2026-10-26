"""Discover local log roots and scan them with the existing adapters."""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Optional

from app.adapters.claude_code import scan_claude_code_dir
from app.adapters.codex import scan_codex_dir
from app.adapters.continue_dev import scan_continue_dir
from app.adapters.workbuddy import scan_workbuddy_dir
from app.tools import normalize_tool

from collector.config import SUPPORTED_TOOLS, CollectorConfig, repo_root

ScanFn = Callable[[Path], list[dict[str, Any]]]

TOOL_SCANNERS: dict[str, ScanFn] = {
    "workbuddy": scan_workbuddy_dir,
    "claude_code": scan_claude_code_dir,
    "codex": scan_codex_dir,
    "continue": scan_continue_dir,
}

ENV_ROOT = {
    "workbuddy": "WORKBUDDY_PROJECTS_DIR",
    "claude_code": "CLAUDE_PROJECTS_DIR",
    "codex": "CODEX_SESSIONS_DIR",
    "continue": "CONTINUE_DATA_DIR",
}

# Workbody is the spoken alias already used by scripts/probe_local_tools.py.
DEFAULT_RELATIVE = {
    "workbuddy": (
        ".workbuddy/projects",
        ".workbody/projects",
        "Library/Application Support/WorkBuddy/projects",
    ),
    "claude_code": (".claude/projects",),
    "codex": (".codex/sessions",),
    "continue": (".continue/dev_data",),
}

FIXTURE_RELATIVE = {
    "workbuddy": "fixtures/workbuddy_sample",
    "claude_code": "fixtures/claude_code_sample",
    "codex": "fixtures/codex_sample",
    "continue": "fixtures/continue_sample",
}

_GENERIC_PARTICIPANTS = {
    "",
    "anonymous",
    "claude-code-user",
    "codex-user",
    "continue-user",
}


@dataclass
class ToolScan:
    tool: str
    roots: list[str] = field(default_factory=list)
    missing_roots: list[str] = field(default_factory=list)
    rows: list[dict[str, Any]] = field(default_factory=list)

    @property
    def found(self) -> bool:
        return bool(self.roots)


def canonical_tool(name: str) -> Optional[str]:
    slug = normalize_tool(name)
    if slug in SUPPORTED_TOOLS:
        return slug
    return None


def _dedupe_paths(paths: list[Path]) -> list[Path]:
    seen: set[str] = set()
    out: list[Path] = []
    for path in paths:
        key = str(path.expanduser())
        if key in seen:
            continue
        seen.add(key)
        out.append(path.expanduser())
    return out


def resolve_roots(
    tool: str,
    cfg: CollectorConfig,
    *,
    fixtures: bool = False,
    home: Optional[Path] = None,
) -> tuple[list[Path], list[Path]]:
    """Return (existing dirs, missing explicit dirs)."""
    if fixtures:
        path = repo_root() / FIXTURE_RELATIVE[tool]
        if path.is_dir():
            return [path], []
        return [], [path]

    explicit: list[Path] = []
    for raw in cfg.roots.get(tool) or []:
        explicit.append(Path(raw).expanduser())
    env_name = ENV_ROOT[tool]
    env_val = (os.environ.get(env_name) or "").strip()
    if env_val:
        explicit.append(Path(env_val).expanduser())
    if explicit:
        unique = _dedupe_paths(explicit)
        found = [p for p in unique if p.is_dir()]
        missing = [p for p in unique if not p.is_dir()]
        return found, missing

    base = home.expanduser() if home is not None else Path.home()
    found = []
    for rel in DEFAULT_RELATIVE[tool]:
        path = base / rel
        if path.is_dir():
            found.append(path)
    return _dedupe_paths(found), []


def _dedupe_rows(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    seen: set[tuple[Any, Any]] = set()
    out: list[dict[str, Any]] = []
    for row in rows:
        key = (row.get("source"), row.get("request_id"))
        if key in seen:
            continue
        seen.add(key)
        out.append(row)
    return out


def apply_identity(row: dict[str, Any], cfg: CollectorConfig) -> dict[str, Any]:
    """Map a log identity onto the company account when configured.

    ``identity_map`` wins per row. A global ``account_id`` fills everyone
    else so a laptop without emails still shows up on the private board.
    """
    out = dict(row)
    mapped = None
    for key in (out.get("account_id"), out.get("participant")):
        if key and key in cfg.identity_map:
            mapped = cfg.identity_map[key]
            break
    if mapped:
        if mapped.get("account_id"):
            out["account_id"] = mapped["account_id"]
        if mapped.get("participant"):
            out["participant"] = mapped["participant"]
        if mapped.get("team"):
            out["team"] = mapped["team"]
        return out
    if cfg.account_id:
        out["account_id"] = cfg.account_id
        participant = out.get("participant") or ""
        if participant in _GENERIC_PARTICIPANTS:
            out["participant"] = cfg.participant or cfg.account_id
    if cfg.participant:
        out["participant"] = cfg.participant
    if cfg.team and not out.get("team"):
        out["team"] = cfg.team
    return out


def scan_tool(
    tool: str,
    cfg: CollectorConfig,
    *,
    fixtures: bool = False,
    home: Optional[Path] = None,
) -> ToolScan:
    found, missing = resolve_roots(tool, cfg, fixtures=fixtures, home=home)
    scanner = TOOL_SCANNERS[tool]
    rows: list[dict[str, Any]] = []
    for root in found:
        rows.extend(scanner(root))
    rows = [apply_identity(row, cfg) for row in _dedupe_rows(rows)]
    return ToolScan(
        tool=tool,
        roots=[str(p) for p in found],
        missing_roots=[str(p) for p in missing],
        rows=rows,
    )


def scan_all(
    cfg: CollectorConfig,
    *,
    tools: Optional[list[str]] = None,
    fixtures: bool = False,
    home: Optional[Path] = None,
) -> dict[str, ToolScan]:
    selected = tools or cfg.tools
    report: dict[str, ToolScan] = {}
    for name in selected:
        slug = canonical_tool(name)
        if slug is None:
            continue
        report[slug] = scan_tool(slug, cfg, fixtures=fixtures, home=home)
    return report
