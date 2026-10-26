#!/usr/bin/env python3
"""Probe local AI coding tools for usage drop paths (no chat content uploaded).

Discovers common directories, counts jsonl/json lines, samples parse success via
WorkBuddy / Cursor / ProCode adapters, writes JSON + Markdown reports.

Examples:
  python scripts/probe_local_tools.py --home fixtures/probe_home --out /tmp/probe-report
  python scripts/probe_local_tools.py   # uses real $HOME
"""
from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

# Allow running from repo root without installing package
ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.adapters.cursor_admin import map_cursor_payload_with_stats  # noqa: E402
from app.adapters.procode import (  # noqa: E402
    ADAPTER_META as PROCODE_ADAPTER_META,
    PROCODE_HOME_RELATIVE,
    diagnostics_for_roots,
    scan_procode_dir,
)
from app.adapters.workbuddy import scan_workbuddy_dir_with_stats  # noqa: E402


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _exists_info(path: Path) -> dict[str, Any]:
    return {
        "path": str(path),
        "exists": path.exists(),
        "is_dir": path.is_dir() if path.exists() else False,
        "is_file": path.is_file() if path.exists() else False,
    }


def _count_jsonl(path: Path, limit_files: int = 200) -> dict[str, Any]:
    files = sorted(path.rglob("*.jsonl"))[:limit_files]
    lines = 0
    for f in files:
        try:
            lines += sum(1 for line in f.read_text(encoding="utf-8", errors="replace").splitlines() if line.strip())
        except OSError:
            continue
    return {"jsonl_files": len(files), "jsonl_lines": lines, "sample_files": [str(f) for f in files[:10]]}


def probe_workbuddy(home: Path) -> dict[str, Any]:
    candidates = [
        home / ".workbuddy" / "projects",
        home / ".workbody" / "projects",
        home / "Library" / "Application Support" / "WorkBuddy" / "projects",
    ]
    found = []
    parse = None
    chosen = None
    for c in candidates:
        info = _exists_info(c)
        if info["exists"] and info["is_dir"]:
            counts = _count_jsonl(c)
            info.update(counts)
            found.append(info)
            if chosen is None:
                chosen = c
        else:
            found.append(info)

    result: dict[str, Any] = {
        "tool": "workbuddy",
        "status": "missing",
        "candidates": found,
    }
    if chosen is not None:
        rows, stats = scan_workbuddy_dir_with_stats(chosen)
        # Ensure no content leaked into mapped rows
        leaked = [
            k
            for r in rows
            for k in r
            if k.lower() in ("content", "prompt", "message", "messages", "response", "transcript")
        ]
        result.update(
            {
                "status": "found",
                "scanned_root": str(chosen),
                "mapped_rows": len(rows),
                "parse_stats": stats.to_dict(),
                "content_leak_keys": leaked,
                "sample_mapped": [
                    {
                        "model": r.get("model"),
                        "prompt_tokens": r.get("prompt_tokens"),
                        "completion_tokens": r.get("completion_tokens"),
                        "account_id": r.get("account_id"),
                        "project_id": r.get("project_id"),
                        "agent_id": r.get("agent_id"),
                        "request_id": r.get("request_id"),
                    }
                    for r in rows[:5]
                ],
            }
        )
    return result


def probe_cursor(home: Path) -> dict[str, Any]:
    candidates = [
        home / ".cursor" / "exports",
        home / ".cursor",
        home / "Library" / "Application Support" / "Cursor",
    ]
    found = []
    export_payloads: list[Path] = []
    seen_exports: set[Path] = set()
    for c in candidates:
        info = _exists_info(c)
        if info["exists"] and info["is_dir"]:
            json_files = sorted(c.rglob("*.json"))[:50]
            jsonl = _count_jsonl(c)
            info["json_files"] = len(json_files)
            info.update(jsonl)
            info["sample_json"] = [str(p) for p in json_files[:10]]
            found.append(info)
            for p in json_files:
                name = p.name.lower()
                if "usage" in name or "filtered" in name or "export" in name:
                    rp = p.resolve()
                    if rp not in seen_exports:
                        seen_exports.add(rp)
                        export_payloads.append(p)
        else:
            found.append(info)

    result: dict[str, Any] = {
        "tool": "cursor",
        "status": "missing",
        "candidates": found,
        "note": "生产推荐 Cursor Admin API；本地 exports 仅作离线对照。",
    }

    mapped_total = 0
    stats_merged = None
    samples = []
    for p in export_payloads[:5]:
        try:
            payload = json.loads(p.read_text(encoding="utf-8", errors="replace"))
        except (OSError, json.JSONDecodeError) as exc:
            result.setdefault("read_errors", []).append({"path": str(p), "error": str(exc)})
            continue
        rows, stats = map_cursor_payload_with_stats(payload)
        mapped_total += len(rows)
        if stats_merged is None:
            stats_merged = stats
        else:
            stats_merged.merge(stats)
        samples.extend(
            [
                {
                    "model": r.get("model"),
                    "account_id": r.get("account_id"),
                    "project_id": r.get("project_id"),
                    "request_id": r.get("request_id"),
                    "cost_basis": r.get("cost_basis"),
                }
                for r in rows[:3]
            ]
        )

    if any(c.get("exists") for c in found):
        result["status"] = "found_paths"
    if mapped_total:
        result["status"] = "parsed_exports"
        result["mapped_rows"] = mapped_total
        result["parse_stats"] = stats_merged.to_dict() if stats_merged else {}
        result["sample_mapped"] = samples[:8]
        result["export_files"] = [str(p) for p in export_payloads[:10]]
    return result


def probe_procode(home: Path) -> dict[str, Any]:
    """Scan usage-export dirs. Never opens encrypted SQLite or launches canoryn."""
    candidates: list[Path] = [home / rel for rel in PROCODE_HOME_RELATIVE]
    try:
        for child in home.iterdir():
            name = child.name.lower()
            if name.startswith(".canoryn") or name.startswith(".procode") or name in (
                "canoryn",
                "procode",
            ):
                candidates.append(child)
    except OSError:
        pass

    found: list[dict[str, Any]] = []
    seen: set[str] = set()
    scanned: list[str] = []
    rows: list[dict[str, Any]] = []
    for path in candidates:
        key = str(path)
        if key in seen:
            continue
        seen.add(key)
        info = _exists_info(path)
        if info["exists"] and info["is_dir"]:
            info.update(_count_jsonl(path))
            scanned.append(key)
            rows.extend(scan_procode_dir(path))
        found.append(info)

    deduped: list[dict[str, Any]] = []
    seen_ids: set[str] = set()
    for row in rows:
        rid = str(row.get("request_id") or "")
        if rid and rid in seen_ids:
            continue
        if rid:
            seen_ids.add(rid)
        deduped.append(row)
    rows = deduped

    diagnostics = diagnostics_for_roots(scanned) if scanned else {
        "adapter_status": "placeholder",
        "blocked": list(PROCODE_ADAPTER_META["blocked"]),
        "note_zh": PROCODE_ADAPTER_META["notes"],
        "parsed_rows": 0,
    }
    leaked = [
        k
        for row in rows
        for k in row
        if str(k).lower() in ("content", "prompt", "message", "messages", "response", "transcript")
    ]
    mapped = len({row.get("request_id") for row in rows})
    if any(row.get("request_id") for row in rows):
        status = "parsed_exports"
    elif any(isinstance(c, dict) and c.get("exists") for c in found):
        status = "paths_present_no_schema"
    else:
        status = "placeholder"
    return {
        "tool": "procode",
        "status": status,
        "adapter_status": "placeholder",
        "aliases": ["procode", "canoryn"],
        "candidates": found,
        "mapped_rows": len(rows),
        "distinct_request_ids": mapped,
        "parse_stats": diagnostics.get("parse") or {},
        "diagnostics": diagnostics,
        "content_leak_keys": leaked,
        "sample_mapped": [
            {
                "model": row.get("model"),
                "account_id": row.get("account_id"),
                "project_id": row.get("project_id"),
                "request_id": row.get("request_id"),
            }
            for row in rows[:8]
        ],
        "notes": PROCODE_ADAPTER_META["notes"],
        "blocked": list(PROCODE_ADAPTER_META["blocked"]),
    }


def build_report(home: Path) -> dict[str, Any]:
    wb = probe_workbuddy(home)
    cur = probe_cursor(home)
    pc = probe_procode(home)
    return {
        "generated_at": _utc_now(),
        "home": str(home),
        "tools": {
            "workbuddy": wb,
            "cursor": cur,
            "procode": pc,
        },
        "summary": {
            "workbuddy": wb.get("status"),
            "cursor": cur.get("status"),
            "procode": pc.get("status"),
            "procode_adapter_status": "placeholder",
            "workbuddy_success_rate_pct": (wb.get("parse_stats") or {}).get("success_rate_pct"),
            "cursor_success_rate_pct": (cur.get("parse_stats") or {}).get("success_rate_pct"),
            "procode_success_rate_pct": (
                (pc.get("parse_stats") or {}).get("success_rate_pct")
                if (pc.get("parse_stats") or {}).get("lines_total")
                else None
            ),
        },
    }


def to_markdown(report: dict[str, Any]) -> str:
    lines = [
        f"# 本地工具探测报告",
        "",
        f"- 生成时间（UTC）：`{report['generated_at']}`",
        f"- HOME：`{report['home']}`",
        "",
        "## 摘要",
        "",
        "| 工具 | 状态 | 解析成功率 |",
        "|---|---|---|",
    ]
    s = report["summary"]
    lines.append(
        f"| WorkBuddy | `{s.get('workbuddy')}` | {s.get('workbuddy_success_rate_pct')}% |"
    )
    lines.append(
        f"| Cursor | `{s.get('cursor')}` | {s.get('cursor_success_rate_pct')}% |"
    )
    rate = s.get("procode_success_rate_pct")
    rate_cell = "n/a（占位，无 usage 文件）" if rate is None else f"{rate}%（适配仍是 placeholder）"
    lines.append(f"| ProCode | `{s.get('procode')}` | {rate_cell} |")
    lines.append("")

    for key, title in (
        ("workbuddy", "WorkBuddy"),
        ("cursor", "Cursor"),
        ("procode", "ProCode / Canoryn"),
    ):
        block = report["tools"][key]
        lines.append(f"## {title}")
        lines.append("")
        lines.append(f"- status: `{block.get('status')}`")
        if block.get("scanned_root"):
            lines.append(f"- scanned_root: `{block['scanned_root']}`")
        if block.get("mapped_rows") is not None:
            lines.append(f"- mapped_rows: **{block['mapped_rows']}**")
        if block.get("parse_stats"):
            ps = block["parse_stats"]
            lines.append(
                f"- parse: lines={ps.get('lines_total')} ok={ps.get('parsed_ok')} "
                f"skip_total={ps.get('skipped_total')} "
                f"(json_err={ps.get('skipped_json_error')}, not_usage={ps.get('skipped_not_usage')}, "
                f"missing_model={ps.get('skipped_missing_model')}, missing_tokens={ps.get('skipped_missing_tokens')}, "
                f"forbidden_content={ps.get('skipped_forbidden_content')})"
            )
        if block.get("content_leak_keys") == []:
            lines.append("- content leak check: **pass**（映射行无 content/prompt）")
        elif block.get("content_leak_keys"):
            lines.append(f"- content leak check: FAIL {block['content_leak_keys']}")
        if block.get("notes"):
            lines.append(f"- notes: {block['notes']}")
        diag = block.get("diagnostics") or {}
        if diag.get("adapter_status"):
            lines.append(f"- adapter_status: `{diag['adapter_status']}`（健康检查仍是这一档）")
        if diag.get("unopened_db_seen"):
            names = ", ".join(diag.get("unopened_db_basenames") or [])
            lines.append(f"- 未打开的数据库/打包文件: **{diag['unopened_db_seen']}**（{names}）")
        if diag.get("discussions_skipped") or diag.get("blueprints_skipped"):
            lines.append(
                f"- 跳过 discussions={diag.get('discussions_skipped')} "
                f"blueprints={diag.get('blueprints_skipped')} "
                f"凭证文件名={diag.get('files_skipped_credential_name')}"
            )
        lines.append("")
        lines.append("候选路径：")
        for c in block.get("candidates") or []:
            if not isinstance(c, dict):
                continue
            flag = "✓" if c.get("exists") else "·"
            extra = ""
            if c.get("jsonl_files") is not None:
                extra = f" · jsonl_files={c['jsonl_files']} lines={c.get('jsonl_lines')}"
            lines.append(f"- {flag} `{c.get('path')}`{extra}")
        lines.append("")

    lines.append("## 闭环建议")
    lines.append("")
    lines.append("1. WorkBuddy：`POST /api/v1/scan/workbuddy?root=...`（需 write API Key）")
    lines.append("2. Cursor：本机 `python -m collector`（state.vscdb 只抽 token；或 `~/.cursor/exports`），或 Admin sync / `POST /api/v1/ingest/cursor`")
    lines.append(
        "3. ProCode：usage json/jsonl → `python -m collector`，或统一 schema → `POST /api/v1/ingest/procode`。"
        "不解密聊天库，不打开 sqlite，不读钥匙串，不启动 canoryn。健康检查里的状态保持 placeholder。"
    )
    lines.append("")
    return "\n".join(lines) + "\n"


def main() -> int:
    ap = argparse.ArgumentParser(description="Probe WorkBuddy / Cursor / ProCode local paths")
    ap.add_argument(
        "--home",
        default=str(Path.home()),
        help="HOME root to scan (use fixtures/probe_home for CI/demo)",
    )
    ap.add_argument(
        "--out",
        default="",
        help="Output prefix without extension; writes .json and .md",
    )
    ap.add_argument("--stdout-md", action="store_true", help="Print markdown to stdout")
    args = ap.parse_args()
    home = Path(args.home).expanduser().resolve()
    report = build_report(home)
    md = to_markdown(report)

    if args.out:
        out = Path(args.out)
        out.parent.mkdir(parents=True, exist_ok=True)
        json_path = out if out.suffix == ".json" else Path(str(out) + ".json")
        md_path = json_path.with_suffix(".md")
        json_path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
        md_path.write_text(md, encoding="utf-8")
        print(f"wrote {json_path}")
        print(f"wrote {md_path}")
    if args.stdout_md or not args.out:
        print(md)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
