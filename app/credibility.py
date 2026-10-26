"""Credibility helpers: cost_basis labels, unlabeled noise, health metrics."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any, Optional

from app.db import db

# Display labels for empty project / account / agent buckets
UNLABELED_PROJECT = "未标注"
UNLABELED_ACCOUNT = "未标注账号"
UNLABELED_AGENT = "未标注"

COST_BASIS_LABELS = {
    "estimate": "估算",
    "cursor_charged": "Cursor 账单",
    "litellm_spend": "LiteLLM spend",
    "invoice": "账单",
    "billed": "账单",
    "mixed": "混合（含估算）",
}


def cost_basis_label(basis: Optional[str]) -> str:
    if not basis:
        return COST_BASIS_LABELS["estimate"]
    return COST_BASIS_LABELS.get(basis, basis)


def summarize_cost_basis(where: str, params: list[Any]) -> dict[str, Any]:
    """Aggregate cost_basis for a filtered event set (non-destructive read)."""
    sql = f"""
        SELECT
            COUNT(*) AS total,
            SUM(CASE WHEN cost_usd IS NOT NULL THEN 1 ELSE 0 END) AS billed_rows,
            SUM(CASE WHEN COALESCE(cost_basis, '') = 'cursor_charged' THEN 1 ELSE 0 END)
                AS cursor_charged,
            SUM(CASE WHEN COALESCE(cost_basis, '') = 'litellm_spend' THEN 1 ELSE 0 END)
                AS litellm_spend,
            SUM(CASE WHEN COALESCE(cost_basis, '') = 'invoice' THEN 1 ELSE 0 END)
                AS invoice,
            SUM(
                CASE
                    WHEN cost_usd IS NOT NULL
                         AND COALESCE(cost_basis, '') NOT IN
                             ('cursor_charged', 'litellm_spend', 'invoice')
                    THEN 1 ELSE 0
                END
            ) AS billed_other
        FROM usage_events
        {where}
    """
    with db() as conn:
        row = conn.execute(sql, params).fetchone()
    total = int(row["total"] or 0) if row else 0
    billed = int(row["billed_rows"] or 0) if row else 0
    estimate = max(total - billed, 0)
    counts = {
        "estimate": estimate,
        "cursor_charged": int(row["cursor_charged"] or 0) if row else 0,
        "litellm_spend": int(row["litellm_spend"] or 0) if row else 0,
        "invoice": int(row["invoice"] or 0) if row else 0,
        "billed_other": int(row["billed_other"] or 0) if row else 0,
    }
    primary = _primary_basis(counts, total, billed)
    return {
        "primary": primary,
        "label": cost_basis_label(primary),
        "counts": counts,
        "billed_rows": billed,
        "estimate_rows": estimate,
        "total_rows": total,
        "note": _basis_note(primary, billed, total),
    }


def _primary_basis(counts: dict[str, int], total: int, billed: int) -> str:
    if total <= 0:
        return "estimate"
    if billed == 0:
        return "estimate"
    if billed < total:
        return "mixed"
    # all billed — pick the dominant billed basis
    billed_keys = ("cursor_charged", "litellm_spend", "invoice", "billed_other")
    dominant = max(billed_keys, key=lambda k: counts.get(k, 0))
    if counts.get(dominant, 0) == billed and dominant != "billed_other":
        return dominant
    if sum(1 for k in billed_keys if counts.get(k, 0) > 0) > 1:
        return "mixed"
    if dominant == "billed_other":
        return "billed"
    return dominant


def _basis_note(primary: str, billed: int, total: int) -> str:
    if total <= 0:
        return "暂无用量"
    if primary == "estimate":
        return "成本均为公开列表价估算，不是云厂商发票或实扣账单"
    if primary == "mixed":
        return f"部分行有账单字段（{billed}/{total}），其余为估算"
    return f"成本来自 {cost_basis_label(primary)}（{billed}/{total} 行）"


def health_credibility(exclude_seed: bool = True) -> dict[str, Any]:
    """Metrics for /health and overview strips."""
    seed_clause = "WHERE COALESCE(source, '') != 'seed'" if exclude_seed else ""
    with db() as conn:
        total = int(
            conn.execute(
                f"SELECT COUNT(*) AS c FROM usage_events {seed_clause}"
            ).fetchone()["c"]
        )
        tools_row = conn.execute(
            f"""
            SELECT COUNT(DISTINCT tool) AS c FROM usage_events
            {seed_clause}
            {"AND" if seed_clause else "WHERE"}
            COALESCE(NULLIF(TRIM(tool), ''), '') != ''
            """
        ).fetchone()
        tools_connected = int(tools_row["c"] or 0)

        agent_row = conn.execute(
            f"""
            SELECT
                SUM(
                    CASE WHEN COALESCE(NULLIF(TRIM(agent_id), ''), '') != ''
                    THEN 1 ELSE 0 END
                ) AS with_agent,
                COUNT(*) AS total
            FROM usage_events
            {seed_clause}
            """
        ).fetchone()
        with_agent = int(agent_row["with_agent"] or 0)
        agent_pct = round(with_agent / total * 100.0, 1) if total else 0.0

        projects_row = conn.execute(
            f"""
            SELECT COUNT(DISTINCT project_id) AS c FROM usage_events
            {seed_clause}
            {"AND" if seed_clause else "WHERE"}
            COALESCE(NULLIF(TRIM(project_id), ''), '') != ''
            """
        ).fetchone()
        active_projects = int(projects_row["c"] or 0)

        accounts_row = conn.execute(
            f"""
            SELECT COUNT(DISTINCT account_id) AS c FROM usage_events
            {seed_clause}
            {"AND" if seed_clause else "WHERE"}
            COALESCE(NULLIF(TRIM(account_id), ''), '') != ''
            """
        ).fetchone()
        active_accounts = int(accounts_row["c"] or 0)

        # Approx concurrency: distinct request_id in last 1h (proxy for parallel work)
        # Use Python-side cutoff so SQLite and Postgres share one dialect.
        since_1h = (datetime.now(timezone.utc) - timedelta(hours=1)).isoformat()
        concurrent_row = conn.execute(
            f"""
            SELECT COUNT(DISTINCT request_id) AS c FROM usage_events
            {seed_clause}
            {"AND" if seed_clause else "WHERE"}
            ts >= ?
            AND COALESCE(NULLIF(TRIM(request_id), ''), '') != ''
            """,
            (since_1h,),
        ).fetchone()
        approx_concurrent = int(concurrent_row["c"] or 0)

        # Also count distinct project_id touched in last 1h
        concurrent_proj = conn.execute(
            f"""
            SELECT COUNT(DISTINCT project_id) AS c FROM usage_events
            {seed_clause}
            {"AND" if seed_clause else "WHERE"}
            ts >= ?
            AND COALESCE(NULLIF(TRIM(project_id), ''), '') != ''
            """,
            (since_1h,),
        ).fetchone()
        active_projects_1h = int(concurrent_proj["c"] or 0)

        tools_list = [
            r["tool"]
            for r in conn.execute(
                f"""
                SELECT DISTINCT tool FROM usage_events
                {seed_clause}
                {"AND" if seed_clause else "WHERE"}
                COALESCE(NULLIF(TRIM(tool), ''), '') != ''
                ORDER BY tool
                """
            ).fetchall()
            if r["tool"]
        ]

    basis = summarize_cost_basis(
        seed_clause if seed_clause else "",
        [],
    )
    return {
        "tools_connected": tools_connected,
        "tools": tools_list,
        "agent_id_coverage_pct": agent_pct,
        "agent_id_events": with_agent,
        "active_projects": active_projects,
        "active_accounts": active_accounts,
        "approx_concurrent_requests_1h": approx_concurrent,
        "active_projects_1h": active_projects_1h,
        "cost_basis": basis,
        "events": total,
    }


def is_unlabeled_name(name: Optional[str]) -> bool:
    if not name:
        return True
    return name in (
        UNLABELED_PROJECT,
        UNLABELED_ACCOUNT,
        UNLABELED_AGENT,
        "未分配",
        "未分配账号",
        "未命名",
        "unknown",
    )
