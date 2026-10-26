"""Pilot alerts: idle / low usage, spend spikes, and per-account thresholds.

Copy is constructive and in zh-Hans. High usage is not treated as skill,
and a total is never the only figure — each account carries its projects.
"""
from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from typing import Any, Optional

from app.auth import Principal, load_user_directory
from app.credibility import UNLABELED_PROJECT
from app.db import db
from app.pricing import estimate_cost

# Constructive copy — not shaming
ALERT_NARRATIVE = (
    "消耗过低可能说明 AI 工作流尚未真正重构。"
    "当大量开发与生产任务真正交给 AI 时，Token 消耗通常会自然上升。"
    "可以把这次提醒当作复盘信号：本周哪些环节还没接上 Cursor / 网关 / Agent？"
)

ENV_WEBHOOK = "LOW_USAGE_WEBHOOK_URL"
ENV_THRESHOLD_TOKENS = "LOW_USAGE_THRESHOLD_TOKENS"
ENV_SPIKE_RATIO = "ALERT_SPEND_SPIKE_RATIO"
ENV_SPIKE_MIN_USD = "ALERT_SPEND_SPIKE_MIN_USD"
ENV_SPEND_THRESHOLD = "ALERT_SPEND_THRESHOLD_USD"
ENV_ACCOUNT_THRESHOLDS = "ALERT_ACCOUNT_THRESHOLDS"

CONFIGURE_ZH = (
    "试点提醒在服务端用环境变量配置，改完需要重启。"
    "LOW_USAGE_THRESHOLD_TOKENS：用量低于该 Token 数则提醒，默认 10000。"
    "ALERT_SPEND_SPIKE_RATIO：本窗口估算费用相对上一等长窗口的倍数，默认 2。"
    "ALERT_SPEND_SPIKE_MIN_USD：低于该美元金额不报突增，默认 1。"
    "ALERT_SPEND_THRESHOLD_USD：单个账号费用上限（美元）；不设置时，"
    "只在接口传入 spend_threshold_usd，或在 ALERT_ACCOUNT_THRESHOLDS 里按账号写 spend_usd 时生效。"
    "ALERT_ACCOUNT_THRESHOLDS：JSON，键是 account_id，"
    '例如 {"acme-prod":{"min_tokens":5000,"spend_usd":20}}。'
    "通知地址仍是 LOW_USAGE_WEBHOOK_URL；请求加上 notify=true 才会 POST。"
    "成员密钥只评估自己的账号。费用是估算或已入库的 cost_usd，不是发票。"
)

PILOT_NARRATIVE = (
    "用量高不等于会用；用量过低或长期为零，往往说明 AI 工作流还没接到日常工作里。"
    "费用突增要按账号和项目拆开看，不要只看一个总数。"
    "这些是复盘信号，不是人事考核，也不是发票。"
)


def webhook_url() -> Optional[str]:
    url = (os.environ.get(ENV_WEBHOOK) or "").strip()
    return url or None


def _env_float(name: str, default: float) -> float:
    raw = (os.environ.get(name) or "").strip()
    if not raw:
        return default
    try:
        return float(raw)
    except ValueError:
        return default


def _env_int(name: str, default: int) -> int:
    raw = (os.environ.get(name) or "").strip()
    if not raw:
        return default
    try:
        return int(float(raw))
    except ValueError:
        return default


def _env_optional_float(name: str) -> Optional[float]:
    raw = (os.environ.get(name) or "").strip()
    if not raw:
        return None
    try:
        value = float(raw)
    except ValueError:
        return None
    if value <= 0:
        return None
    return value


def account_threshold_overrides() -> tuple[dict[str, dict[str, float]], Optional[str]]:
    """Per-account min_tokens / spend_usd. Invalid JSON does not crash alerts."""
    raw = (os.environ.get(ENV_ACCOUNT_THRESHOLDS) or "").strip()
    if not raw:
        return {}, None
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        return {}, "ALERT_ACCOUNT_THRESHOLDS 不是合法 JSON，已忽略"
    if not isinstance(data, dict):
        return {}, "ALERT_ACCOUNT_THRESHOLDS 必须是 JSON 对象，已忽略"
    out: dict[str, dict[str, float]] = {}
    for key, value in data.items():
        if not isinstance(value, dict):
            continue
        item: dict[str, float] = {}
        if "min_tokens" in value:
            try:
                item["min_tokens"] = float(int(value["min_tokens"]))
            except (TypeError, ValueError):
                pass
        if "spend_usd" in value:
            try:
                item["spend_usd"] = float(value["spend_usd"])
            except (TypeError, ValueError):
                pass
        if item:
            out[str(key).strip()] = item
    return out, None


def _resolve_spend_threshold(passed: Optional[float]) -> Optional[float]:
    if passed is not None:
        return float(passed) if float(passed) > 0 else None
    return _env_optional_float(ENV_SPEND_THRESHOLD)


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


def _group_cost(row: Any) -> float:
    requests = int(row["requests"] or 0)
    priced_rows = int(row["cost_usd_rows"] or 0)
    if requests > 0 and priced_rows == requests:
        return float(row["cost_usd_sum"] or 0)
    return estimate_cost(
        row["model"], int(row["prompt_tokens"] or 0), int(row["completion_tokens"] or 0)
    )


def _usage_rollup(
    since_iso: Optional[str],
    until_iso: Optional[str],
) -> dict[str, dict[str, Any]]:
    """Non-seed usage in the window, split by account and project."""
    clauses = [
        "COALESCE(source, '') != 'seed'",
        "COALESCE(NULLIF(TRIM(account_id), ''), '') != ''",
    ]
    params: list[Any] = []
    if since_iso:
        clauses.append("ts >= ?")
        params.append(since_iso)
    if until_iso:
        clauses.append("ts < ?")
        params.append(until_iso)
    where = " WHERE " + " AND ".join(clauses)
    with db() as conn:
        rows = conn.execute(
            f"""
            SELECT
                TRIM(account_id) AS account_id,
                COALESCE(NULLIF(TRIM(project_id), ''), '{UNLABELED_PROJECT}') AS project_id,
                model,
                COALESCE(SUM(prompt_tokens), 0) AS prompt_tokens,
                COALESCE(SUM(completion_tokens), 0) AS completion_tokens,
                COALESCE(SUM(total_tokens), 0) AS total_tokens,
                COALESCE(SUM(cost_usd), 0) AS cost_usd_sum,
                SUM(CASE WHEN cost_usd IS NOT NULL THEN 1 ELSE 0 END) AS cost_usd_rows,
                COUNT(*) AS requests
            FROM usage_events
            {where}
            GROUP BY TRIM(account_id),
                     COALESCE(NULLIF(TRIM(project_id), ''), '{UNLABELED_PROJECT}'),
                     model
            """,
            params,
        ).fetchall()
        counts = conn.execute(
            f"""
            SELECT
                TRIM(account_id) AS account_id,
                COUNT(DISTINCT NULLIF(TRIM(agent_id), '')) AS agents,
                COUNT(DISTINCT NULLIF(TRIM(tool), '')) AS tools
            FROM usage_events
            {where}
            GROUP BY TRIM(account_id)
            """,
            params,
        ).fetchall()
    agents_tools = {
        str(r["account_id"]): (int(r["agents"] or 0), int(r["tools"] or 0)) for r in counts
    }
    out: dict[str, dict[str, Any]] = {}
    for row in rows:
        aid = str(row["account_id"])
        bucket = out.setdefault(
            aid,
            {
                "requests": 0,
                "total_tokens": 0,
                "estimated_cost_usd": 0.0,
                "projects": {},
            },
        )
        tokens = int(row["total_tokens"] or 0)
        requests = int(row["requests"] or 0)
        cost = _group_cost(row)
        bucket["requests"] += requests
        bucket["total_tokens"] += tokens
        bucket["estimated_cost_usd"] += cost
        proj = bucket["projects"].setdefault(
            row["project_id"],
            {"project_id": row["project_id"], "requests": 0, "total_tokens": 0, "estimated_cost_usd": 0.0},
        )
        proj["requests"] += requests
        proj["total_tokens"] += tokens
        proj["estimated_cost_usd"] += cost
    for aid, bucket in out.items():
        agents, tools = agents_tools.get(aid, (0, 0))
        bucket["agents"] = agents
        bucket["tools"] = tools
        splits = sorted(
            bucket["projects"].values(),
            key=lambda item: item["total_tokens"],
            reverse=True,
        )
        for item in splits:
            item["estimated_cost_usd"] = round(float(item["estimated_cost_usd"]), 6)
        bucket["projects"] = splits
        bucket["estimated_cost_usd"] = round(float(bucket["estimated_cost_usd"]), 6)
    return out


def _usd(value: float) -> str:
    return f"${value:.2f}"


def build_pilot_alerts(
    *,
    principal: Optional[Principal],
    window: str,
    since_iso: Optional[str],
    until_iso: Optional[str],
    previous_since_iso: Optional[str],
    previous_until_iso: Optional[str],
    spike_comparable: bool,
    threshold_tokens: Optional[int] = None,
    spend_threshold_usd: Optional[float] = None,
    spike_ratio: Optional[float] = None,
    spike_min_usd: Optional[float] = None,
) -> dict[str, Any]:
    """Idle, low-usage, spend-spike, and per-account spend rules.

    Members are limited to their own account. Each account includes project
    splits so the result is not only a total.
    """
    tokens_threshold = (
        int(threshold_tokens)
        if threshold_tokens is not None
        else _env_int(ENV_THRESHOLD_TOKENS, 10000)
    )
    ratio = float(spike_ratio) if spike_ratio is not None else _env_float(ENV_SPIKE_RATIO, 2.0)
    if ratio <= 0:
        ratio = 2.0
    spike_floor = (
        float(spike_min_usd)
        if spike_min_usd is not None
        else _env_float(ENV_SPIKE_MIN_USD, 1.0)
    )
    if spike_floor < 0:
        spike_floor = 0.0
    global_spend = _resolve_spend_threshold(spend_threshold_usd)
    overrides, override_warning = account_threshold_overrides()
    current = _usage_rollup(since_iso, until_iso)
    previous = (
        _usage_rollup(previous_since_iso, previous_until_iso) if spike_comparable else {}
    )
    spend_rule_enabled = global_spend is not None or any(
        "spend_usd" in item for item in overrides.values()
    )

    accounts = _known_accounts(principal)
    alerts: list[dict[str, Any]] = []
    by_account: list[dict[str, Any]] = []
    for acct in accounts:
        aid = acct["account_id"]
        stats = current.get(aid) or {
            "requests": 0,
            "total_tokens": 0,
            "estimated_cost_usd": 0.0,
            "projects": [],
            "agents": 0,
            "tools": 0,
        }
        prev_cost = float((previous.get(aid) or {}).get("estimated_cost_usd") or 0.0)
        override = overrides.get(aid) or {}
        account_tokens_threshold = int(override.get("min_tokens", tokens_threshold))
        if "spend_usd" in override:
            account_spend = float(override["spend_usd"])
        else:
            account_spend = global_spend
        projects = list(stats.get("projects") or [])
        rule_ids: list[str] = []
        tokens = int(stats.get("total_tokens") or 0)
        cost = float(stats.get("estimated_cost_usd") or 0.0)
        common = {
            "account_id": aid,
            "name": acct.get("name") or aid,
            "total_tokens": tokens,
            "requests": int(stats.get("requests") or 0),
            "projects": projects,
            "project_count": len(projects),
            "agents": int(stats.get("agents") or 0),
            "tools": int(stats.get("tools") or 0),
            "estimated_cost_usd": round(cost, 6),
            "previous_estimated_cost_usd": round(prev_cost, 6),
        }
        if tokens <= 0:
            rule_ids.append("idle")
            alerts.append(
                {
                    **common,
                    "rule": "idle",
                    "level": "idle",
                    "label": "闲置",
                    "message": (
                        "这个账号在本窗口没有用量。长期为零通常说明 AI 工作流还没接到日常工作里，"
                        "而不只是少花了钱。请核对采集器是否按账号和项目分开上报。"
                    ),
                }
            )
        elif tokens < account_tokens_threshold:
            rule_ids.append("low_usage")
            alerts.append(
                {
                    **common,
                    "rule": "low_usage",
                    "level": "low",
                    "label": "用量过低",
                    "threshold_tokens": account_tokens_threshold,
                    "deficit_tokens": account_tokens_threshold - tokens,
                    "message": (
                        f"本窗口 {tokens} Tokens，低于阈值 {account_tokens_threshold}。"
                        "用量偏低往往说明关键环节还没交给 AI。"
                        "请按下面的项目拆开看，而不是只看账号合计。"
                    ),
                }
            )
        if spike_comparable and cost + 1e-9 >= spike_floor and (
            prev_cost <= 0 or cost >= prev_cost * ratio
        ):
            rule_ids.append("spend_spike")
            if prev_cost > 0:
                shown = round(cost / prev_cost, 2)
                compare = f"约 {shown} 倍（本窗 {_usd(cost)}，上窗 {_usd(prev_cost)}）"
            else:
                shown = None
                compare = f"上窗没有费用，本窗 {_usd(cost)}"
            alerts.append(
                {
                    **common,
                    "rule": "spend_spike",
                    "level": "spike",
                    "label": "费用突增",
                    "spike_ratio": shown,
                    "threshold_ratio": ratio,
                    "message": (
                        f"估算费用相对上一等长窗口{compare}。"
                        "请按账号和项目拆开核对，确认是工作量上升，而不是密钥共用或重复上报。"
                        "费用高不等于使用水平高。"
                    ),
                }
            )
        if account_spend is not None and cost + 1e-9 >= float(account_spend):
            rule_ids.append("account_spend")
            alerts.append(
                {
                    **common,
                    "rule": "account_spend",
                    "level": "spend",
                    "label": "账号费用阈值",
                    "spend_threshold_usd": float(account_spend),
                    "message": (
                        f"本窗口估算费用 {_usd(cost)} 已达到该账号阈值 {_usd(float(account_spend))}。"
                        "请打开项目拆分，看是哪些项目、哪些模型，不要只留一个总数。"
                    ),
                }
            )
        by_account.append({**common, "alert_rules": rule_ids})

    rule_order = {"idle": 0, "low_usage": 1, "spend_spike": 2, "account_spend": 3}
    alerts.sort(key=lambda item: (rule_order.get(item["rule"], 9), item["account_id"]))
    by_account.sort(key=lambda item: item["estimated_cost_usd"], reverse=True)
    rules = [
        {
            "id": "idle",
            "label": "闲置",
            "enabled": True,
            "description": "窗口内 Token 为 0。长期为零通常表示工作流还没接上，而不是省钱。",
        },
        {
            "id": "low_usage",
            "label": "用量过低",
            "enabled": True,
            "threshold_tokens": tokens_threshold,
            "description": "有用量，但低于 Token 阈值。可用查询参数或按账号 min_tokens 覆盖。",
        },
        {
            "id": "spend_spike",
            "label": "费用突增",
            "enabled": spike_comparable,
            "spike_ratio": ratio,
            "spike_min_usd": spike_floor,
            "description": (
                "与上一等长窗口比较。全部时间（all）没有上一窗，不评估突增。"
                if not spike_comparable
                else "本窗口费用达到倍数且不低于最小金额时提醒。"
            ),
        },
        {
            "id": "account_spend",
            "label": "账号费用阈值",
            "enabled": spend_rule_enabled,
            "spend_threshold_usd": global_spend,
            "description": (
                "未设置全局阈值，也没有按账号的 spend_usd。"
                if not spend_rule_enabled
                else "达到该账号的美元阈值时提醒。全局值可被 ALERT_ACCOUNT_THRESHOLDS 覆盖。"
            ),
        },
    ]
    return {
        "window": window,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "narrative": PILOT_NARRATIVE,
        "configure": CONFIGURE_ZH,
        "scope": "self" if principal and principal.role == "member" else "all",
        "accounts_evaluated": len(accounts),
        "alert_count": len(alerts),
        "threshold_tokens": tokens_threshold,
        "spend_threshold_usd": global_spend,
        "spike_ratio": ratio,
        "spike_min_usd": spike_floor,
        "spike_comparable": spike_comparable,
        "webhook_configured": webhook_url() is not None,
        "config_warning": override_warning,
        "rules": rules,
        "alerts": alerts,
        "by_account": by_account,
        "price_note": (
            "没有 cost_usd 的事件按列表价估算。不在价目表里的模型保留原名，"
            "估价用 default 档，FinOps 导出会把 estimate 标成 true。"
        ),
    }


def maybe_post_webhook(
    payload: dict[str, Any],
    *,
    notify: bool,
    event_type: str = "low_usage_alert",
) -> dict[str, Any]:
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
    body: dict[str, Any] = {
        "type": event_type,
        "narrative": payload.get("narrative"),
        "window": payload.get("window"),
        "threshold_tokens": payload.get("threshold_tokens"),
        "alert_count": payload.get("alert_count"),
        "alerts": payload.get("alerts"),
        "generated_at": payload.get("generated_at"),
    }
    if payload.get("rules"):
        body["rules"] = [
            {"id": rule.get("id"), "label": rule.get("label"), "enabled": rule.get("enabled")}
            for rule in payload["rules"]
        ]
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
