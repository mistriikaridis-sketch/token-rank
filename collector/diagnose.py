"""Local connection check for a first install.

Prints a Chinese reason and a next step. Posts only tool names, row counts,
and the clock — never chat, code, or the key itself.
"""
from __future__ import annotations

import json
import urllib.error
import urllib.request
from datetime import datetime, timezone
from typing import Any, Callable, Optional

from collector.config import SUPPORTED_TOOLS, CollectorConfig
from collector.sources import scan_all

SKEW_LIMIT_SECONDS = 300
TIMEOUT_SECONDS = 5

ERROR_NEXT = {
    "ok": "可以打开网页里的「我的」，看今天的用量。",
    "not_reported": "回到网页点「再检查连接」。若仍是未上报，在仓库根目录再运行一次 python -m collector once。",
    "invalid_key": "密钥无效。回到网页重新复制命令，不要手改，也不要转发。不要把服务器的写入密钥贴进网页。",
    "clock_skew": "打开系统设置，把日期和时间改成自动设置，等一分钟后再运行 python -m collector once。",
    "no_local_logs": "没装的工具不用管。已经安装的工具先在本机用一次，再运行 python -m collector preview。Cursor 本地全是 0 时，把仪表盘导出的 filtered-usage-events.json 放到 ~/.cursor/exports/ 。",
    "stale_report": "运行 python -m collector once。还是没有新数据时，运行 python -m collector service 看定时任务是否还在。",
    "unreachable": "先在浏览器打开排行榜地址，确认页面能开。打不开就先启动服务，或把地址发给负责安装的同事核对。",
    "missing_key": "回到网页「如何接入」，粘贴用户密钥后再复制命令。不要把命令发到群里。",
}

ERROR_REASON = {
    "ok": "已经连上，服务器收到过用量。",
    "not_reported": "密钥可用，但这账号还没有用量。",
    "invalid_key": "排行榜不认识这串密钥。",
    "clock_skew": "这台电脑的时间和服务器差得比较多，「今天」会对不齐。",
    "no_local_logs": "这台电脑上没找到可上报的本地用量。",
    "stale_report": "上次上报已经超过 36 小时。",
    "unreachable": "连不上排行榜。",
    "missing_key": "还没有个人上报密钥。",
}


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _parse_time(value: Any) -> Optional[datetime]:
    if not value:
        return None
    try:
        seen = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    if seen.tzinfo is None:
        seen = seen.replace(tzinfo=timezone.utc)
    return seen.astimezone(timezone.utc)


def _auth_headers(cfg: CollectorConfig) -> dict[str, str]:
    key = (cfg.api_key or "").strip()
    return {
        "Authorization": f"Bearer {key}",
        "X-API-Key": key,
        "Accept": "application/json",
        "Content-Type": "application/json",
    }


def _urlopen(req: urllib.request.Request) -> tuple[int, str]:
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT_SECONDS) as resp:
            return int(resp.status), resp.read().decode("utf-8")
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")
        return int(exc.code), detail


Fetch = Callable[[str, Optional[dict[str, Any]], CollectorConfig], tuple[int, str]]


def urllib_fetch(path: str, body: Optional[dict[str, Any]], cfg: CollectorConfig) -> tuple[int, str]:
    url = cfg.rankings_url.rstrip("/") + path
    data = None
    headers = {"Accept": "application/json"}
    if body is not None:
        data = json.dumps(body, ensure_ascii=False).encode("utf-8")
        headers = _auth_headers(cfg)
    req = urllib.request.Request(url, data=data, headers=headers, method="POST" if body is not None else "GET")
    try:
        return _urlopen(req)
    except urllib.error.URLError:
        return 0, ""


def _local_probe(cfg: CollectorConfig, *, tools: Optional[list[str]], fixtures: bool, home: Any) -> dict[str, Any]:
    report = scan_all(cfg, tools=tools, fixtures=fixtures, home=home)
    detected: list[str] = []
    missing: list[str] = []
    local_rows = 0
    for name in tools or list(SUPPORTED_TOOLS):
        info = report.get(name)
        if info is None:
            continue
        local_rows += len(info.rows)
        if info.found:
            detected.append(name)
        else:
            missing.append(name)
    return {
        "detected": detected,
        "missing": missing,
        "local_rows": local_rows,
        "has_local_logs": bool(detected) or local_rows > 0,
    }


def _result(code: str, *, probe: dict[str, Any], extra: Optional[dict[str, Any]] = None) -> dict[str, Any]:
    body = {
        "error_code": code,
        "message": ERROR_REASON.get(code, code),
        "next_step": ERROR_NEXT.get(code, "回到网页点「再检查连接」，把终端最后 30 行发出来时先删掉密钥。"),
        "detected": probe.get("detected") or [],
        "missing": probe.get("missing") or [],
        "local_rows": int(probe.get("local_rows") or 0),
        "has_local_logs": bool(probe.get("has_local_logs")),
    }
    if extra:
        body.update(extra)
    return body


def run_diagnose(
    cfg: CollectorConfig,
    *,
    tools: Optional[list[str]] = None,
    fixtures: bool = False,
    home: Any = None,
    fetch: Optional[Fetch] = None,
) -> dict[str, Any]:
    """Scan this machine and, when a key is set, tell the server what we found."""
    send = fetch or urllib_fetch
    probe = _local_probe(cfg, tools=tools, fixtures=fixtures, home=home)
    if not (cfg.api_key or "").strip():
        return _result("missing_key", probe=probe)
    status, raw = send("/health", None, cfg)
    if status == 0:
        return _result("unreachable", probe=probe)
    server_time = None
    try:
        health = json.loads(raw) if raw else {}
    except json.JSONDecodeError:
        health = {}
    if isinstance(health, dict):
        server_time = health.get("server_time")
    client_time = _now().isoformat()
    skew = None
    server_dt = _parse_time(server_time)
    if server_dt is not None:
        skew = (_now() - server_dt).total_seconds()
    payload = {
        "client_time": client_time,
        "detected": probe["detected"],
        "missing": probe["missing"],
        "local_rows": probe["local_rows"],
        "has_local_logs": probe["has_local_logs"],
    }
    code, report_raw = send("/api/v1/onboarding/report", payload, cfg)
    if code in (401, 403):
        return _result("invalid_key", probe=probe, extra={"http_status": code, "skew_seconds": skew})
    if code == 0 or code >= 400:
        return _result(
            "unreachable",
            probe=probe,
            extra={"http_status": code, "skew_seconds": skew},
        )
    try:
        reported = json.loads(report_raw) if report_raw else {}
    except json.JSONDecodeError:
        reported = {}
    if not isinstance(reported, dict):
        reported = {}
    error_code = str(reported.get("error_code") or "not_reported")
    result = _result(error_code, probe=probe, extra={"skew_seconds": reported.get("skew_seconds", skew), "server": reported})
    if reported.get("message"):
        result["message"] = reported["message"]
    if reported.get("next_step"):
        result["next_step"] = reported["next_step"]
    result["error_code"] = error_code
    return result


def format_diagnose(doc: dict[str, Any]) -> str:
    detected = "、".join(doc.get("detected") or []) or "（没有）"
    missing = "、".join(doc.get("missing") or []) or "（没有）"
    lines = [
        f"检测到：{detected}",
        f"未检测到：{missing}",
        f"失败（{doc.get('error_code')}）：{doc.get('message')}"
        if doc.get("error_code") not in ("ok",)
        else f"结果（{doc.get('error_code')}）：{doc.get('message')}",
        f"错误码：{doc.get('error_code')}",
        f"下一步：{doc.get('next_step')}",
        "只上报 Token 数字。不上传代码、对话、文件、密钥或截图。",
    ]
    return "\n".join(lines) + "\n"
