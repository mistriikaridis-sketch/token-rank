"""CLI: install / preview / once / upload / service."""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Optional, Sequence

from collector.config import (
    DEFAULT_INTERVAL_MINUTES,
    SUPPORTED_TOOLS,
    CollectorConfig,
    env_file_path,
    load_config,
    save_config,
)
from collector.diagnose import format_diagnose, run_diagnose
from collector.payload import build_payloads, summarize
from collector.service import detect_os, enable_hint, install_files, render, try_enable_systemd
from collector.sources import canonical_tool, scan_all
from collector.upload import UploadError, upload_payloads


def _configure_stdio() -> None:
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is not None:
            try:
                reconfigure(encoding="utf-8")
            except (OSError, ValueError):
                pass


def _positive_int(value: str) -> int:
    number = int(value)
    if number < 1:
        raise argparse.ArgumentTypeError("间隔至少 1 分钟")
    return number


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m collector",
        description=(
            "本机采集 WorkBuddy / Claude Code / Codex / Continue / Cursor / ProCode 的 Token 用量，"
            "上报到私有 Token 消耗排行榜。不上传对话、代码或源文件名。"
        ),
    )
    sub = parser.add_subparsers(dest="command", required=True)

    install = sub.add_parser("install", help="把配置写到本机（默认不含密钥）")
    install.add_argument("--url", help="排行榜地址，例如 http://127.0.0.1:8000")
    install.add_argument("--account-id", help="榜上的账号。会覆盖日志里的邮箱")
    install.add_argument("--participant", help="榜上的显示名")
    install.add_argument("--team", help="部门；只在日志没有部门时补上")
    install.add_argument(
        "--interval",
        type=_positive_int,
        default=None,
        help=f"定时采集间隔（分钟），默认 {DEFAULT_INTERVAL_MINUTES}",
    )
    install.add_argument(
        "--save-key",
        action="store_true",
        help="把当前 TOKEN_RANKINGS_API_KEY 写入 collector.env（权限 0600）",
    )
    install.set_defaults(func=cmd_install)

    preview = sub.add_parser("preview", help="扫描并打印将要上报的用量，不发送")
    _add_scan_flags(preview)
    preview.add_argument("--json", action="store_true", help="把预览以 JSON 打到标准输出")
    preview.add_argument("--out", help="把同一份 JSON 写到文件，供 upload 使用")
    preview.set_defaults(func=cmd_preview)

    once = sub.add_parser("once", help="扫描并立刻上报")
    _add_scan_flags(once)
    once.add_argument("--dry-run", action="store_true", help="同 preview，不发送")
    once.add_argument("--json", action="store_true", help="dry-run 时输出 JSON")
    once.add_argument("--url", help="覆盖 RANKINGS_URL")
    once.set_defaults(func=cmd_once)

    upload = sub.add_parser("upload", help="上报 preview --out 产生的 JSON")
    upload.add_argument("--file", required=True, help="preview --out 写出的文件")
    upload.add_argument("--url", help="覆盖 RANKINGS_URL")
    upload.set_defaults(func=cmd_upload)

    diagnose = sub.add_parser("diagnose", help="检查连接：未上报、密钥、时钟、本地日志")
    _add_scan_flags(diagnose)
    diagnose.add_argument("--json", action="store_true", help="以 JSON 打印诊断")
    diagnose.set_defaults(func=cmd_diagnose)

    service = sub.add_parser("service", help="安装 cron / systemd / launchd / 计划任务")
    service.add_argument(
        "--os",
        dest="os_name",
        choices=("auto", "linux", "macos", "windows", "cron"),
        default="auto",
        help="auto 按当前系统选择；cron 是可移植的例子",
    )
    service.add_argument("--interval", type=_positive_int, default=None)
    service.add_argument("--print", dest="print_only", action="store_true", help="只打印，不写文件")
    service.add_argument("--enable", action="store_true", help="Linux 上尝试 systemctl --user enable")
    service.set_defaults(func=cmd_service)
    return parser


def _add_scan_flags(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--fixtures",
        action="store_true",
        help="扫描仓库 fixtures/*_sample，而不是家目录",
    )
    parser.add_argument("--home", help="把这个目录当作家目录来找日志")
    parser.add_argument(
        "--tool",
        action="append",
        default=None,
        help="只扫一种工具，可重复。workbody 会当成 workbuddy，canoryn 会当成 procode",
    )
    parser.add_argument("--account-id")
    parser.add_argument("--participant")
    parser.add_argument("--team")


def _selected_tools(args: argparse.Namespace, cfg: CollectorConfig) -> list[str]:
    if not args.tool:
        return list(cfg.tools)
    selected: list[str] = []
    for name in args.tool:
        slug = canonical_tool(name)
        if slug is None:
            names = " ".join(SUPPORTED_TOOLS)
            raise SystemExit(
                f"不支持的工具：{name}。可选：{names}（workbody→workbuddy，canoryn→procode）"
            )
        if slug not in selected:
            selected.append(slug)
    return selected


def _apply_cli_identity(cfg: CollectorConfig, args: argparse.Namespace) -> None:
    if getattr(args, "url", None):
        cfg.rankings_url = str(args.url).rstrip("/")
    if getattr(args, "account_id", None):
        cfg.account_id = args.account_id
    if getattr(args, "participant", None):
        cfg.participant = args.participant
    if getattr(args, "team", None):
        cfg.team = args.team


def _scan_from_args(args: argparse.Namespace, cfg: CollectorConfig):
    home = Path(args.home).expanduser() if getattr(args, "home", None) else None
    tools = _selected_tools(args, cfg)
    return scan_all(cfg, tools=tools, fixtures=bool(args.fixtures), home=home)


def _preview_document(args: argparse.Namespace, cfg: CollectorConfig) -> dict:
    report = _scan_from_args(args, cfg)
    payloads = build_payloads(report)
    doc = summarize(report, payloads)
    doc["rankings_url"] = cfg.rankings_url
    doc["api_key_configured"] = bool(cfg.api_key)
    doc["payloads"] = payloads
    return doc


def _print_human(doc: dict) -> None:
    totals = doc["totals"]
    print(
        f"共 {totals['rows']} 行用量，"
        f"prompt {totals['prompt_tokens']}，completion {totals['completion_tokens']}，"
        f"估算费用 {totals['estimated_cost_usd']} USD（{totals['cost_note_zh']}）",
        file=sys.stderr,
    )
    for tool, info in doc["tools"].items():
        roots = ", ".join(info["roots"]) or "（没找到目录）"
        missing = ""
        if info["missing_roots"]:
            missing = "；缺少 " + ", ".join(info["missing_roots"])
        print(f"  {tool}: {info['rows']} 行 ← {roots}{missing}", file=sys.stderr)
        note = (info.get("diagnostics") or {}).get("note_zh")
        if note:
            print(f"    {note}", file=sys.stderr)
    print(doc["privacy"]["model_support_zh"], file=sys.stderr)


def _emit_preview(doc: dict, *, as_json: bool, out_path: Optional[str]) -> None:
    text = json.dumps(doc, ensure_ascii=False, indent=2)
    if out_path:
        Path(out_path).write_text(text + "\n", encoding="utf-8")
        print(f"已写入 {out_path}", file=sys.stderr)
    if as_json:
        print(text)
    else:
        _print_human(doc)


def cmd_install(args: argparse.Namespace) -> int:
    cfg = load_config()
    _apply_cli_identity(cfg, args)
    if args.interval is not None:
        cfg.interval_minutes = args.interval
    key_to_save = None
    if args.save_key:
        key_to_save = cfg.api_key
        if not key_to_save:
            print(
                "没有读到 TOKEN_RANKINGS_API_KEY，未写 collector.env。",
                file=sys.stderr,
            )
            return 2
    path = save_config(cfg, api_key=key_to_save)
    print(f"已写入 {path}")
    if key_to_save:
        print(f"写入密钥在 {env_file_path()}（权限尽量设为仅本人可读）")
    else:
        print("写入密钥没有放进 json。请导出 TOKEN_RANKINGS_API_KEY，或再执行 install --save-key。")
    print("下一步：python -m collector preview")
    print("确认无误后：python -m collector once")
    print("定时：python -m collector service --os linux   或   --os macos   或   --os cron --print")
    return 0


def cmd_preview(args: argparse.Namespace) -> int:
    cfg = load_config()
    _apply_cli_identity(cfg, args)
    doc = _preview_document(args, cfg)
    _emit_preview(doc, as_json=bool(args.json), out_path=args.out)
    return 0


def cmd_once(args: argparse.Namespace) -> int:
    cfg = load_config()
    _apply_cli_identity(cfg, args)
    if args.dry_run:
        doc = _preview_document(args, cfg)
        _emit_preview(doc, as_json=bool(args.json), out_path=None)
        return 0
    report = _scan_from_args(args, cfg)
    payloads = build_payloads(report)
    doc = summarize(report, payloads)
    _print_human(doc)
    if doc["totals"]["rows"] == 0:
        print("没有用量可上报。", file=sys.stderr)
        return 0
    try:
        result = upload_payloads(payloads, cfg)
    except UploadError as exc:
        print(str(exc), file=sys.stderr)
        if exc.body:
            print(exc.body, file=sys.stderr)
        return 1
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


def cmd_upload(args: argparse.Namespace) -> int:
    cfg = load_config()
    _apply_cli_identity(cfg, args)
    path = Path(args.file)
    try:
        doc = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        print(f"读不了 {path}：{exc}", file=sys.stderr)
        return 2
    payloads = doc.get("payloads")
    if not isinstance(payloads, dict):
        print("文件里没有 payloads。请用 preview --out 生成。", file=sys.stderr)
        return 2
    cleaned = {str(k): v for k, v in payloads.items() if isinstance(v, list)}
    try:
        result = upload_payloads(cleaned, cfg)
    except UploadError as exc:
        print(str(exc), file=sys.stderr)
        if exc.body:
            print(exc.body, file=sys.stderr)
        return 1
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


def cmd_diagnose(args: argparse.Namespace) -> int:
    cfg = load_config()
    _apply_cli_identity(cfg, args)
    home = Path(args.home).expanduser() if getattr(args, "home", None) else None
    tools = _selected_tools(args, cfg)
    doc = run_diagnose(cfg, tools=tools, fixtures=bool(args.fixtures), home=home)
    if args.json:
        print(json.dumps(doc, ensure_ascii=False, indent=2))
    else:
        print(format_diagnose(doc), end="")
    if doc.get("error_code") in ("invalid_key", "clock_skew", "unreachable", "missing_key"):
        return 1
    return 0


def cmd_service(args: argparse.Namespace) -> int:
    cfg = load_config()
    interval = args.interval if args.interval is not None else cfg.interval_minutes
    os_name = detect_os(args.os_name)
    text = render(os_name, interval_minutes=interval)
    if args.print_only:
        print(text, end="" if text.endswith("\n") else "\n")
        if os_name != "cron":
            print("\n# 可移植 cron 例子：", file=sys.stderr)
            print(render("cron", interval_minutes=interval), end="", file=sys.stderr)
        return 0
    written = install_files(os_name, interval_minutes=interval)
    for path in written:
        print(f"已写入 {path}")
    print(enable_hint(os_name))
    if os_name != "cron":
        cron_path = install_files("cron", interval_minutes=interval)
        print(f"可移植 cron 例子也写了：{cron_path[0]}")
    if args.enable and os_name == "linux":
        print(try_enable_systemd())
    elif args.enable:
        print("当前只在 --os linux 时尝试自动启用。请按上面的命令手动启用。")
    return 0


def main(argv: Optional[Sequence[str]] = None) -> int:
    _configure_stdio()
    parser = _build_parser()
    args = parser.parse_args(list(argv) if argv is not None else None)
    return int(args.func(args))
