"""Low-usage alerts: constructive nudge when AI workflow may not be rebuilt yet."""
from __future__ import annotations

import os
from datetime import datetime, timezone
from typing import Any, Optional

from app.auth import Principal, load_user_directory
from app.db import db

# Constructive copy — not shaming
ALERT_NARRATIVE = (
    "消耗过低可能说明 AI 工作流尚未真正重构。"
    "当大量开发与生产任务真正交给 AI 时，Token 消耗通常会自然上升。"
    "可以把这次提醒当作复盘信号：本周哪些环节还没接上 Cursor / 网关 / Agent？"
)

ENV_WEBHOOK = "LOW_USAGE_WEBHOOK_URL"


def webhook_url() -> Optional[str]:
    url = (os.environ.get(ENV_WEBHOOK) or "").strip()
    return url or None


def _known_accounts(principal: Optional[Principal]) -> list[dict[str, Any]]:
    """Accounts to evaluate. Member → own only; admin/anon → directory + DB."""
    if principal and principal.role == "member":
        own = (principal.account_id or "").strip()
        if not own:
            return []
        name = principal.name or own
        return [{"account_id": own, "name": name, "source": "member"}]

    seen: dict[str, dict[str, Any]] = {}
    try:
        for u in load_user_directory():
            if u.get("role") == "member" and u.get("account_id"):
                aid = str(u["account_id"]).strip()
                if aid and aid not in seen:
                    seen[aid] = {
                        "account_id": aid,
                        "name": u.get("name") or aid,
                        "source": "users",
                    }
    except Exception:  # noqa: BLE001
        pass

    with db() as conn:
        rows = conn.execute(
            """
            SELECT DISTINCT account_id
            FROM usage_events
            WHERE COALESCE(NULLIF(TRIM(account_id), ''), '') != ''
              AND COALESCE(source, '') != 'seed'
            ORDER BY account_id
            """
        ).fetchall()
    for r in rows:
        aid = str(r["account_id"]).strip()
        if aid and aid not in seen:
            seen[aid] = {"account_id": aid, "name": aid, "source": "usage"}
    return list(seen.values())


def _account_tokens(
    account_id: str,
    since_iso: Optional[str],
    until_iso: Optional[str],
) -> dict[str, Any]:
    clauses = [
        "account_id = ?",
        "COALESCE(source, '') != 'seed'",
    ]
    params: list[Any] = [account_id]
    if since_iso:
        clauses.append("ts >= ?")
        params.append(since_iso)
    if until_iso:
        clauses.append("ts < ?")
        params.append(until_iso)
    where = " WHERE " + " AND ".join(clauses)
    with db() as conn:
        row = conn.execute(
            f"""
            SELECT
                COUNT(*) AS requests,
                COALESCE(SUM(total_tokens), 0) AS total_tokens,
                COUNT(DISTINCT NULLIF(TRIM(project_id), '')) AS projects,
                COUNT(DISTINCT NULLIF(TRIM(agent_id), '')) AS agents,
                COUNT(DISTINCT NULLIF(TRIM(tool), '')) AS tools
            FROM usage_events
            {where}
            """,
            params,
        ).fetchone()
    return {
        "requests": int(row["requests"] or 0),
        "total_tokens": int(row["total_tokens"] or 0),
        "projects": int(row["projects"] or 0),
        "agents": int(row["agents"] or 0),
        "tools": int(row["tools"] or 0),
    }


def build_low_usage_alerts(
    *,
    principal: Optional[Principal],
    window: str,
    since_iso: Optional[str],
    until_iso: Optional[str],
    threshold_tokens: int,
) -> dict[str, Any]:
    accounts = _known_accounts(principal)
    alerts: list[dict[str, Any]] = []
    ok_list: list[dict[str, Any]] = []
    for acct in accounts:
        stats = _account_tokens(acct["account_id"], since_iso, until_iso)
        entry = {
            "account_id": acct["account_id"],
            "name": acct.get("name") or acct["account_id"],
            "total_tokens": stats["total_tokens"],
            "requests": stats["requests"],
            "projects": stats["projects"],
            "agents": stats["agents"],
            "tools": stats["tools"],
            "threshold_tokens": threshold_tokens,
            "deficit_tokens": max(threshold_tokens - stats["total_tokens"], 0),
            "level": "low" if stats["total_tokens"] < threshold_tokens else "ok",
            "message": (
                ALERT_NARRATIVE
                if stats["total_tokens"] < threshold_tokens
                else "本窗口消耗已达到或超过阈值，继续保持把工作流交给 AI。"
            ),
        }
        if entry["level"] == "low":
            alerts.append(entry)
        else:
            ok_list.append(entry)

    alerts.sort(key=lambda a: a["total_tokens"])
    ok_list.sort(key=lambda a: a["total_tokens"], reverse=True)
    return {
        "window": window,
        "threshold_tokens": threshold_tokens,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "narrative": ALERT_NARRATIVE,
        "scope": (
            "self"
            if principal and principal.role == "member"
            else "all"
        ),
        "accounts_evaluated": len(accounts),
        "alert_count": len(alerts),
        "ok_count": len(ok_list),
        "alerts": alerts,
        "ok": ok_list,
        "webhook_configured": webhook_url() is not None,
    }


def maybe_post_webhook(payload: dict[str, Any], *, notify: bool) -> dict[str, Any]:
    """If LOW_USAGE_WEBHOOK_URL set and notify=True, POST JSON. Else skip."""
    url = webhook_url()
    if not url:
        return {"sent": False, "skipped": True, "reason": "webhook_not_configured"}
    if not notify:
        return {"sent": False, "skipped": True, "reason": "notify_false"}
    try:
        import httpx
    except ImportError:
        return {"sent": False, "skipped": True, "reason": "httpx_missing"}
    body = {
        "type": "low_usage_alert",
        "narrative": payload.get("narrative"),
        "window": payload.get("window"),
        "threshold_tokens": payload.get("threshold_tokens"),
        "alert_count": payload.get("alert_count"),
        "alerts": payload.get("alerts"),
        "generated_at": payload.get("generated_at"),
    }
    try:
        with httpx.Client(timeout=10.0) as client:
            resp = client.post(url, json=body)
        return {
            "sent": True,
            "skipped": False,
            "status_code": resp.status_code,
            "ok": 200 <= resp.status_code < 300,
        }
    except Exception as exc:  # noqa: BLE001
        return {
            "sent": False,
            "skipped": False,
            "error": str(exc),
        }
