"""Enterprise self-service: onboarding, privacy, my usage, visibility, devices.

This is not a public city or community leaderboard. Collector tokens can
only write usage for one account. They cannot scan the server or clear data.
"""
from __future__ import annotations

import hashlib
import secrets
import shlex
from datetime import datetime, timedelta, timezone
from typing import Any, Literal, Optional
from zoneinfo import ZoneInfo

from fastapi import APIRouter, Depends, Header, HTTPException, Query, Request
from fastapi.responses import Response
from pydantic import BaseModel, Field

from app.auth import (
    Principal,
    apply_account_scope,
    extract_presented_key,
    optional_principal,
    resolve_principal,
)
from app.collector_scope import CollectorScope, current_collector_scope
from app.db import db, insert_audit
from app.forum import looks_like_secret
from app.pricing import FORMULA_ZH, LEADERBOARD_COST_ZH, estimate_usage_parts, pricing_formula
from app.tools import tool_display

router = APIRouter()

VISIBILITY_COMPANY = "company"
VISIBILITY_DEPARTMENT = "department"
VISIBILITY_SELF = "self"
VISIBILITIES = (VISIBILITY_COMPANY, VISIBILITY_DEPARTMENT, VISIBILITY_SELF)

DEVICE_ACTIVE = "active"
DEVICE_HIDDEN = "hidden"
DEVICE_DISABLED = "disabled"
DEVICE_STATUSES = (DEVICE_ACTIVE, DEVICE_HIDDEN, DEVICE_DISABLED)

DEFAULT_TZ = "Asia/Shanghai"
TIMEZONES: dict[str, str] = {
    "Asia/Shanghai": "北京时间",
    "Asia/Tokyo": "东京时间",
    "Asia/Singapore": "新加坡时间",
    "Europe/London": "伦敦时间",
    "America/Los_Angeles": "美国太平洋时间",
    "America/New_York": "美国东部时间",
    "UTC": "UTC",
}

SECRET_WARNING = "勿转发。命令里含个人上报密钥。别人拿到后，可以往你的账号写入用量。"

AI_INSTALL_INTRO = (
    "请在本机的 token-rank 仓库根目录帮我接入企业内部 Token 消耗排行榜。"
    "只做下面这些事：\n"
    "1. 运行我贴出的安装命令。命令里有个人上报密钥，不要发到别的聊天，不要写进 git。\n"
    "2. 运行 python -m collector preview，确认输出只有 Token 数字、模型名、账号和项目目录名。\n"
    "3. 确认没有上传代码、对话、文件内容、密钥或截图。\n"
    "4. 再运行 python -m collector once。\n"
    "采集器只上报 Token 计数。ProCode 不解密、不打开 sqlite、不读钥匙串、不启动 canoryn。\n"
)

AI_AUDIT_PROMPT = (
    "请帮我审计这台电脑上的 Token 消耗排行榜采集器，只看结构和去向，不要读取对话正文，"
    "不要解密任何数据库，不要把密钥贴回聊天。\n"
    "请检查：\n"
    "1. 这些目录是否存在，以及里面有没有 usage 日志（不要打开对话内容）：\n"
    "   ~/.workbuddy  ~/.workbody  ~/.claude  ~/.codex  ~/.continue\n"
    "   ~/.cursor  ~/Library/Application Support/Cursor  %APPDATA%\\Cursor\n"
    "   ~/.procode  ~/.canoryn  以及各系统应用数据目录里的 ProCode / Canoryn\n"
    "2. 定时任务：crontab、systemd --user、launchd、Windows 计划任务里有没有 collector。\n"
    "3. 出站地址是不是只有排行榜自己的 RANKINGS_URL。不要连到别的用量网站。\n"
    "4. python -m collector preview 的字段是否只有模型名和 Token 数字。\n"
    "5. ProCode：确认没有解密、没有打开 sqlite、没有读钥匙串、没有启动 canoryn。\n"
    "用简短中文告诉我：会上报什么、只留在本机什么、你没看到密钥或对话原文。\n"
)

PRIVACY_ROWS: list[dict[str, str]] = [
    {"item": "Token 计数（输入、输出、缓存读、缓存写、推理）", "bucket": "会上传", "note": "只有数字"},
    {"item": "模型名、工具名、时间", "bucket": "会上传", "note": "用来归类，并按北京时间切日"},
    {"item": "账号、显示名、部门", "bucket": "会上传", "note": "只在企业内部排行里用"},
    {"item": "项目目录名", "bucket": "会上传", "note": "例如 chatbot-core，不是完整路径，也不是源文件名"},
    {"item": "去重用的 request_id", "bucket": "会上传", "note": "同一条再报一次不会叠量"},
    {"item": "本机原始日志和 Cursor 的 state.vscdb", "bucket": "仅本地", "note": "只在你的电脑上抽出计数，整库不上传"},
    {"item": "定时任务和采集配置", "bucket": "仅本地", "note": "cron / systemd / launchd / 计划任务"},
    {"item": "个人上报密钥（collector.env）", "bucket": "仅本地", "note": "文件权限 0600。不要转发，不要提交到 git"},
    {"item": "代码和源文件内容", "bucket": "永不采集", "note": "不上传"},
    {"item": "对话、提示词、回复", "bucket": "永不采集", "note": "含大段 content / prompt 的行会整行跳过"},
    {"item": "文件正文、截图", "bucket": "永不采集", "note": "不上传"},
    {"item": "密钥、Cookie、钥匙串", "bucket": "永不采集", "note": "ProCode 不解密、不打开 sqlite、不读钥匙串、不启动 canoryn"},
]

DRY_RUN_COMMANDS = [
    "python -m collector preview",
    "python -m collector preview --json",
    "python -m collector once --dry-run",
    "python -m collector preview --fixtures --json",
]

ANOMALY_RULE = (
    "按北京时间看今天。如果今天的 Token 达到近 14 个有用量日期的中位数的 3 倍，"
    "并且不少于 5 万，就标成「异常用量待审」。这是给费用核对用的，不是比赛处罚。"
)


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _sha256(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _hint(value: str) -> str:
    if len(value) <= 4:
        return "****"
    return "…" + value[-4:]


def _zone(name: str) -> ZoneInfo:
    if name not in TIMEZONES:
        raise HTTPException(
            status_code=400,
            detail={
                "error": "bad_timezone",
                "message": "时区不在可选列表里。默认是北京时间。",
                "allowed": list(TIMEZONES),
            },
        )
    return ZoneInfo(name)


def _require_user(principal: Optional[Principal]) -> Principal:
    if principal is None:
        raise HTTPException(
            status_code=401,
            detail={
                "error": "missing_api_key",
                "message": "请先在页面「首次配置」或右上角粘贴用户密钥。",
            },
        )
    return principal


def _account_for(
    principal: Optional[Principal],
    account_id: Optional[str],
    *,
    required: bool = True,
) -> Optional[str]:
    scoped = apply_account_scope(principal, account_id)
    if scoped:
        return scoped.strip()
    if principal and principal.role == "member" and principal.account_id:
        return principal.account_id
    if not required:
        return None
    raise HTTPException(
        status_code=400,
        detail={
            "error": "account_id_required",
            "message": "请先粘贴用户密钥，或填上你的账号。管理员需要指定账号。",
        },
    )


def match_collector_token(presented: Optional[str]) -> Optional[CollectorScope]:
    """Return the account scope for a personal report token, else None."""
    if not presented or not presented.startswith("ctr_"):
        return None
    digest = _sha256(presented)
    try:
        with db() as conn:
            row = conn.execute(
                """
                SELECT account_id FROM collector_tokens
                WHERE token_sha256 = ?
                """,
                (digest,),
            ).fetchone()
    except Exception:
        return None
    if not row or not row["account_id"]:
        return None
    return CollectorScope(account_id=str(row["account_id"]), token_hint=_hint(presented))


def gate_ingest_row(conn: Any, row: dict[str, Any]) -> Optional[dict[str, Any]]:
    """Force collector-token writes onto that account. Drop disabled tools."""
    out = dict(row)
    scope = current_collector_scope()
    if scope is not None and scope.account_id:
        out["account_id"] = scope.account_id
    account = str(out.get("account_id") or "").strip()
    tool = str(out.get("tool") or "unknown").strip() or "unknown"
    if not account:
        return out
    try:
        found = conn.execute(
            """
            SELECT status FROM devices
            WHERE account_id = ? AND device_key = ?
            """,
            (account, tool),
        ).fetchone()
    except Exception:
        return out
    if found and str(found["status"]) == DEVICE_DISABLED:
        return None
    return out


def remember_devices(touches: list[tuple[str, str, str]]) -> None:
    if not touches:
        return
    with db() as conn:
        for account, tool, ts in touches:
            if not account:
                continue
            key = tool or "unknown"
            row = conn.execute(
                """
                SELECT id, status FROM devices
                WHERE account_id = ? AND device_key = ?
                """,
                (account, key),
            ).fetchone()
            if row is None:
                conn.execute(
                    """
                    INSERT INTO devices
                    (account_id, device_key, name, status, last_seen, last_tool,
                     report_count, created_at)
                    VALUES (?, ?, ?, ?, ?, ?, 1, ?)
                    """,
                    (
                        account,
                        key,
                        tool_display(key),
                        DEVICE_ACTIVE,
                        ts or _now().isoformat(),
                        key,
                        _now().isoformat(),
                    ),
                )
            elif str(row["status"]) != DEVICE_DISABLED:
                conn.execute(
                    """
                    UPDATE devices
                    SET last_seen = ?, last_tool = ?, report_count = report_count + 1
                    WHERE id = ?
                    """,
                    (ts or _now().isoformat(), key, row["id"]),
                )


def _default_prefs(account_id: str) -> dict[str, Any]:
    return {
        "account_id": account_id,
        "visibility": VISIBILITY_COMPANY,
        "visibility_label": "全公司",
        "org_tag": None,
        "department": None,
        "timezone": DEFAULT_TZ,
        "timezone_label": TIMEZONES[DEFAULT_TZ],
        "compare_timezone": None,
        "compare_timezone_label": None,
        "show_rank": True,
    }


def _visibility_label(value: str) -> str:
    return {
        VISIBILITY_COMPANY: "全公司",
        VISIBILITY_DEPARTMENT: "本部门",
        VISIBILITY_SELF: "仅自己",
    }.get(value, value)


def _row_prefs(row: Any) -> dict[str, Any]:
    visibility = str(row["visibility"] or VISIBILITY_COMPANY)
    if visibility not in VISIBILITIES:
        visibility = VISIBILITY_COMPANY
    tz_name = str(row["timezone"] or DEFAULT_TZ)
    if tz_name not in TIMEZONES:
        tz_name = DEFAULT_TZ
    compare = row["compare_timezone"] or None
    if compare and compare not in TIMEZONES:
        compare = None
    return {
        "account_id": row["account_id"],
        "visibility": visibility,
        "visibility_label": _visibility_label(visibility),
        "org_tag": row["org_tag"] or None,
        "department": row["department"] or None,
        "timezone": tz_name,
        "timezone_label": TIMEZONES.get(tz_name, tz_name),
        "compare_timezone": compare,
        "compare_timezone_label": TIMEZONES.get(compare) if compare else None,
        "show_rank": bool(int(row["show_rank"] or 0)),
    }


def get_prefs(account_id: str) -> dict[str, Any]:
    with db() as conn:
        row = conn.execute(
            "SELECT * FROM account_prefs WHERE account_id = ?",
            (account_id,),
        ).fetchone()
    if not row:
        return _default_prefs(account_id)
    return _row_prefs(row)


def _all_prefs() -> dict[str, dict[str, Any]]:
    try:
        with db() as conn:
            rows = conn.execute("SELECT * FROM account_prefs").fetchall()
    except Exception:
        return {}
    return {str(r["account_id"]): _row_prefs(r) for r in rows}


def _dominant_team(account_id: str) -> str:
    if not account_id:
        return ""
    with db() as conn:
        row = conn.execute(
            """
            SELECT team FROM usage_events
            WHERE account_id = ?
              AND COALESCE(TRIM(team), '') != ''
              AND COALESCE(source, '') != 'seed'
            GROUP BY team
            ORDER BY COUNT(*) DESC
            LIMIT 1
            """,
            (account_id,),
        ).fetchone()
    if not row:
        return ""
    return str(row["team"] or "").strip()


def visibility_clause(principal: Optional[Principal]) -> tuple[str, list[Any]]:
    """Hide accounts that opted out of the company board.

    Admins still see everyone, so FinOps can reconcile cost.
    Anonymous viewers only see 全公司. Members also see 本部门 peers
    and always see themselves.
    """
    if principal is not None and principal.role == "admin":
        return "", []
    viewer = ""
    if principal is not None and principal.role == "member":
        viewer = (principal.account_id or "").strip()
    viewer_team = _dominant_team(viewer) if viewer else ""
    if viewer:
        own = get_prefs(viewer)
        if own.get("department"):
            viewer_team = own["department"] or viewer_team
    hidden: list[str] = []
    for account_id, pref in _all_prefs().items():
        if viewer and account_id == viewer:
            continue
        visibility = pref["visibility"]
        if visibility == VISIBILITY_COMPANY:
            continue
        if visibility == VISIBILITY_DEPARTMENT and viewer_team:
            their_team = pref.get("department") or _dominant_team(account_id)
            if their_team and their_team == viewer_team:
                continue
        hidden.append(account_id)
    if not hidden:
        return "", []
    marks = ", ".join("?" for _ in hidden)
    clause = (
        "(account_id IS NULL OR TRIM(account_id) = '' "
        f"OR account_id NOT IN ({marks}))"
    )
    return clause, hidden


def mint_collector_token(account_id: str) -> str:
    with db() as conn:
        row = conn.execute(
            "SELECT token_plain FROM collector_tokens WHERE account_id = ?",
            (account_id,),
        ).fetchone()
        if row and row["token_plain"]:
            return str(row["token_plain"])
        token = "ctr_" + secrets.token_urlsafe(24)
        conn.execute(
            """
            INSERT INTO collector_tokens
            (account_id, token_sha256, token_hint, token_plain, created_at)
            VALUES (?, ?, ?, ?, ?)
            """,
            (account_id, _sha256(token), _hint(token), token, _now().isoformat()),
        )
        return token


def _shell_quote(value: str) -> str:
    return shlex.quote(value or "")


def _ps_quote(value: str) -> str:
    return "'" + (value or "").replace("'", "''") + "'"


def build_commands(
    *,
    base_url: str,
    account_id: str,
    participant: str,
    token: str,
) -> dict[str, str]:
    url = base_url.rstrip("/")
    unix_direct = "\n".join(
        [
            f"export TOKEN_RANKINGS_API_KEY={_shell_quote(token)}",
            f"export RANKINGS_URL={_shell_quote(url)}",
            "python -m collector install --url \"$RANKINGS_URL\" "
            f"--account-id {_shell_quote(account_id)} "
            f"--participant {_shell_quote(participant)} --save-key",
            "python -m collector preview",
            "python -m collector once",
        ]
    )
    unix_curl = (
        "curl -fsSL "
        f"-H {_shell_quote('Authorization: Bearer ' + token)} "
        f"{_shell_quote(url + '/api/v1/onboarding/install.sh?os=unix')} | sh"
    )
    windows = "\n".join(
        [
            f"$env:TOKEN_RANKINGS_API_KEY = {_ps_quote(token)}",
            f"$env:RANKINGS_URL = {_ps_quote(url)}",
            "python -m collector install --url $env:RANKINGS_URL "
            f"--account-id {_ps_quote(account_id)} "
            f"--participant {_ps_quote(participant)} --save-key",
            "python -m collector preview",
            "python -m collector once",
        ]
    )
    return {"unix": unix_direct, "unix_curl": unix_curl, "windows": windows}


def render_install_script(
    *,
    os_name: str,
    base_url: str,
    account_id: str,
    participant: str,
    token: str,
) -> str:
    url = base_url.rstrip("/")
    if os_name == "windows":
        return "\n".join(
            [
                "# Token 消耗排行榜 · 本机接入",
                "# 只上报 Token 数字。不上传代码、对话、文件、密钥、截图。",
                "# 勿转发：下面含个人上报密钥。",
                "# ProCode 不解密、不打开 sqlite、不读钥匙串、不启动 canoryn。",
                f"$env:TOKEN_RANKINGS_API_KEY = {_ps_quote(token)}",
                f"$env:RANKINGS_URL = {_ps_quote(url)}",
                f"$env:COLLECTOR_ACCOUNT_ID = {_ps_quote(account_id)}",
                f"$env:COLLECTOR_PARTICIPANT = {_ps_quote(participant)}",
                "python -m collector install --url $env:RANKINGS_URL "
                "--account-id $env:COLLECTOR_ACCOUNT_ID "
                "--participant $env:COLLECTOR_PARTICIPANT --save-key",
                "python -m collector preview",
                "python -m collector once",
                "",
            ]
        )
    return "\n".join(
        [
            "#!/bin/sh",
            "# Token 消耗排行榜 · 本机接入",
            "# 只上报 Token 数字。不上传代码、对话、文件、密钥、截图。",
            "# 勿转发：下面含个人上报密钥。",
            "# ProCode 不解密、不打开 sqlite、不读钥匙串、不启动 canoryn。",
            "set -eu",
            f"export TOKEN_RANKINGS_API_KEY={_shell_quote(token)}",
            f"export RANKINGS_URL={_shell_quote(url)}",
            f"export COLLECTOR_ACCOUNT_ID={_shell_quote(account_id)}",
            f"export COLLECTOR_PARTICIPANT={_shell_quote(participant)}",
            'cd "$(dirname "$0")" 2>/dev/null || true',
            "python -m collector install --url \"$RANKINGS_URL\" "
            "--account-id \"$COLLECTOR_ACCOUNT_ID\" "
            "--participant \"$COLLECTOR_PARTICIPANT\" --save-key",
            "python -m collector preview",
            "python -m collector once",
            "",
        ]
    )


def _placeholder_commands() -> dict[str, str]:
    text = (
        "echo '请先在页面右上角粘贴用户密钥，再回到「如何接入」复制命令。'\n"
        "echo '不要把服务器的 TOKEN_RANKINGS_API_KEY 贴进网页或转发到群里。'"
    )
    return {"unix": text, "unix_curl": text, "windows": text}


class PrefsIn(BaseModel):
    visibility: Literal["company", "department", "self"] = "company"
    org_tag: Optional[str] = Field(None, max_length=64)
    department: Optional[str] = Field(None, max_length=64)
    timezone: str = DEFAULT_TZ
    compare_timezone: Optional[str] = None
    show_rank: bool = True


class DevicePatch(BaseModel):
    name: Optional[str] = Field(None, max_length=64)
    status: Optional[Literal["active", "hidden", "disabled"]] = None


class FeedbackIn(BaseModel):
    tool_name: str = Field("", max_length=64)
    note: str = Field("", max_length=500)
    structure_text: str = Field("", max_length=2000)


def _clean(value: Optional[str], limit: int) -> Optional[str]:
    if value is None:
        return None
    text = str(value).strip()
    if not text:
        return None
    return text[:limit]


@router.get("/api/v1/onboarding")
def get_onboarding(
    request: Request,
    account_id: Optional[str] = Query(None),
    principal: Optional[Principal] = Depends(optional_principal),
) -> dict[str, Any]:
    """Install commands. A personal token is minted only for a signed-in user."""
    base = str(request.base_url).rstrip("/")
    participant = "我"
    token_ready = False
    commands = _placeholder_commands()
    resolved: Optional[str] = None
    if principal is not None:
        if principal.role == "admin":
            resolved = (account_id or principal.account_id or "").strip() or None
        else:
            resolved = _account_for(principal, account_id, required=False)
        if resolved:
            participant = principal.name or resolved
            token = mint_collector_token(resolved)
            commands = build_commands(
                base_url=base,
                account_id=resolved,
                participant=participant,
                token=token,
            )
            token_ready = True
    ai_prompt = AI_INSTALL_INTRO + "\n安装命令：\n" + commands["unix"]
    return {
        "account_id": resolved,
        "token_ready": token_ready,
        "warning": SECRET_WARNING,
        "privacy_note": (
            "命令只在你的电脑上运行本仓库的采集器。"
            "它上报 Token 数字，不上传代码、对话、文件、密钥或截图。"
            "这不是公开榜，也没有城市榜。"
        ),
        "commands": commands,
        "ai_prompt": ai_prompt,
        "steps": [
            "选择 macOS / Linux 或 Windows",
            "在仓库根目录粘贴命令。先预览，再上报。",
            "回到本页点「再检查连接」，看上次上报时间。",
        ],
    }


@router.get("/api/v1/onboarding/install.sh")
def install_script(
    request: Request,
    os_name: str = Query("unix", alias="os"),
    authorization: Optional[str] = Header(None),
    x_api_key: Optional[str] = Header(None, alias="X-API-Key"),
) -> Response:
    presented = extract_presented_key(authorization, x_api_key)
    if not presented:
        raise HTTPException(
            status_code=401,
            detail={"error": "missing_api_key", "message": "安装脚本需要个人上报密钥或用户密钥。"},
        )
    scope = match_collector_token(presented)
    principal: Optional[Principal] = None
    if scope is None:
        principal = resolve_principal(presented)
        if principal is None:
            raise HTTPException(
                status_code=401,
                detail={"error": "invalid_api_key", "message": "密钥不正确。"},
            )
        account_id = principal.account_id
        if not account_id:
            raise HTTPException(
                status_code=400,
                detail={"error": "account_id_required", "message": "管理员请用个人上报密钥，或先指定账号。"},
            )
        participant = principal.name or account_id
        token = mint_collector_token(account_id)
    else:
        account_id = scope.account_id
        participant = account_id
        token = presented
    script = render_install_script(
        os_name="windows" if os_name == "windows" else "unix",
        base_url=str(request.base_url).rstrip("/"),
        account_id=account_id,
        participant=participant,
        token=token,
    )
    media = "text/plain; charset=utf-8"
    return Response(content=script, media_type=media)


@router.get("/api/v1/onboarding/status")
def onboarding_status(
    account_id: Optional[str] = Query(None),
    principal: Optional[Principal] = Depends(optional_principal),
) -> dict[str, Any]:
    account = _account_for(principal, account_id)
    with db() as conn:
        row = conn.execute(
            """
            SELECT COUNT(*) AS events, MAX(ts) AS last_ts
            FROM usage_events
            WHERE account_id = ? AND COALESCE(source, '') != 'seed'
            """,
            (account,),
        ).fetchone()
    events = int(row["events"] or 0) if row else 0
    last_ts = row["last_ts"] if row else None
    age_hours = None
    if last_ts:
        try:
            seen = datetime.fromisoformat(str(last_ts).replace("Z", "+00:00"))
            if seen.tzinfo is None:
                seen = seen.replace(tzinfo=timezone.utc)
            age_hours = (_now() - seen.astimezone(timezone.utc)).total_seconds() / 3600
        except ValueError:
            age_hours = None
    if not last_ts:
        connected = False
        message = "还没连上。请在仓库根目录运行安装命令，然后再检查一次。"
    elif age_hours is not None and age_hours > 36:
        connected = False
        message = "上次上报已超过 36 小时。看一下定时任务是否还在跑。"
    else:
        connected = True
        message = "已收到上报。"
    label = None
    if last_ts:
        try:
            seen = datetime.fromisoformat(str(last_ts).replace("Z", "+00:00"))
            if seen.tzinfo is None:
                seen = seen.replace(tzinfo=timezone.utc)
            local = seen.astimezone(ZoneInfo(DEFAULT_TZ))
            label = local.strftime("%Y-%m-%d %H:%M") + "（北京时间）"
        except ValueError:
            label = str(last_ts)
    return {
        "account_id": account,
        "connected": connected,
        "last_report_at": last_ts,
        "last_report_label": label,
        "event_count": events,
        "message": message,
    }


@router.get("/api/v1/privacy")
def privacy_dictionary() -> dict[str, Any]:
    from collector.payload import ALLOWED_KEYS

    sample = {
        "model": "example-model",
        "prompt_tokens": 0,
        "completion_tokens": 0,
        "cache_read_tokens": 0,
        "cache_write_tokens": 0,
        "reasoning_tokens": 0,
        "total_tokens": 0,
        "account_id": "your-account",
        "participant": "显示名",
        "team": "部门",
        "project_id": "目录名",
        "tool": "cursor",
        "source": "cursor",
        "request_id": "用来去重的 id",
        "timestamp": "2026-01-01T00:00:00+00:00",
    }
    return {
        "title": "会上报什么",
        "rows": PRIVACY_ROWS,
        "dry_run": {
            "commands": DRY_RUN_COMMANDS,
            "note": (
                "preview 和 once --dry-run 只在你的电脑上打印将要上报的数字，不会发送。"
                "排行榜服务器不会去读你的家目录。"
                "python -m collector preview --fixtures 用仓库里的样例，同样不读家目录。"
            ),
        },
        "sample_event": sample,
        "allowed_keys": sorted(ALLOWED_KEYS),
        "ai_audit_prompt": AI_AUDIT_PROMPT,
        "procode": "ProCode 仍是占位：不解密、不打开 sqlite、不读钥匙串、不启动 canoryn。只读取已经放在那儿的 usage json。",
    }


def _parse_ts(value: Any) -> Optional[datetime]:
    if not value:
        return None
    try:
        seen = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    if seen.tzinfo is None:
        seen = seen.replace(tzinfo=timezone.utc)
    return seen.astimezone(timezone.utc)


def _window_bounds(window: str, tz_name: str) -> tuple[Optional[datetime], Optional[datetime]]:
    tz = _zone(tz_name)
    now_local = datetime.now(tz)
    start_today = now_local.replace(hour=0, minute=0, second=0, microsecond=0)
    key = {
        "today": "today",
        "1d": "today",
        "今日": "today",
        "昨天": "yesterday",
        "yesterday": "yesterday",
        "昨日": "yesterday",
        "3d": "3d",
        "近3天": "3d",
        "7d": "7d",
        "近7天": "7d",
        "30d": "30d",
        "近30天": "30d",
        "90d": "90d",
        "近90天": "90d",
        "all": "all",
        "全部": "all",
    }.get(window.strip().lower(), window.strip().lower())
    if key == "all":
        return None, None
    if key == "today":
        return start_today.astimezone(timezone.utc), (start_today + timedelta(days=1)).astimezone(timezone.utc)
    if key == "yesterday":
        start = start_today - timedelta(days=1)
        return start.astimezone(timezone.utc), start_today.astimezone(timezone.utc)
    days = {"3d": 3, "7d": 7, "30d": 30, "90d": 90}.get(key)
    if days is None:
        raise HTTPException(
            status_code=400,
            detail={"error": "bad_window", "message": "时间范围用 今日 / 昨日 / 近3天 / 近7天 / 近30天 / 近90天 / 全部。"},
        )
    end = _now()
    return end - timedelta(days=days), None


def _sum_between(account: str, start: Optional[datetime], end: Optional[datetime]) -> int:
    clauses = ["account_id = ?", "COALESCE(source, '') != 'seed'"]
    params: list[Any] = [account]
    if start is not None:
        clauses.append("ts >= ?")
        params.append(start.isoformat())
    if end is not None:
        clauses.append("ts < ?")
        params.append(end.isoformat())
    with db() as conn:
        row = conn.execute(
            f"SELECT COALESCE(SUM(total_tokens), 0) AS tokens FROM usage_events WHERE {' AND '.join(clauses)}",
            params,
        ).fetchone()
    return int(row["tokens"] or 0) if row else 0


def _fetch_events(account: str, start: Optional[datetime], end: Optional[datetime]) -> list[dict[str, Any]]:
    clauses = ["account_id = ?", "COALESCE(source, '') != 'seed'"]
    params: list[Any] = [account]
    if start is not None:
        clauses.append("ts >= ?")
        params.append(start.isoformat())
    if end is not None:
        clauses.append("ts < ?")
        params.append(end.isoformat())
    sql = f"""
        SELECT ts, model, tool, request_id,
               prompt_tokens, completion_tokens,
               cache_read_tokens, cache_write_tokens, total_tokens
        FROM usage_events
        WHERE {' AND '.join(clauses)}
        ORDER BY ts ASC
    """
    with db() as conn:
        rows = conn.execute(sql, params).fetchall()
    return [dict(r) for r in rows]


def _heatmap(account: str, tz_name: str, weeks: int = 27) -> list[dict[str, Any]]:
    tz = _zone(tz_name)
    today = datetime.now(tz).replace(hour=0, minute=0, second=0, microsecond=0)
    monday = today - timedelta(days=today.weekday())
    start = monday - timedelta(weeks=weeks - 1)
    since = (start - timedelta(days=1)).astimezone(timezone.utc)
    rows = _fetch_events(account, since, None)
    buckets: dict[str, int] = {}
    for row in rows:
        seen = _parse_ts(row.get("ts"))
        if seen is None:
            continue
        key = seen.astimezone(tz).date().isoformat()
        buckets[key] = buckets.get(key, 0) + int(row.get("total_tokens") or 0)
    days: list[dict[str, Any]] = []
    cursor = start
    end = monday + timedelta(days=6)
    while cursor <= end:
        key = cursor.date().isoformat()
        days.append(
            {
                "date": key,
                "tokens": buckets.get(key, 0),
                "weekday": cursor.weekday(),
                "future": cursor.date() > today.date(),
            }
        )
        cursor += timedelta(days=1)
    positive = sorted(d["tokens"] for d in days if d["tokens"] > 0)
    def level(tokens: int, future: bool) -> int:
        if future or tokens <= 0 or not positive:
            return 0
        rank = sum(1 for value in positive if value <= tokens) / len(positive)
        if rank > 0.85:
            return 4
        if rank > 0.6:
            return 3
        if rank > 0.35:
            return 2
        return 1
    for day in days:
        day["level"] = level(day["tokens"], day["future"])
    return days


def _streak(days: list[dict[str, Any]], tz_name: str) -> int:
    tz = _zone(tz_name)
    today = datetime.now(tz).date().isoformat()
    by_date = {d["date"]: d["tokens"] for d in days}
    cursor = datetime.now(tz).date()
    if by_date.get(today, 0) <= 0:
        cursor = cursor - timedelta(days=1)
    streak = 0
    while by_date.get(cursor.isoformat(), 0) > 0:
        streak += 1
        cursor = cursor - timedelta(days=1)
        if streak > 400:
            break
    return streak


def _anomaly(daily: dict[str, int], today_key: str) -> dict[str, Any]:
    today_tokens = int(daily.get(today_key) or 0)
    prior_keys = sorted(k for k in daily if k < today_key and daily[k] > 0)[-14:]
    prior = [daily[k] for k in prior_keys]
    if len(prior) < 3 or today_tokens <= 0:
        return {
            "status": "ok",
            "label": "正常",
            "reason": "对照天数还不够，或今天还没有用量。",
            "today_tokens": today_tokens,
        }
    ordered = sorted(prior)
    median = ordered[len(ordered) // 2]
    if median > 0 and today_tokens >= median * 3 and today_tokens >= 50_000:
        return {
            "status": "review",
            "label": "异常用量待审",
            "reason": (
                f"今天的用量约是近两周中位数的 {today_tokens / median:.1f} 倍。"
                "请费用负责人核对是不是集中任务，不要直接当成扣费。"
            ),
            "today_tokens": today_tokens,
            "baseline_median_tokens": median,
        }
    return {
        "status": "ok",
        "label": "正常",
        "reason": "没有超过近两周中位数的 3 倍。",
        "today_tokens": today_tokens,
        "baseline_median_tokens": median,
    }


def _rank(account: str, tokens: int, pref: dict[str, Any]) -> dict[str, Any]:
    if not pref.get("show_rank") or pref["visibility"] == VISIBILITY_SELF:
        return {
            "rank": None,
            "scope": pref["visibility_label"],
            "label": "未参加企业榜" if pref["visibility"] == VISIBILITY_SELF else "已关闭排名显示",
            "population": 0,
        }
    start, end = _window_bounds("today", pref["timezone"])
    clauses = ["COALESCE(source, '') != 'seed'", "COALESCE(TRIM(account_id), '') != ''"]
    params: list[Any] = []
    if start is not None:
        clauses.append("ts >= ?")
        params.append(start.isoformat())
    if end is not None:
        clauses.append("ts < ?")
        params.append(end.isoformat())
    with db() as conn:
        rows = conn.execute(
            f"""
            SELECT account_id, COALESCE(SUM(total_tokens), 0) AS tokens
            FROM usage_events
            WHERE {' AND '.join(clauses)}
            GROUP BY account_id
            """,
            params,
        ).fetchall()
    mine_team = pref.get("department") or _dominant_team(account)
    pool: list[tuple[str, int]] = []
    prefs = _all_prefs()
    for row in rows:
        acct = str(row["account_id"])
        other = prefs.get(acct) or _default_prefs(acct)
        if other["visibility"] == VISIBILITY_SELF and acct != account:
            continue
        if pref["visibility"] == VISIBILITY_COMPANY and other["visibility"] != VISIBILITY_COMPANY and acct != account:
            continue
        if pref["visibility"] == VISIBILITY_DEPARTMENT and acct != account:
            their = other.get("department") or _dominant_team(acct)
            if not mine_team or their != mine_team:
                continue
        pool.append((acct, int(row["tokens"] or 0)))
    if not any(acct == account for acct, _ in pool):
        pool.append((account, tokens))
    higher = sum(1 for _acct, value in pool if value > tokens)
    scope = "全公司" if pref["visibility"] == VISIBILITY_COMPANY else "本部门"
    return {
        "rank": higher + 1,
        "scope": scope,
        "label": f"{scope}第 {higher + 1} 名",
        "population": len(pool),
    }


def ensure_devices_from_usage(account: str) -> None:
    with db() as conn:
        rows = conn.execute(
            """
            SELECT COALESCE(NULLIF(TRIM(tool), ''), 'unknown') AS tool,
                   MAX(ts) AS last_ts,
                   COUNT(*) AS reports
            FROM usage_events
            WHERE account_id = ? AND COALESCE(source, '') != 'seed'
            GROUP BY COALESCE(NULLIF(TRIM(tool), ''), 'unknown')
            """,
            (account,),
        ).fetchall()
        for row in rows:
            key = str(row["tool"])
            existing = conn.execute(
                "SELECT id, status, report_count FROM devices WHERE account_id = ? AND device_key = ?",
                (account, key),
            ).fetchone()
            if existing is None:
                conn.execute(
                    """
                    INSERT INTO devices
                    (account_id, device_key, name, status, last_seen, last_tool,
                     report_count, created_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        account,
                        key,
                        tool_display(key),
                        DEVICE_ACTIVE,
                        row["last_ts"],
                        key,
                        int(row["reports"] or 0),
                        _now().isoformat(),
                    ),
                )
            elif str(existing["status"]) != DEVICE_DISABLED:
                conn.execute(
                    """
                    UPDATE devices
                    SET last_seen = ?, last_tool = ?,
                        report_count = CASE
                            WHEN report_count < ? THEN ?
                            ELSE report_count
                        END
                    WHERE id = ?
                    """,
                    (
                        row["last_ts"],
                        key,
                        int(row["reports"] or 0),
                        int(row["reports"] or 0),
                        existing["id"],
                    ),
                )


def list_devices(account: str) -> list[dict[str, Any]]:
    ensure_devices_from_usage(account)
    with db() as conn:
        rows = conn.execute(
            """
            SELECT * FROM devices
            WHERE account_id = ?
            ORDER BY COALESCE(last_seen, '') DESC, id DESC
            """,
            (account,),
        ).fetchall()
    items = []
    for row in rows:
        items.append(
            {
                "id": int(row["id"]),
                "account_id": row["account_id"],
                "device_key": row["device_key"],
                "name": row["name"],
                "status": row["status"],
                "status_label": {
                    DEVICE_ACTIVE: "上报中",
                    DEVICE_HIDDEN: "已隐藏",
                    DEVICE_DISABLED: "已停用",
                }.get(row["status"], row["status"]),
                "last_seen": row["last_seen"],
                "last_tool": row["last_tool"],
                "report_count": int(row["report_count"] or 0),
            }
        )
    return items


def _tz_row(account: str, tz_name: str) -> dict[str, Any]:
    tz = _zone(tz_name)
    now_local = datetime.now(tz)
    start_today = now_local.replace(hour=0, minute=0, second=0, microsecond=0)
    start_week = start_today - timedelta(days=6)
    end = start_today + timedelta(days=1)
    return {
        "timezone": tz_name,
        "label": TIMEZONES[tz_name],
        "civil_date": start_today.date().isoformat(),
        "today_tokens": _sum_between(
            account, start_today.astimezone(timezone.utc), end.astimezone(timezone.utc)
        ),
        "recent_7d_tokens": _sum_between(
            account, start_week.astimezone(timezone.utc), end.astimezone(timezone.utc)
        ),
    }


@router.get("/api/v1/me/dashboard")
def my_dashboard(
    account_id: Optional[str] = Query(None),
    window: str = Query("today"),
    principal: Optional[Principal] = Depends(optional_principal),
) -> dict[str, Any]:
    account = _account_for(principal, account_id)
    pref = get_prefs(account)
    tz_name = pref["timezone"]
    start, end = _window_bounds(window, tz_name)
    rows = _fetch_events(account, start, end)
    tools: dict[str, int] = {}
    models: dict[str, dict[str, int]] = {}
    prompt = completion = cache_read = cache_write = total = 0
    request_ids: set[str] = set()
    for row in rows:
        total += int(row.get("total_tokens") or 0)
        pt = int(row.get("prompt_tokens") or 0)
        ct = int(row.get("completion_tokens") or 0)
        cr = int(row.get("cache_read_tokens") or 0)
        cw = int(row.get("cache_write_tokens") or 0)
        prompt += pt
        completion += ct
        cache_read += cr
        cache_write += cw
        tool = str(row.get("tool") or "unknown")
        tools[tool] = tools.get(tool, 0) + int(row.get("total_tokens") or 0)
        model = str(row.get("model") or "")
        bucket = models.setdefault(
            model, {"tokens": 0, "prompt": 0, "completion": 0, "cache_read": 0, "cache_write": 0}
        )
        bucket["tokens"] += int(row.get("total_tokens") or 0)
        bucket["prompt"] += pt
        bucket["completion"] += ct
        bucket["cache_read"] += cr
        bucket["cache_write"] += cw
        rid = str(row.get("request_id") or "").strip()
        if rid:
            request_ids.add(rid)

    cost = {"input_usd": 0.0, "output_usd": 0.0, "cache_read_usd": 0.0, "cache_write_usd": 0.0, "total_usd": 0.0, "cache_savings_usd": 0.0}
    model_rows = []
    for name, bucket in models.items():
        parts = estimate_usage_parts(
            name, bucket["prompt"], bucket["completion"], bucket["cache_read"], bucket["cache_write"]
        )
        for key in cost:
            cost[key] += float(parts[key])
        model_rows.append(
            {
                "model": name,
                "total_tokens": bucket["tokens"],
                "estimated_cost_usd": parts["total_usd"],
                "price_tier": parts["tier"],
            }
        )
    for key in cost:
        cost[key] = round(cost[key], 6)
    model_rows.sort(key=lambda item: item["total_tokens"], reverse=True)
    for item in model_rows:
        item["share_pct"] = round(item["total_tokens"] / total * 100, 1) if total else 0.0
    tool_rows = []
    for tool, tokens in sorted(tools.items(), key=lambda kv: kv[1], reverse=True):
        tool_rows.append(
            {
                "tool": tool,
                "label": tool_display(tool),
                "total_tokens": tokens,
                "share_pct": round(tokens / total * 100, 1) if total else 0.0,
            }
        )
    part_tokens = {
        "input": prompt,
        "output": completion,
        "cache_read": cache_read,
        "cache_write": cache_write,
    }
    part_sum = sum(part_tokens.values()) or 0
    parts_pct = {
        key: round(value / part_sum * 100, 1) if part_sum else 0.0
        for key, value in part_tokens.items()
    }
    hit_denom = prompt + cache_read
    hit_rate = round(cache_read / hit_denom * 100, 1) if hit_denom else 0.0

    heat = _heatmap(account, tz_name)
    tz = _zone(tz_name)
    older = _fetch_events(account, _now() - timedelta(days=400), None)
    daily: dict[str, int] = {}
    for row in older:
        seen = _parse_ts(row.get("ts"))
        if seen is None:
            continue
        key = seen.astimezone(tz).date().isoformat()
        daily[key] = daily.get(key, 0) + int(row.get("total_tokens") or 0)
    today_key = datetime.now(tz).date().isoformat()
    today_tokens = int(daily.get(today_key) or 0)
    streak_days = 0
    cursor = datetime.now(tz).date()
    if daily.get(cursor.isoformat(), 0) <= 0:
        cursor = cursor - timedelta(days=1)
    while daily.get(cursor.isoformat(), 0) > 0:
        streak_days += 1
        cursor = cursor - timedelta(days=1)
        if streak_days > 400:
            break
    anomaly = _anomaly(daily, today_key)
    devices = list_devices(account)
    active_devices = [d for d in devices if d["status"] == DEVICE_ACTIVE]
    last_seen = None
    for device in devices:
        if device["last_seen"] and (last_seen is None or str(device["last_seen"]) > last_seen):
            last_seen = str(device["last_seen"])
    civil_days: set[str] = set()
    hour_buckets: dict[int, dict[str, Any]] = {
        hour: {"hour": f"{hour:02d}:00", "tokens": 0, "messages": 0, "request_ids": set()}
        for hour in range(24)
    }
    for row in rows:
        seen = _parse_ts(row.get("ts"))
        if seen is None:
            continue
        local = seen.astimezone(tz)
        civil_days.add(local.date().isoformat())
        slot = hour_buckets[local.hour]
        slot["tokens"] += int(row.get("total_tokens") or 0)
        slot["messages"] += 1
        rid = str(row.get("request_id") or "").strip()
        if rid:
            slot["request_ids"].add(rid)
    hourly = []
    for hour in range(23, -1, -1):
        slot = hour_buckets[hour]
        ids = slot.pop("request_ids")
        conversations = len(ids) if ids else slot["messages"]
        hourly.append(
            {
                "hour": slot["hour"],
                "tokens": slot["tokens"],
                "conversations": conversations,
                "messages": slot["messages"],
                "net_reported": slot["tokens"],
            }
        )
    conversations = len(request_ids) if request_ids else len(rows)
    rank = _rank(account, today_tokens, pref)
    compare_rows = [_tz_row(account, tz_name)]
    if pref.get("compare_timezone") and pref["compare_timezone"] != tz_name:
        compare_rows.append(_tz_row(account, pref["compare_timezone"]))
    window_label = {
        "today": "今日",
        "yesterday": "昨日",
        "3d": "近3天",
        "7d": "近7天",
        "30d": "近30天",
        "90d": "近90天",
        "all": "全部",
    }.get(window, window)
    return {
        "account_id": account,
        "window": window,
        "window_label": window_label,
        "generated_at": _now().isoformat(),
        "overview": {
            "total_tokens": total,
            "today_tokens": today_tokens,
            "estimated_cost_usd": cost["total_usd"],
            "cost_basis": "estimate",
            "cost_basis_label": "列表价估算",
            "rank": rank,
            "streak_days": streak_days,
            "device_count": len(active_devices),
            "device_total": len(devices),
            "last_sync": last_seen,
            "org_tag": pref.get("org_tag"),
            "timezone": tz_name,
            "timezone_label": TIMEZONES.get(tz_name, tz_name),
        },
        "heatmap": heat,
        "tools": tool_rows,
        "models": model_rows[:8],
        "cache": {
            "estimated_cost_usd": cost["total_usd"],
            "cache_savings_usd": cost["cache_savings_usd"],
            "cache_hit_rate_pct": hit_rate,
            "parts_tokens": part_tokens,
            "parts_pct": parts_pct,
            "parts_usd": {
                "input": cost["input_usd"],
                "output": cost["output_usd"],
                "cache_read": cost["cache_read_usd"],
                "cache_write": cost["cache_write_usd"],
            },
            "footnote": "列表价估算，不是实扣账单。缓存价没单列时，读按输入价的 10%，写按输入价的 125%。",
        },
        "detail": {
            "conversations": conversations,
            "messages": len(rows),
            "days_with_usage": len(civil_days),
            "hourly": hourly,
            "hourly_note": f"按{TIMEZONES.get(tz_name, tz_name)}的小时汇总。含缓存的数来自事件里的 total_tokens。上报净增就是这一小时新记上的 Token。",
        },
        "timezones": compare_rows,
        "anomaly": anomaly,
        "formula_zh": FORMULA_ZH,
        "leaderboard_zh": LEADERBOARD_COST_ZH,
        "empty": len(rows) == 0,
    }


@router.get("/api/v1/me/prefs")
def read_prefs(
    account_id: Optional[str] = Query(None),
    principal: Optional[Principal] = Depends(optional_principal),
) -> dict[str, Any]:
    account = _account_for(principal, account_id)
    pref = get_prefs(account)
    pref["timezones"] = [{"id": key, "label": label} for key, label in TIMEZONES.items()]
    pref["note"] = (
        "全公司：同事能在企业榜上看到你。本部门：只给同部门的同事看。"
        "仅自己：企业榜上不出现，管理员仍能核对费用。"
        "组织标签只是备注，不会做城市榜。"
    )
    return pref


@router.put("/api/v1/me/prefs")
def write_prefs(
    body: PrefsIn,
    account_id: Optional[str] = Query(None),
    principal: Optional[Principal] = Depends(optional_principal),
) -> dict[str, Any]:
    user = _require_user(principal)
    account = _account_for(user, account_id)
    _zone(body.timezone)
    compare = _clean(body.compare_timezone, 64)
    if compare:
        _zone(compare)
    org_tag = _clean(body.org_tag, 64)
    department = _clean(body.department, 64)
    with db() as conn:
        conn.execute(
            """
            INSERT INTO account_prefs
            (account_id, visibility, org_tag, department, timezone,
             compare_timezone, show_rank, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(account_id) DO UPDATE SET
                visibility = excluded.visibility,
                org_tag = excluded.org_tag,
                department = excluded.department,
                timezone = excluded.timezone,
                compare_timezone = excluded.compare_timezone,
                show_rank = excluded.show_rank,
                updated_at = excluded.updated_at
            """,
            (
                account,
                body.visibility,
                org_tag,
                department,
                body.timezone,
                compare,
                1 if body.show_rank else 0,
                _now().isoformat(),
            ),
        )
    insert_audit(
        actor_key_hint=user.key_hint,
        actor_name=user.name,
        actor_role=user.role,
        actor_account_id=user.account_id,
        action="prefs.update",
        filters={"account_id": account, "visibility": body.visibility},
        row_count=1,
    )
    return get_prefs(account)


@router.get("/api/v1/devices")
def get_devices(
    account_id: Optional[str] = Query(None),
    principal: Optional[Principal] = Depends(optional_principal),
) -> dict[str, Any]:
    account = _account_for(principal, account_id)
    return {
        "account_id": account,
        "grouping": "每一种已上报的工具算一条。同一种工具的多台电脑会显示在一起，可以改显示名。",
        "items": list_devices(account),
    }


@router.patch("/api/v1/devices/{device_id}")
def patch_device(
    device_id: int,
    body: DevicePatch,
    principal: Optional[Principal] = Depends(optional_principal),
) -> dict[str, Any]:
    user = _require_user(principal)
    with db() as conn:
        row = conn.execute("SELECT * FROM devices WHERE id = ?", (device_id,)).fetchone()
        if row is None:
            raise HTTPException(status_code=404, detail={"error": "not_found", "message": "没有这台设备。"})
        _account_for(user, str(row["account_id"]))
        name = _clean(body.name, 64) if body.name is not None else None
        status = body.status or row["status"]
        if status not in DEVICE_STATUSES:
            raise HTTPException(status_code=400, detail={"error": "bad_status", "message": "状态只能是上报中、隐藏或停用。"})
        conn.execute(
            """
            UPDATE devices
            SET name = COALESCE(?, name), status = ?
            WHERE id = ?
            """,
            (name, status, device_id),
        )
        updated = conn.execute("SELECT * FROM devices WHERE id = ?", (device_id,)).fetchone()
    return {
        "id": int(updated["id"]),
        "name": updated["name"],
        "status": updated["status"],
        "message": {
            DEVICE_ACTIVE: "已恢复上报。",
            DEVICE_HIDDEN: "已从列表里隐藏。用量仍会计入。",
            DEVICE_DISABLED: "已停用。这种工具新的上报会计入跳过，不再累加。",
        }.get(updated["status"], "已保存"),
    }


@router.post("/api/v1/feedback")
def submit_feedback(
    body: FeedbackIn,
    principal: Optional[Principal] = Depends(optional_principal),
) -> dict[str, Any]:
    user = _require_user(principal)
    account = user.account_id or ""
    if user.role == "admin" and not account:
        account = user.name or "admin"
    structure = (body.structure_text or "").strip()
    note = (body.note or "").strip()
    tool_name = (body.tool_name or "").strip()
    if not structure and not note:
        raise HTTPException(
            status_code=400,
            detail={"error": "empty", "message": "写一下工具名和日志结构，例如目录在不在、preview 有几行。不要贴对话。"},
        )
    for text in (structure, note, tool_name):
        if looks_like_secret(text):
            raise HTTPException(
                status_code=400,
                detail={"error": "secrets_forbidden", "message": "不要粘贴密钥、Token 或对话原文。只写目录、行数和任务是否存在。"},
            )
    with db() as conn:
        if conn.__class__.__name__ == "_PgConnection":
            row = conn.execute(
                """
                INSERT INTO feedback
                (account_id, author_name, kind, tool_name, note, structure_text, status, created_at)
                VALUES (?, ?, 'diagnostic', ?, ?, ?, 'open', ?)
                RETURNING id
                """,
                (account, user.name, tool_name[:64], note[:500], structure[:2000], _now().isoformat()),
            ).fetchone()
            feedback_id = int(row["id"])
        else:
            cur = conn.execute(
                """
                INSERT INTO feedback
                (account_id, author_name, kind, tool_name, note, structure_text, status, created_at)
                VALUES (?, ?, 'diagnostic', ?, ?, ?, 'open', ?)
                """,
                (account, user.name, tool_name[:64], note[:500], structure[:2000], _now().isoformat()),
            )
            feedback_id = int(cur.lastrowid)
    insert_audit(
        actor_key_hint=user.key_hint,
        actor_name=user.name,
        actor_role=user.role,
        actor_account_id=user.account_id,
        action="feedback.create",
        filters={"tool_name": tool_name[:64]},
        row_count=1,
    )
    return {"id": feedback_id, "status": "open", "message": "已记下。我们只保存你写的结构说明，不保存对话内容。"}


@router.get("/api/v1/feedback")
def list_feedback(
    principal: Optional[Principal] = Depends(optional_principal),
) -> dict[str, Any]:
    user = _require_user(principal)
    if user.role == "admin":
        sql = "SELECT * FROM feedback ORDER BY id DESC LIMIT 50"
        params: list[Any] = []
    else:
        sql = "SELECT * FROM feedback WHERE account_id = ? ORDER BY id DESC LIMIT 50"
        params = [user.account_id or ""]
    with db() as conn:
        rows = conn.execute(sql, params).fetchall()
    return {
        "items": [
            {
                "id": int(r["id"]),
                "account_id": r["account_id"],
                "tool_name": r["tool_name"],
                "note": r["note"],
                "structure_text": r["structure_text"],
                "status": r["status"],
                "created_at": r["created_at"],
            }
            for r in rows
        ]
    }


@router.get("/api/v1/anomalies")
def list_anomalies(
    principal: Optional[Principal] = Depends(optional_principal),
) -> dict[str, Any]:
    if principal is not None and principal.role == "member":
        accounts = [principal.account_id] if principal.account_id else []
    else:
        with db() as conn:
            rows = conn.execute(
                """
                SELECT DISTINCT account_id FROM usage_events
                WHERE COALESCE(TRIM(account_id), '') != ''
                  AND COALESCE(source, '') != 'seed'
                LIMIT 500
                """
            ).fetchall()
        accounts = [str(r["account_id"]) for r in rows]
    items = []
    for account in accounts:
        if not account:
            continue
        pref = get_prefs(account)
        older = _fetch_events(account, _now() - timedelta(days=40), None)
        tz = _zone(pref["timezone"])
        daily: dict[str, int] = {}
        for row in older:
            seen = _parse_ts(row.get("ts"))
            if seen is None:
                continue
            key = seen.astimezone(tz).date().isoformat()
            daily[key] = daily.get(key, 0) + int(row.get("total_tokens") or 0)
        today_key = datetime.now(tz).date().isoformat()
        found = _anomaly(daily, today_key)
        if found["status"] == "review":
            found["account_id"] = account
            items.append(found)
    return {"rule": ANOMALY_RULE, "items": items}


@router.get("/api/v1/pricing/formula")
def formula() -> dict[str, Any]:
    return pricing_formula()
