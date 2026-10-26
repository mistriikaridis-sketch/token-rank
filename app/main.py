"""Token Rankings MVP — FastAPI app."""
from __future__ import annotations

import csv
import io
import os
from collections import Counter
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Literal, Optional

from fastapi import Body, Depends, FastAPI, HTTPException, Query
from fastapi.responses import FileResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from app.auth import (
    Principal,
    api_key_configured,
    apply_account_scope,
    auth_status,
    optional_principal,
    require_admin,
    require_principal,
    require_write_api_key,
    setup_warnings,
)
from app.alerts import build_low_usage_alerts, build_pilot_alerts, maybe_post_webhook
from app.adapters.cursor_admin import extract_cursor_events, map_cursor_payload, map_cursor_payload_with_stats
from app.adapters.claude_code import (
    extract_claude_code_payload,
    map_claude_code_events,
    scan_claude_code_dir,
)
from app.adapters.codex import (
    extract_codex_payload,
    map_codex_events,
    scan_codex_dir,
)
from app.adapters.continue_dev import (
    extract_continue_payload,
    map_continue_events,
    scan_continue_dir,
)
from app.adapters.workbuddy import (
    extract_workbuddy_payload,
    map_workbuddy_events,
    scan_workbuddy_dir,
    scan_workbuddy_dir_with_stats,
)
from app.adapters.procode import (
    ADAPTER_META as PROCODE_ADAPTER_META,
    extract_procode_payload,
    map_procode_events,
    map_procode_events_with_stats,
)
from app.db import (
    clear_events,
    count_events,
    count_forum_posts,
    database_status,
    db,
    init_db,
    insert_audit,
    insert_forum_post,
    insert_usage_rows,
    like_forum_post,
    list_audit,
    list_forum_posts,
)
from app.ingest import map_records
from app.pricing import estimate_cost, list_priced_models, load_pricing, price_tier
from app.seed import seed
from app.forum import (
    BODY_MAX,
    TITLE_MAX,
    forum_disclaimer,
    seed_forum_if_empty,
    validate_post_content,
)
from app.tools import normalize_tool, tool_display, tool_labels_map
from app.credibility import (
    UNLABELED_ACCOUNT,
    UNLABELED_AGENT,
    UNLABELED_PROJECT,
    cost_basis_label,
    health_credibility,
    is_unlabeled_name,
    summarize_cost_basis,
)

STATIC_DIR = Path(__file__).resolve().parent.parent / "static"
DOCS_DIR = Path(__file__).resolve().parent.parent / "docs"
VERSION_FILE = Path(__file__).resolve().parent.parent / "VERSION"


def read_app_version() -> str:
    try:
        text = VERSION_FILE.read_text(encoding="utf-8").strip()
    except OSError:
        text = ""
    return text or "0.21.0"


APP_VERSION = read_app_version()

app = FastAPI(
    title="Token 消耗排行榜",
    description="企业内 LLM Token / 成本排行榜。不做自研网关，不做公网热度榜。",
    version=APP_VERSION,
)


class UsageIn(BaseModel):
    model: str = Field(..., min_length=1, max_length=128)
    prompt_tokens: int = Field(0, ge=0)
    completion_tokens: int = Field(0, ge=0)
    total_tokens: Optional[int] = Field(None, ge=0)
    team: Optional[str] = Field(None, max_length=128)
    api_key: Optional[str] = Field(None, max_length=256, alias="key")
    tag: Optional[str] = Field(None, max_length=128)
    participant: Optional[str] = Field(None, max_length=128)
    tool: Optional[str] = Field(None, max_length=128)
    account_id: Optional[str] = Field(None, max_length=128)
    project_id: Optional[str] = Field(None, max_length=128)
    agent_id: Optional[str] = Field(None, max_length=128)
    agent_name: Optional[str] = Field(None, max_length=128)
    request_id: Optional[str] = Field(None, max_length=256)
    source: Optional[str] = Field(None, max_length=64)
    timestamp: Optional[str] = None

    model_config = {"populate_by_name": True}


class UsageOut(BaseModel):
    id: int
    model: str
    prompt_tokens: int
    completion_tokens: int
    total_tokens: int
    team: Optional[str]
    participant: Optional[str]
    tool: Optional[str]
    account_id: Optional[str] = None
    project_id: Optional[str] = None
    agent_id: Optional[str] = None
    agent_name: Optional[str] = None
    estimated_cost_usd: float
    ts: str


class RankingRow(BaseModel):
    rank: int
    name: str
    requests: int
    prompt_tokens: int
    completion_tokens: int
    total_tokens: int
    estimated_cost_usd: float
    share_pct: float
    account_id: Optional[str] = None
    project_id: Optional[str] = None
    cost_basis: Optional[str] = None
    cost_basis_label: Optional[str] = None


class DimensionSplit(BaseModel):
    """One account × project cell. Rankings keep these beside any total."""

    account_id: str
    project_id: str
    requests: int = 0
    total_tokens: int = 0
    estimated_cost_usd: float = 0.0


class RankingsResponse(BaseModel):
    window: str
    group_by: str
    generated_at: str
    totals: dict[str, Any]
    items: list[RankingRow]


class PersonRow(BaseModel):
    rank: int
    participant: str
    primary_tool: str
    primary_model: str
    primary_project: Optional[str] = None
    team: Optional[str] = None
    account_id: Optional[str] = None
    account_count: int = 0
    project_count: int = 0
    splits: list[DimensionSplit] = Field(default_factory=list)
    requests: int
    total_tokens: int
    estimated_cost_usd: float
    share_pct: float
    cost_basis: Optional[str] = None
    cost_basis_label: Optional[str] = None


class PeopleRankingsResponse(BaseModel):
    window: str
    generated_at: str
    overview: dict[str, Any]
    totals: dict[str, Any]
    items: list[PersonRow]
    filters: dict[str, Any]


class OrgRow(BaseModel):
    rank: int
    team: str
    total_tokens: int
    per_capita_tokens: float
    participants: int
    requests: int
    estimated_cost_usd: float
    share_pct: float
    account_count: int = 0
    project_count: int = 0
    splits: list[DimensionSplit] = Field(default_factory=list)
    cost_basis: Optional[str] = None
    cost_basis_label: Optional[str] = None


class OrgRankingsResponse(BaseModel):
    window: str
    generated_at: str
    totals: dict[str, Any]
    items: list[OrgRow]
    filters: dict[str, Any] = {}


class ProjectRow(BaseModel):
    rank: int
    project_id: str
    account_id: Optional[str] = None
    label: str = ""
    tool: Optional[str] = None
    participants: int
    requests: int
    total_tokens: int
    estimated_cost_usd: float
    share_pct: float
    cost_basis: Optional[str] = None
    cost_basis_label: Optional[str] = None
    unlabeled: bool = False


class ProjectRankingsResponse(BaseModel):
    window: str
    generated_at: str
    totals: dict[str, Any]
    items: list[ProjectRow]
    filters: dict[str, Any] = {}


class AgentRow(BaseModel):
    rank: int
    agent_id: str
    agent_name: Optional[str] = None
    label: str = ""
    account_id: Optional[str] = None
    team: Optional[str] = None
    tool: Optional[str] = None
    participant: Optional[str] = None
    requests: int
    total_tokens: int
    estimated_cost_usd: float
    share_pct: float
    project_count: int = 0
    projects: list[str] = Field(default_factory=list)
    cost_basis: Optional[str] = None
    cost_basis_label: Optional[str] = None
    unlabeled: bool = False


class AgentRankingsResponse(BaseModel):
    window: str
    generated_at: str
    totals: dict[str, Any]
    items: list[AgentRow]
    filters: dict[str, Any] = {}


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _start_of_day(dt: datetime) -> datetime:
    return dt.astimezone(timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0)


def _parse_window(window: str) -> tuple[Optional[datetime], Optional[datetime]]:
    """Return (since, until) UTC bounds. until=None means open-ended."""
    now = _utc_now()
    today0 = _start_of_day(now)
    w = window.strip().lower()
    aliases = {
        "today": "today",
        "1d": "today",
        "今日": "today",
        "昨天": "yesterday",
        "yesterday": "yesterday",
        "昨日": "yesterday",
        "7d": "7d",
        "近7天": "7d",
        "30d": "30d",
        "近30天": "30d",
        "90d": "90d",
        "all": "all",
        "全部": "all",
        "*": "all",
    }
    key = aliases.get(w, w)
    if key == "all":
        return None, None
    if key == "today":
        return today0, None
    if key == "yesterday":
        return today0 - timedelta(days=1), today0
    if key == "7d":
        return now - timedelta(days=7), None
    if key == "30d":
        return now - timedelta(days=30), None
    if key == "90d":
        return now - timedelta(days=90), None
    raise HTTPException(
        status_code=400,
        detail="unsupported window, use today|yesterday|7d|30d|90d|all",
    )


def _where_ts(
    since: Optional[datetime], until: Optional[datetime]
) -> tuple[str, list[Any]]:
    clauses: list[str] = []
    params: list[Any] = []
    if since is not None:
        clauses.append("ts >= ?")
        params.append(since.isoformat())
    if until is not None:
        clauses.append("ts < ?")
        params.append(until.isoformat())
    if not clauses:
        return "", params
    return " WHERE " + " AND ".join(clauses), params


def _and_filters(
    where: str, params: list[Any], extra: list[tuple[str, Any]]
) -> tuple[str, list[Any]]:
    clauses = [c for c, _ in extra if c]
    values = [v for c, v in extra if c]
    if not clauses:
        return where, params
    joined = " AND ".join(clauses)
    if where:
        return where + " AND " + joined, params + values
    return " WHERE " + joined, params + values




def _seed_filter(include_seed: bool) -> tuple[str, Any]:
    """Default: hide source=seed demo rows from rankings."""
    if include_seed:
        return "", None
    return ("COALESCE(source, '') != ?", "seed")




def _scoped_account(
    principal: Optional[Principal],
    account_id: Optional[str],
) -> Optional[str]:
    """RBAC: member forced to own account_id; admin/anon use request."""
    return apply_account_scope(principal, account_id)


def _dropdown_account(
    principal: Optional[Principal],
    scoped_account_id: Optional[str],
) -> Optional[str]:
    """Member filter lists stay on their account. Admin lists stay complete."""
    if principal and principal.role == "member":
        return scoped_account_id
    return None


def _previous_bounds(window: str) -> tuple[Optional[str], Optional[str], bool]:
    """Equal-length window immediately before ``window``. ``all`` is not comparable."""
    since, until = _parse_window(window)
    if since is None:
        return None, None, False
    end = until or _utc_now()
    delta = end - since
    if delta.total_seconds() <= 0:
        return None, None, False
    return (since - delta).isoformat(), since.isoformat(), True


def _as_splits(split_map: dict[tuple[str, str], dict[str, Any]]) -> list[DimensionSplit]:
    ordered = sorted(
        split_map.items(),
        key=lambda kv: kv[1]["total_tokens"],
        reverse=True,
    )
    return [
        DimensionSplit(
            account_id=acct,
            project_id=pid,
            requests=int(bucket["requests"]),
            total_tokens=int(bucket["total_tokens"]),
            estimated_cost_usd=round(float(bucket["cost"]), 6),
        )
        for (acct, pid), bucket in ordered
    ]


def _row_cost(model: str, prompt: int, completion: int, cost_usd: Any = None) -> float:
    """Prefer stored cost_usd (e.g. cursor_charged) when present."""
    if cost_usd is not None:
        try:
            return float(cost_usd)
        except (TypeError, ValueError):
            pass
    return estimate_cost(model, prompt, completion)

def _participant_expr() -> str:
    return "COALESCE(NULLIF(TRIM(participant), ''), NULLIF(TRIM(api_key), ''), 'anonymous')"


def _cost_for_group(
    rows_by_model: list[Any],
) -> float:
    total = 0.0
    for r in rows_by_model:
        total += estimate_cost(
            r["model"], int(r["prompt_tokens"]), int(r["completion_tokens"])
        )
    return total


@app.on_event("startup")
def on_startup() -> None:
    init_db()
    # Default OFF — demo seed must not pollute real project rankings
    if os.environ.get("SEED_ON_START", "0") in ("1", "true", "yes"):
        n = seed(force=False)
        if n:
            print(f"[seed] inserted {n} demo events (source=seed)")
        else:
            print(f"[seed] skipped, already have {count_events()} events")
    else:
        print(f"[seed] SEED_ON_START=0; events={count_events()} (seed hidden in rankings by default)")
    # Forum demo posts (independent of usage SEED_ON_START)
    if os.environ.get("FORUM_SEED_ON_START", "1") in ("1", "true", "yes"):
        fn = seed_forum_if_empty(force=False)
        if fn:
            print(f"[forum] seeded {fn} demo posts")
        else:
            print(f"[forum] posts={count_forum_posts()} (skip seed)")
    else:
        print(f"[forum] FORUM_SEED_ON_START=0; posts={count_forum_posts()}")


@app.get("/health")
def health() -> dict[str, Any]:
    cred = health_credibility(exclude_seed=True)
    adapters = [
        {"id": "litellm", "status": "supported", "display": "LiteLLM"},
        {"id": "cursor", "status": "supported", "display": "Cursor"},
        {"id": "workbuddy", "status": "supported", "display": "WorkBuddy"},
        {"id": "claude_code", "status": "supported", "display": "Claude Code"},
        {"id": "codex", "status": "supported", "display": "Codex"},
        {"id": "continue", "status": "supported", "display": "Continue"},
        {
            "id": PROCODE_ADAPTER_META["id"],
            "status": PROCODE_ADAPTER_META["status"],
            "display": PROCODE_ADAPTER_META["display"],
            "notes": PROCODE_ADAPTER_META["notes"],
        },
        {"id": "openai-usage", "status": "supported", "display": "OpenAI usage"},
    ]
    return {
        "status": "ok",
        "version": APP_VERSION,
        "setup": setup_warnings(),
        "events": count_events(),
        "events_non_seed": count_events(exclude_seed=True),
        "priced_models": len(list_priced_models()),
        "write_auth_configured": api_key_configured(),
        "rbac": auth_status(),
        "database": database_status(),
        "tools_connected": cred["tools_connected"],
        "tools": cred["tools"],
        "adapters": adapters,
        "agent_id_coverage_pct": cred["agent_id_coverage_pct"],
        "agent_id_events": cred["agent_id_events"],
        "active_projects": cred["active_projects"],
        "active_accounts": cred["active_accounts"],
        "approx_concurrent_requests_1h": cred["approx_concurrent_requests_1h"],
        "active_projects_1h": cred["active_projects_1h"],
        "cost_basis": cred["cost_basis"],
        "forum_posts": count_forum_posts(),
    }


@app.post("/api/v1/usage", response_model=UsageOut)
def ingest_usage(
    body: UsageIn,
    _auth: str = Depends(require_write_api_key),
) -> UsageOut:
    total = body.total_tokens
    if total is None:
        total = body.prompt_tokens + body.completion_tokens
    ts = body.timestamp
    if not ts:
        ts = _utc_now().isoformat()
    else:
        try:
            datetime.fromisoformat(ts.replace("Z", "+00:00"))
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=f"bad timestamp: {exc}") from exc

    participant = body.participant
    if not participant and body.api_key:
        participant = body.api_key

    tool = normalize_tool(body.tool) if body.tool else None
    result = insert_usage_rows(
        [
            {
                "model": body.model,
                "prompt_tokens": body.prompt_tokens,
                "completion_tokens": body.completion_tokens,
                "total_tokens": total,
                "team": body.team,
                "api_key": body.api_key,
                "tag": body.tag,
                "participant": participant,
                "tool": tool,
                "account_id": body.account_id,
                "project_id": body.project_id,
                "agent_id": body.agent_id,
                "agent_name": body.agent_name,
                "request_id": body.request_id,
                "source": body.source or "api",
                "ts": ts,
            }
        ]
    )
    if result["accepted"] == 0:
        raise HTTPException(
            status_code=409,
            detail={
                "error": "duplicate_request_id",
                "message": "Same source+request_id already ingested",
                "skipped": result["skipped"],
            },
        )
    row_id = result["ids"][0]
    cost = estimate_cost(body.model, body.prompt_tokens, body.completion_tokens)
    return UsageOut(
        id=row_id,
        model=body.model,
        prompt_tokens=body.prompt_tokens,
        completion_tokens=body.completion_tokens,
        total_tokens=total,
        team=body.team,
        participant=participant,
        tool=tool,
        account_id=body.account_id,
        project_id=body.project_id,
        agent_id=body.agent_id,
        agent_name=body.agent_name,
        estimated_cost_usd=cost,
        ts=ts,
    )



class IngestResult(BaseModel):
    accepted: int
    skipped: int
    ids: list[int]
    source: str


def _ingest_payload(payload: Any, source: str) -> IngestResult:
    from app.ingest import extract_records

    mapped = map_records(payload, default_source=source)
    # Ensure every row carries source for unique index namespace
    for row in mapped:
        row["source"] = row.get("source") or source
        if row.get("tool"):
            row["tool"] = normalize_tool(row["tool"])
    raw_count = len(extract_records(payload))
    unmapped = max(raw_count - len(mapped), 0)
    result = insert_usage_rows(mapped)
    return IngestResult(
        accepted=result["accepted"],
        skipped=unmapped + result["skipped"],
        ids=result["ids"],
        source=source,
    )


@app.post("/api/v1/ingest/litellm", response_model=IngestResult)
def ingest_litellm(
    payload: Any = Body(...),
    _auth: str = Depends(require_write_api_key),
) -> IngestResult:
    """Batch ingest LiteLLM SpendLogs (single object, list, or {data:[...]})."""
    return _ingest_payload(payload, source="litellm")


@app.post("/api/v1/ingest/openai-usage", response_model=IngestResult)
def ingest_openai_usage(
    payload: Any = Body(...),
    _auth: str = Depends(require_write_api_key),
) -> IngestResult:
    """Ingest OpenAI-compatible chat.completion usage (+ optional metadata)."""
    return _ingest_payload(payload, source="openai-usage")


@app.post("/api/v1/ingest/cursor", response_model=IngestResult)
def ingest_cursor(
    payload: Any = Body(...),
    _auth: str = Depends(require_write_api_key),
) -> IngestResult:
    """Offline / push path: Cursor filtered-usage-events JSON → unified usage."""
    events = extract_cursor_events(payload)
    mapped = map_cursor_payload(payload)
    for row in mapped:
        row["source"] = row.get("source") or "cursor"
        row["tool"] = normalize_tool(row.get("tool") or "cursor")
    result = insert_usage_rows(mapped)
    return IngestResult(
        accepted=result["accepted"],
        skipped=max(len(events) - len(mapped), 0) + result["skipped"],
        ids=result["ids"],
        source="cursor",
    )


@app.post("/api/v1/sync/cursor")
def sync_cursor(
    days: int = Query(1, ge=1, le=30, description="lookback days"),
    page_size: int = Query(100, ge=1, le=1000),
    _auth: str = Depends(require_write_api_key),
) -> dict[str, Any]:
    """Pull Cursor Admin POST /teams/filtered-usage-events then ingest.

    Needs CURSOR_ADMIN_API_KEY. Returns 503 if unset/unreachable; does not crash the app.
    """
    key = (os.environ.get("CURSOR_ADMIN_API_KEY") or "").strip()
    if not key:
        raise HTTPException(
            status_code=503,
            detail={
                "error": "cursor_not_configured",
                "message": "Set CURSOR_ADMIN_API_KEY to enable Cursor Admin sync",
            },
        )
    base = (os.environ.get("CURSOR_API_BASE") or "https://api.cursor.com").rstrip("/")
    try:
        import httpx
    except ImportError as exc:
        raise HTTPException(
            status_code=503,
            detail={"error": "httpx_missing", "message": str(exc)},
        ) from exc

    now = _utc_now()
    end_ms = int(now.timestamp() * 1000)
    start_ms = int((now - timedelta(days=days)).timestamp() * 1000)
    all_events: list[dict[str, Any]] = []
    page = 1
    errors: list[str] = []
    try:
        with httpx.Client(timeout=45.0, follow_redirects=True) as client:
            while page <= 50:
                body = {
                    "startDate": start_ms,
                    "endDate": end_ms,
                    "page": page,
                    "pageSize": page_size,
                }
                resp = client.post(
                    f"{base}/teams/filtered-usage-events",
                    json=body,
                    auth=(key, ""),
                    headers={"Content-Type": "application/json", "Accept": "application/json"},
                )
                if resp.status_code >= 400:
                    errors.append(f"page {page} -> HTTP {resp.status_code}: {resp.text[:200]}")
                    break
                data = resp.json()
                batch = extract_cursor_events(data)
                all_events.extend(batch)
                pag = data.get("pagination") if isinstance(data, dict) else None
                has_next = bool(pag.get("hasNextPage")) if isinstance(pag, dict) else False
                if not has_next or not batch:
                    break
                page += 1
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(
            status_code=503,
            detail={
                "error": "cursor_unreachable",
                "message": str(exc),
                "tried": errors,
            },
        ) from exc

    if errors and not all_events:
        raise HTTPException(
            status_code=503,
            detail={
                "error": "cursor_fetch_failed",
                "message": "Could not read filtered-usage-events",
                "tried": errors,
            },
        )

    mapped = map_cursor_payload({"usageEvents": all_events})
    for row in mapped:
        row["source"] = row.get("source") or "cursor"
        row["tool"] = normalize_tool(row.get("tool") or "cursor")
    result = insert_usage_rows(mapped)
    return {
        "ok": True,
        "start_ms": start_ms,
        "end_ms": end_ms,
        "pages_fetched": page,
        "raw_events": len(all_events),
        "accepted": result["accepted"],
        "skipped": max(len(all_events) - len(mapped), 0) + result["skipped"],
        "ids": result["ids"],
        "total_events": count_events(),
        "warnings": errors,
    }


@app.post("/api/v1/ingest/workbuddy", response_model=IngestResult)
def ingest_workbuddy(
    payload: Any = Body(...),
    _auth: str = Depends(require_write_api_key),
) -> IngestResult:
    """Push already-parsed WorkBuddy usage events (no chat content stored)."""
    events = extract_workbuddy_payload(payload)
    mapped = map_workbuddy_events(events)
    for row in mapped:
        row["source"] = row.get("source") or "workbuddy"
        row["tool"] = normalize_tool(row.get("tool") or "workbuddy")
    result = insert_usage_rows(mapped)
    return IngestResult(
        accepted=result["accepted"],
        skipped=max(len(events) - len(mapped), 0) + result["skipped"],
        ids=result["ids"],
        source="workbuddy",
    )


@app.post("/api/v1/scan/workbuddy")
def scan_workbuddy_endpoint(
    root: Optional[str] = Query(
        None, description="Override WORKBUDDY_PROJECTS_DIR for this scan"
    ),
    _auth: str = Depends(require_write_api_key),
) -> dict[str, Any]:
    """Scan local WorkBuddy projects dir for *.jsonl usage lines and ingest."""
    from pathlib import Path as _Path

    scan_root = root or os.environ.get("WORKBUDDY_PROJECTS_DIR") or str(
        _Path.home() / ".workbuddy" / "projects"
    )
    path_root = _Path(scan_root).expanduser()
    if not path_root.is_dir():
        raise HTTPException(
            status_code=503,
            detail={
                "error": "workbuddy_dir_missing",
                "message": f"Directory not found: {path_root}",
                "hint": "Set WORKBUDDY_PROJECTS_DIR or pass ?root=",
            },
        )
    mapped, parse_stats = scan_workbuddy_dir_with_stats(path_root)
    for row in mapped:
        row["source"] = row.get("source") or "workbuddy"
        row["tool"] = normalize_tool(row.get("tool") or "workbuddy")
    result = insert_usage_rows(mapped)
    return {
        "ok": True,
        "root": str(path_root),
        "accepted": result["accepted"],
        "skipped": result["skipped"],
        "parse_skipped": parse_stats.skipped_total,
        "parse_stats": parse_stats.to_dict(),
        "ids": result["ids"],
        "total_events": count_events(),
    }


@app.post("/api/v1/ingest/procode", response_model=IngestResult)
def ingest_procode(
    payload: Any = Body(...),
    _auth: str = Depends(require_write_api_key),
) -> IngestResult:
    """Placeholder ProCode ingest: unified usage schema with tool=procode.

    Public ProCode/Canoryn schema is incomplete; tag gateway traffic and push here.
    """
    events = extract_procode_payload(payload)
    mapped, parse_stats = map_procode_events_with_stats(events)
    for row in mapped:
        row["source"] = row.get("source") or "procode"
        row["tool"] = normalize_tool("procode")
    result = insert_usage_rows(mapped)
    return IngestResult(
        accepted=result["accepted"],
        skipped=max(parse_stats.skipped_total, 0) + result["skipped"],
        ids=result["ids"],
        source="procode",
    )



@app.post("/api/v1/ingest/claude_code", response_model=IngestResult)
def ingest_claude_code(
    payload: Any = Body(...),
    _auth: str = Depends(require_write_api_key),
) -> IngestResult:
    """Push Claude Code assistant jsonl events (message.usage only; no chat content)."""
    events = extract_claude_code_payload(payload)
    mapped = map_claude_code_events(events)
    for row in mapped:
        row["source"] = row.get("source") or "claude_code"
        row["tool"] = normalize_tool(row.get("tool") or "claude_code")
    result = insert_usage_rows(mapped)
    return IngestResult(
        accepted=result["accepted"],
        skipped=max(len(events) - len(mapped), 0) + result["skipped"],
        ids=result["ids"],
        source="claude_code",
    )


@app.post("/api/v1/scan/claude_code")
def scan_claude_code_endpoint(
    root: Optional[str] = Query(
        None, description="Override CLAUDE_PROJECTS_DIR for this scan"
    ),
    _auth: str = Depends(require_write_api_key),
) -> dict[str, Any]:
    """Scan local Claude Code projects dir (~/.claude/projects) for *.jsonl usage."""
    from pathlib import Path as _Path

    scan_root = root or os.environ.get("CLAUDE_PROJECTS_DIR") or str(
        _Path.home() / ".claude" / "projects"
    )
    path_root = _Path(scan_root).expanduser()
    if not path_root.is_dir():
        raise HTTPException(
            status_code=503,
            detail={
                "error": "claude_code_dir_missing",
                "message": f"Directory not found: {path_root}",
                "hint": "Set CLAUDE_PROJECTS_DIR or pass ?root=",
            },
        )
    mapped = scan_claude_code_dir(path_root)
    for row in mapped:
        row["source"] = row.get("source") or "claude_code"
        row["tool"] = normalize_tool(row.get("tool") or "claude_code")
    result = insert_usage_rows(mapped)
    return {
        "ok": True,
        "root": str(path_root),
        "accepted": result["accepted"],
        "skipped": result["skipped"],
        "ids": result["ids"],
        "total_events": count_events(),
    }


@app.post("/api/v1/ingest/codex", response_model=IngestResult)
def ingest_codex(
    payload: Any = Body(...),
    _auth: str = Depends(require_write_api_key),
) -> IngestResult:
    """Push Codex CLI rollout lines / usage events (no chat content stored).

    Prefer token_usage_record; request_id=response_id. cached_input ⊆ input
    is split into fresh prompt + cache_read.
    """
    events = extract_codex_payload(payload)
    mapped = map_codex_events(events)
    for row in mapped:
        row["source"] = row.get("source") or "codex"
        row["tool"] = normalize_tool(row.get("tool") or "codex")
    result = insert_usage_rows(mapped)
    return IngestResult(
        accepted=result["accepted"],
        skipped=max(len(events) - len(mapped), 0) + result["skipped"],
        ids=result["ids"],
        source="codex",
    )


@app.post("/api/v1/scan/codex")
def scan_codex_endpoint(
    root: Optional[str] = Query(
        None, description="Override CODEX_SESSIONS_DIR for this scan"
    ),
    _auth: str = Depends(require_write_api_key),
) -> dict[str, Any]:
    """Scan local Codex CLI sessions dir (~/.codex/sessions) for rollout-*.jsonl."""
    from pathlib import Path as _Path

    scan_root = root or os.environ.get("CODEX_SESSIONS_DIR") or str(
        _Path.home() / ".codex" / "sessions"
    )
    path_root = _Path(scan_root).expanduser()
    if not path_root.is_dir():
        raise HTTPException(
            status_code=503,
            detail={
                "error": "codex_dir_missing",
                "message": f"Directory not found: {path_root}",
                "hint": "Set CODEX_SESSIONS_DIR or pass ?root=",
            },
        )
    mapped = scan_codex_dir(path_root)
    for row in mapped:
        row["source"] = row.get("source") or "codex"
        row["tool"] = normalize_tool(row.get("tool") or "codex")
    result = insert_usage_rows(mapped)
    return {
        "ok": True,
        "root": str(path_root),
        "accepted": result["accepted"],
        "skipped": result["skipped"],
        "ids": result["ids"],
        "total_events": count_events(),
    }



@app.post("/api/v1/ingest/continue", response_model=IngestResult)
def ingest_continue(
    payload: Any = Body(...),
    _auth: str = Depends(require_write_api_key),
) -> IngestResult:
    """Push Continue tokensGenerated events (HTTP destination or flat jsonl).

    Accepts Continue config.yaml HTTP envelope
    ``{name, data, schema, level, profileId}`` or a batch ``{events:[...]}``.
    Never stores prompt/completion bodies. tool=continue.
    """
    events = extract_continue_payload(payload)
    mapped = map_continue_events(events)
    for row in mapped:
        row["source"] = row.get("source") or "continue"
        row["tool"] = normalize_tool(row.get("tool") or "continue")
    result = insert_usage_rows(mapped)
    return IngestResult(
        accepted=result["accepted"],
        skipped=max(len(events) - len(mapped), 0) + result["skipped"],
        ids=result["ids"],
        source="continue",
    )


@app.post("/api/v1/scan/continue")
def scan_continue_endpoint(
    root: Optional[str] = Query(
        None, description="Override CONTINUE_DATA_DIR for this scan"
    ),
    _auth: str = Depends(require_write_api_key),
) -> dict[str, Any]:
    """Scan local Continue dir (~/.continue/dev_data) for **/tokensGenerated.jsonl."""
    from pathlib import Path as _Path

    scan_root = root or os.environ.get("CONTINUE_DATA_DIR") or str(
        _Path.home() / ".continue" / "dev_data"
    )
    path_root = _Path(scan_root).expanduser()
    if not path_root.is_dir():
        raise HTTPException(
            status_code=503,
            detail={
                "error": "continue_dir_missing",
                "message": f"Directory not found: {path_root}",
                "hint": "Set CONTINUE_DATA_DIR or pass ?root=",
            },
        )
    mapped = scan_continue_dir(path_root)
    for row in mapped:
        row["source"] = row.get("source") or "continue"
        row["tool"] = normalize_tool(row.get("tool") or "continue")
    result = insert_usage_rows(mapped)
    return {
        "ok": True,
        "root": str(path_root),
        "accepted": result["accepted"],
        "skipped": result["skipped"],
        "ids": result["ids"],
        "total_events": count_events(),
    }


@app.post("/api/v1/sync/litellm")
def sync_litellm(
    start_date: Optional[str] = Query(
        None, description="YYYY-MM-DD or YYYY-MM-DD HH:MM:SS"
    ),
    end_date: Optional[str] = Query(None),
    days: int = Query(1, ge=1, le=90, description="lookback days if start_date omitted"),
    _auth: str = Depends(require_write_api_key),
) -> dict[str, Any]:
    """Pull from LiteLLM GET /spend/logs then ingest. Needs env LITELLM_*.

    Fails gracefully with 503 if unset or unreachable; does not crash the app.
    """
    base = (os.environ.get("LITELLM_BASE_URL") or "").rstrip("/")
    key = os.environ.get("LITELLM_MASTER_KEY") or ""
    if not base or not key:
        raise HTTPException(
            status_code=503,
            detail={
                "error": "litellm_not_configured",
                "message": "Set LITELLM_BASE_URL and LITELLM_MASTER_KEY to enable sync",
            },
        )

    try:
        import httpx
    except ImportError as exc:
        raise HTTPException(
            status_code=503,
            detail={"error": "httpx_missing", "message": str(exc)},
        ) from exc

    now = _utc_now()
    if end_date:
        end_s = end_date.replace("T", " ").replace("Z", "")
        if len(end_s) == 10:
            end_s = end_s + " 23:59:59"
    else:
        end_s = now.strftime("%Y-%m-%d %H:%M:%S")
    if start_date:
        start_s = start_date.replace("T", " ").replace("Z", "")
        if len(start_s) == 10:
            start_s = start_s + " 00:00:00"
    else:
        start_s = (now - timedelta(days=days)).strftime("%Y-%m-%d %H:%M:%S")

    headers = {"Authorization": f"Bearer {key}", "Accept": "application/json"}
    paths = [
        f"/spend/logs/v2?start_date={start_s}&end_date={end_s}&page_size=500",
        f"/spend/logs?start_date={start_s}&end_date={end_s}",
    ]
    fetched: Any = None
    errors: list[str] = []
    used_path = ""
    try:
        with httpx.Client(timeout=30.0, follow_redirects=True) as client:
            for path in paths:
                try:
                    resp = client.get(base + path, headers=headers)
                    if resp.status_code >= 400:
                        errors.append(f"{path} -> HTTP {resp.status_code}")
                        continue
                    fetched = resp.json()
                    used_path = path.split("?")[0]
                    break
                except Exception as exc:  # noqa: BLE001
                    errors.append(f"{path} -> {exc}")
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(
            status_code=503,
            detail={
                "error": "litellm_unreachable",
                "message": str(exc),
                "tried": errors,
            },
        ) from exc

    if fetched is None:
        raise HTTPException(
            status_code=503,
            detail={
                "error": "litellm_fetch_failed",
                "message": "Could not read spend logs from LiteLLM",
                "tried": errors,
            },
        )

    result = _ingest_payload(fetched, source=f"litellm-sync:{used_path}")
    return {
        "ok": True,
        "litellm_path": used_path,
        "start_date": start_s,
        "end_date": end_s,
        "accepted": result.accepted,
        "skipped": result.skipped,
        "ids": result.ids,
        "total_events": count_events(),
    }


@app.get("/api/v1/rankings", response_model=RankingsResponse)
def get_rankings(
    window: str = Query("7d", description="today|yesterday|7d|30d|90d|all"),
    group_by: Literal["model", "team", "project", "account", "account_project"] = Query(
        "model",
        description="account_project is account_id and project_id together, not a single total",
    ),
    project_id: Optional[str] = Query(None),
    account_id: Optional[str] = Query(None),
    team: Optional[str] = Query(None),
    include_seed: bool = Query(False, description="include source=seed demo rows"),
    principal: Optional[Principal] = Depends(optional_principal),
) -> RankingsResponse:
    account_id = _scoped_account(principal, account_id)
    since, until = _parse_window(window)
    where, params = _where_ts(since, until)
    seed_clause, seed_val = _seed_filter(include_seed)
    where, params = _and_filters(
        where,
        params,
        [
            ("project_id = ?", project_id) if project_id else ("", None),
            ("account_id = ?", account_id) if account_id else ("", None),
            ("team = ?", team) if team else ("", None),
            (seed_clause, seed_val) if seed_clause else ("", None),
        ],
    )
    acct_expr = f"COALESCE(NULLIF(TRIM(account_id), ''), '{UNLABELED_ACCOUNT}')"
    proj_expr = f"COALESCE(NULLIF(TRIM(project_id), ''), '{UNLABELED_PROJECT}')"
    if group_by == "model":
        name_expr = "COALESCE(NULLIF(TRIM(model), ''), 'unknown')"
        group_sql = name_expr
        dim_sql = "NULL AS account_id, NULL AS project_id"
    elif group_by == "team":
        name_expr = "COALESCE(NULLIF(TRIM(team), ''), 'unknown')"
        group_sql = name_expr
        dim_sql = "NULL AS account_id, NULL AS project_id"
    elif group_by == "project":
        name_expr = proj_expr
        group_sql = name_expr
        dim_sql = f"NULL AS account_id, {proj_expr} AS project_id"
    elif group_by == "account":
        name_expr = acct_expr
        group_sql = name_expr
        dim_sql = f"{acct_expr} AS account_id, NULL AS project_id"
    else:
        name_expr = f"{acct_expr} || ' · ' || {proj_expr}"
        group_sql = f"{acct_expr}, {proj_expr}"
        dim_sql = f"{acct_expr} AS account_id, {proj_expr} AS project_id"

    sql = f"""
        SELECT
            {name_expr} AS name,
            {dim_sql},
            COUNT(*) AS requests,
            COALESCE(SUM(prompt_tokens), 0) AS prompt_tokens,
            COALESCE(SUM(completion_tokens), 0) AS completion_tokens,
            COALESCE(SUM(total_tokens), 0) AS total_tokens
        FROM usage_events
        {where}
        GROUP BY {group_sql}
        ORDER BY total_tokens DESC
    """
    with db() as conn:
        rows = conn.execute(sql, params).fetchall()

    cost_by_name: dict[str, float] = {}
    if group_by == "model":
        for r in rows:
            cost_by_name[r["name"]] = estimate_cost(
                r["name"], int(r["prompt_tokens"]), int(r["completion_tokens"])
            )
    else:
        detail_sql = f"""
            SELECT
                {name_expr} AS grp,
                model,
                COALESCE(SUM(prompt_tokens), 0) AS prompt_tokens,
                COALESCE(SUM(completion_tokens), 0) AS completion_tokens
            FROM usage_events
            {where}
            GROUP BY {group_sql}, model
        """
        with db() as conn:
            detail_rows = conn.execute(detail_sql, params).fetchall()
        for dr in detail_rows:
            name = dr["grp"]
            cost_by_name[name] = cost_by_name.get(name, 0.0) + estimate_cost(
                dr["model"], int(dr["prompt_tokens"]), int(dr["completion_tokens"])
            )

    grand_tokens = sum(int(r["total_tokens"]) for r in rows)
    grand_req = sum(int(r["requests"]) for r in rows)
    grand_cost = sum(cost_by_name.get(r["name"], 0.0) for r in rows)

    items: list[RankingRow] = []
    for idx, r in enumerate(rows, start=1):
        total_tok = int(r["total_tokens"])
        cost = round(cost_by_name.get(r["name"], 0.0), 6)
        share = (total_tok / grand_tokens * 100.0) if grand_tokens else 0.0
        row_account = r["account_id"]
        row_project = r["project_id"]
        items.append(
            RankingRow(
                rank=idx,
                name=r["name"],
                requests=int(r["requests"]),
                prompt_tokens=int(r["prompt_tokens"]),
                completion_tokens=int(r["completion_tokens"]),
                total_tokens=total_tok,
                estimated_cost_usd=cost,
                share_pct=round(share, 2),
                account_id=row_account or None,
                project_id=row_project or None,
                cost_basis="estimate",
                cost_basis_label=cost_basis_label("estimate"),
            )
        )

    basis = summarize_cost_basis(where, params)
    for it in items:
        it.cost_basis = basis["primary"]
        it.cost_basis_label = basis["label"]
    breakdown = {
        "model": "model",
        "team": "team",
        "project": "project_id",
        "account": "account_id",
        "account_project": "account_id × project_id",
    }[group_by]
    return RankingsResponse(
        window=window,
        group_by=group_by,
        generated_at=_utc_now().isoformat(),
        totals={
            "requests": grand_req,
            "total_tokens": grand_tokens,
            "estimated_cost_usd": round(grand_cost, 6),
            "groups": len(items),
            "breakdown": breakdown,
            "cost_basis": basis["primary"],
            "cost_basis_label": basis["label"],
            "cost_basis_note": basis["note"],
            "cost_basis_detail": basis,
        },
        items=items,
    )


@app.get("/api/v1/rankings/people", response_model=PeopleRankingsResponse)
def get_people_rankings(
    window: str = Query("today"),
    tool: Optional[str] = Query(None),
    model: Optional[str] = Query(None),
    team: Optional[str] = Query(None),
    project_id: Optional[str] = Query(None),
    account_id: Optional[str] = Query(None),
    q: Optional[str] = Query(None, description="search participant name"),
    include_seed: bool = Query(False, description="include source=seed demo rows"),
    principal: Optional[Principal] = Depends(optional_principal),
) -> PeopleRankingsResponse:
    account_id = _scoped_account(principal, account_id)
    since, until = _parse_window(window)
    where, params = _where_ts(since, until)
    extras: list[tuple[str, Any]] = []
    if tool:
        extras.append(("tool = ?", normalize_tool(tool)))
    if model:
        extras.append(("model = ?", model))
    if team:
        extras.append(("team = ?", team))
    if project_id:
        extras.append(("project_id = ?", project_id))
    if account_id:
        extras.append(("account_id = ?", account_id))
    if q:
        extras.append((f"{_participant_expr()} LIKE ?", f"%{q}%"))
    seed_clause, seed_val = _seed_filter(include_seed)
    if seed_clause:
        extras.append((seed_clause, seed_val))
    where, params = _and_filters(where, params, extras)

    pexpr = _participant_expr()
    sql = f"""
        SELECT
            {pexpr} AS participant,
            model,
            COALESCE(NULLIF(TRIM(tool), ''), 'unknown') AS tool,
            COALESCE(team, '') AS team,
            COALESCE(project_id, '') AS project_id,
            COALESCE(account_id, '') AS account_id,
            COALESCE(SUM(prompt_tokens), 0) AS prompt_tokens,
            COALESCE(SUM(completion_tokens), 0) AS completion_tokens,
            COALESCE(SUM(total_tokens), 0) AS total_tokens,
            COALESCE(SUM(cost_usd), 0) AS cost_usd_sum,
            SUM(CASE WHEN cost_usd IS NOT NULL THEN 1 ELSE 0 END) AS cost_usd_rows,
            COUNT(*) AS requests
        FROM usage_events
        {where}
        GROUP BY {pexpr}, model,
                 COALESCE(NULLIF(TRIM(tool), ''), 'unknown'),
                 COALESCE(team, ''),
                 COALESCE(project_id, ''),
                 COALESCE(account_id, '')
    """
    filter_extras: list[tuple[str, Any]] = []
    dropdown_account = _dropdown_account(principal, account_id)
    if dropdown_account:
        filter_extras.append(("account_id = ?", dropdown_account))
    if seed_clause:
        filter_extras.append((seed_clause, seed_val))
    filter_where, filter_params = _and_filters("", [], filter_extras)
    with db() as conn:
        detail = conn.execute(sql, params).fetchall()
        filter_rows = conn.execute(
            f"""
            SELECT DISTINCT
                COALESCE(NULLIF(TRIM(tool), ''), 'unknown') AS tool,
                model,
                COALESCE(team, '') AS team,
                COALESCE(project_id, '') AS project_id,
                COALESCE(account_id, '') AS account_id
            FROM usage_events
            {filter_where}
            """,
            filter_params,
        ).fetchall()

    # Aggregate per participant, and keep every account × project cell.
    agg: dict[str, dict[str, Any]] = {}
    for r in detail:
        name = r["participant"]
        if name not in agg:
            agg[name] = {
                "requests": 0,
                "total_tokens": 0,
                "cost": 0.0,
                "tools": Counter(),
                "models": Counter(),
                "teams": Counter(),
                "projects": Counter(),
                "accounts": Counter(),
                "split_map": {},
            }
        a = agg[name]
        tok = int(r["total_tokens"])
        req = int(r["requests"])
        a["requests"] += req
        a["total_tokens"] += tok
        # Prefer billed rows when every row in group has cost_usd; else blend
        if int(r["cost_usd_rows"] or 0) == int(r["requests"] or 0) and int(r["requests"] or 0) > 0:
            delta = float(r["cost_usd_sum"] or 0)
        else:
            billed = float(r["cost_usd_sum"] or 0)
            # estimate only for rows without cost_usd — approximate by proportion
            est = estimate_cost(
                r["model"], int(r["prompt_tokens"]), int(r["completion_tokens"])
            )
            if int(r["cost_usd_rows"] or 0) > 0 and int(r["requests"] or 0) > 0:
                # use billed sum + estimate scaled for missing fraction
                missing = 1.0 - (int(r["cost_usd_rows"]) / int(r["requests"]))
                delta = billed + est * missing
            else:
                delta = est
        a["cost"] += delta
        acct_key = (r["account_id"] or "").strip() or UNLABELED_ACCOUNT
        proj_key = (r["project_id"] or "").strip() or UNLABELED_PROJECT
        cell = a["split_map"].setdefault(
            (acct_key, proj_key),
            {"requests": 0, "total_tokens": 0, "cost": 0.0},
        )
        cell["requests"] += req
        cell["total_tokens"] += tok
        cell["cost"] += delta
        a["tools"][r["tool"]] += tok
        a["models"][r["model"]] += tok
        if r["team"]:
            a["teams"][r["team"]] += tok
        if r["project_id"]:
            a["projects"][r["project_id"]] += tok
        if r["account_id"]:
            a["accounts"][r["account_id"]] += tok

    ordered = sorted(agg.items(), key=lambda kv: kv[1]["total_tokens"], reverse=True)
    grand_tokens = sum(v["total_tokens"] for _, v in ordered)
    grand_cost = sum(v["cost"] for _, v in ordered)
    grand_req = sum(v["requests"] for _, v in ordered)

    items: list[PersonRow] = []
    for idx, (name, v) in enumerate(ordered, start=1):
        primary_tool_slug = v["tools"].most_common(1)[0][0] if v["tools"] else "unknown"
        primary_tool = tool_display(primary_tool_slug)
        primary_model = v["models"].most_common(1)[0][0] if v["models"] else "未知"
        primary_team = v["teams"].most_common(1)[0][0] if v["teams"] else None
        primary_project = (
            v["projects"].most_common(1)[0][0] if v["projects"] else None
        )
        primary_account = (
            v["accounts"].most_common(1)[0][0] if v["accounts"] else None
        )
        share = (v["total_tokens"] / grand_tokens * 100.0) if grand_tokens else 0.0
        splits = _as_splits(v["split_map"])
        items.append(
            PersonRow(
                rank=idx,
                participant=name,
                primary_tool=primary_tool,
                primary_model=primary_model,
                primary_project=primary_project,
                team=primary_team,
                account_id=primary_account,
                account_count=len({row.account_id for row in splits}),
                project_count=len({row.project_id for row in splits}),
                splits=splits,
                requests=v["requests"],
                total_tokens=v["total_tokens"],
                estimated_cost_usd=round(v["cost"], 6),
                share_pct=round(share, 2),
            )
        )

    # Overview: first participant as "me" demo (top user) for MVP card
    me = items[0] if items else None
    basis = summarize_cost_basis(where, params)
    for it in items:
        it.cost_basis = basis["primary"]
        it.cost_basis_label = basis["label"]
    overview = {
        "label": "今日概况" if window in ("today", "1d", "今日") else "时段概况",
        "my_tokens": me.total_tokens if me else 0,
        "my_cost_usd": me.estimated_cost_usd if me else 0.0,
        "my_rank": me.rank if me else None,
        "my_name": me.participant if me else None,
        "participants": len(items),
        "cost_basis": basis["primary"],
        "cost_basis_label": basis["label"],
        "cost_basis_note": basis["note"],
    }

    tools = sorted({normalize_tool(r["tool"]) for r in filter_rows if r["tool"]})
    models = sorted({r["model"] for r in filter_rows if r["model"]})
    teams = sorted({r["team"] for r in filter_rows if r["team"]})
    projects = sorted({r["project_id"] for r in filter_rows if r["project_id"]})
    accounts = sorted({r["account_id"] for r in filter_rows if r["account_id"]})
    labels = tool_labels_map(tools)
    split_rows = sum(len(it.splits) for it in items)

    return PeopleRankingsResponse(
        window=window,
        generated_at=_utc_now().isoformat(),
        overview=overview,
        totals={
            "requests": grand_req,
            "total_tokens": grand_tokens,
            "estimated_cost_usd": round(grand_cost, 6),
            "participants": len(items),
            "breakdown": "participant + account_id × project_id",
            "split_rows": split_rows,
            "cost_basis": basis["primary"],
            "cost_basis_label": basis["label"],
            "cost_basis_note": basis["note"],
            "cost_basis_detail": basis,
        },
        items=items,
        filters={
            "tools": tools,
            "tool_labels": labels,  # type: ignore[dict-item]
            "models": models,
            "teams": teams,
            "projects": projects,
            "accounts": accounts,
        },
    )


@app.get("/api/v1/rankings/orgs", response_model=OrgRankingsResponse)
def get_org_rankings(
    window: str = Query("7d"),
    tool: Optional[str] = Query(None),
    project_id: Optional[str] = Query(None),
    account_id: Optional[str] = Query(None),
    include_seed: bool = Query(False, description="include source=seed demo rows"),
    principal: Optional[Principal] = Depends(optional_principal),
) -> OrgRankingsResponse:
    account_id = _scoped_account(principal, account_id)
    since, until = _parse_window(window)
    where, params = _where_ts(since, until)
    seed_clause, seed_val = _seed_filter(include_seed)
    where, params = _and_filters(
        where,
        params,
        [
            ("tool = ?", normalize_tool(tool)) if tool else ("", None),
            ("project_id = ?", project_id) if project_id else ("", None),
            ("account_id = ?", account_id) if account_id else ("", None),
            (seed_clause, seed_val) if seed_clause else ("", None),
        ],
    )
    pexpr = _participant_expr()

    sql = f"""
        SELECT
            COALESCE(NULLIF(TRIM(team), ''), '未标注') AS team,
            {pexpr} AS participant,
            model,
            COALESCE(NULLIF(TRIM(account_id), ''), '{UNLABELED_ACCOUNT}') AS account_id,
            COALESCE(NULLIF(TRIM(project_id), ''), '{UNLABELED_PROJECT}') AS project_id,
            COALESCE(SUM(prompt_tokens), 0) AS prompt_tokens,
            COALESCE(SUM(completion_tokens), 0) AS completion_tokens,
            COALESCE(SUM(total_tokens), 0) AS total_tokens,
            COUNT(*) AS requests
        FROM usage_events
        {where}
        GROUP BY COALESCE(NULLIF(TRIM(team), ''), '未标注'), {pexpr}, model,
                 COALESCE(NULLIF(TRIM(account_id), ''), '{UNLABELED_ACCOUNT}'),
                 COALESCE(NULLIF(TRIM(project_id), ''), '{UNLABELED_PROJECT}')
    """
    filter_extras: list[tuple[str, Any]] = []
    dropdown_account = _dropdown_account(principal, account_id)
    if dropdown_account:
        filter_extras.append(("account_id = ?", dropdown_account))
    if seed_clause:
        filter_extras.append((seed_clause, seed_val))
    filter_where, filter_params = _and_filters("", [], filter_extras)
    with db() as conn:
        rows = conn.execute(sql, params).fetchall()
        filter_rows = conn.execute(
            f"""
            SELECT DISTINCT
                COALESCE(NULLIF(TRIM(tool), ''), 'unknown') AS tool,
                COALESCE(NULLIF(TRIM(account_id), ''), '') AS account_id,
                COALESCE(NULLIF(TRIM(project_id), ''), '') AS project_id
            FROM usage_events
            {filter_where}
            """,
            filter_params,
        ).fetchall()

    agg: dict[str, dict[str, Any]] = {}
    for r in rows:
        team = r["team"]
        if team not in agg:
            agg[team] = {
                "total_tokens": 0,
                "requests": 0,
                "cost": 0.0,
                "people": set(),
                "split_map": {},
            }
        a = agg[team]
        tok = int(r["total_tokens"])
        req = int(r["requests"])
        delta = estimate_cost(
            r["model"], int(r["prompt_tokens"]), int(r["completion_tokens"])
        )
        a["total_tokens"] += tok
        a["requests"] += req
        a["cost"] += delta
        a["people"].add(r["participant"])
        cell = a["split_map"].setdefault(
            (r["account_id"], r["project_id"]),
            {"requests": 0, "total_tokens": 0, "cost": 0.0},
        )
        cell["requests"] += req
        cell["total_tokens"] += tok
        cell["cost"] += delta

    ordered = sorted(agg.items(), key=lambda kv: kv[1]["total_tokens"], reverse=True)
    grand_tokens = sum(v["total_tokens"] for _, v in ordered)
    grand_cost = sum(v["cost"] for _, v in ordered)
    all_people = set()
    for _, v in ordered:
        all_people |= v["people"]

    items: list[OrgRow] = []
    for idx, (team, v) in enumerate(ordered, start=1):
        n_people = max(len(v["people"]), 1)
        share = (v["total_tokens"] / grand_tokens * 100.0) if grand_tokens else 0.0
        splits = _as_splits(v["split_map"])
        items.append(
            OrgRow(
                rank=idx,
                team=team,
                total_tokens=v["total_tokens"],
                per_capita_tokens=round(v["total_tokens"] / n_people, 1),
                participants=len(v["people"]),
                requests=v["requests"],
                estimated_cost_usd=round(v["cost"], 6),
                share_pct=round(share, 2),
                account_count=len({row.account_id for row in splits}),
                project_count=len({row.project_id for row in splits}),
                splits=splits,
            )
        )

    basis = summarize_cost_basis(where, params)
    for it in items:
        it.cost_basis = basis["primary"]
        it.cost_basis_label = basis["label"]
    tools = sorted({normalize_tool(r["tool"]) for r in filter_rows if r["tool"]})
    accounts = sorted({r["account_id"] for r in filter_rows if r["account_id"]})
    projects = sorted({r["project_id"] for r in filter_rows if r["project_id"]})
    return OrgRankingsResponse(
        window=window,
        generated_at=_utc_now().isoformat(),
        totals={
            "orgs": len(items),
            "participants": len(all_people),
            "total_tokens": grand_tokens,
            "estimated_cost_usd": round(grand_cost, 6),
            "requests": sum(v["requests"] for _, v in ordered),
            "breakdown": "team + account_id × project_id",
            "split_rows": sum(len(it.splits) for it in items),
            "cost_basis": basis["primary"],
            "cost_basis_label": basis["label"],
            "cost_basis_note": basis["note"],
            "cost_basis_detail": basis,
        },
        items=items,
        filters={
            "tools": tools,
            "tool_labels": tool_labels_map(tools),
            "accounts": accounts,
            "projects": projects,
        },
    )



@app.get("/api/v1/rankings/projects", response_model=ProjectRankingsResponse)
def get_project_rankings(
    window: str = Query("7d"),
    tool: Optional[str] = Query(None),
    account_id: Optional[str] = Query(None),
    project_id: Optional[str] = Query(None),
    team: Optional[str] = Query(None),
    participant: Optional[str] = Query(None),
    hide_unlabeled: bool = Query(
        True, description="hide rows with missing project_id (未标注)"
    ),
    include_seed: bool = Query(False, description="include source=seed demo rows"),
    principal: Optional[Principal] = Depends(optional_principal),
) -> ProjectRankingsResponse:
    """Rank by account_id × project_id so multi-project usage is not a black-box total."""
    account_id = _scoped_account(principal, account_id)
    since, until = _parse_window(window)
    where, params = _where_ts(since, until)
    extras: list[tuple[str, Any]] = []
    if tool:
        extras.append(("tool = ?", normalize_tool(tool)))
    if account_id:
        extras.append(("account_id = ?", account_id))
    if project_id:
        extras.append(("project_id = ?", project_id))
    if team:
        extras.append(("team = ?", team))
    if participant:
        extras.append((f"{_participant_expr()} = ?", participant))
    if hide_unlabeled:
        extras.append(("COALESCE(NULLIF(TRIM(project_id), ''), '') != ?", ""))
    seed_clause, seed_val = _seed_filter(include_seed)
    if seed_clause:
        extras.append((seed_clause, seed_val))
    where, params = _and_filters(where, params, extras)
    pexpr = _participant_expr()

    sql = f"""
        SELECT
            COALESCE(NULLIF(TRIM(project_id), ''), '{UNLABELED_PROJECT}') AS project_id,
            COALESCE(NULLIF(TRIM(account_id), ''), '{UNLABELED_ACCOUNT}') AS account_id,
            COALESCE(NULLIF(TRIM(tool), ''), 'unknown') AS tool,
            {pexpr} AS participant,
            model,
            COALESCE(SUM(prompt_tokens), 0) AS prompt_tokens,
            COALESCE(SUM(completion_tokens), 0) AS completion_tokens,
            COALESCE(SUM(total_tokens), 0) AS total_tokens,
            COALESCE(SUM(cost_usd), 0) AS cost_usd_sum,
            SUM(CASE WHEN cost_usd IS NOT NULL THEN 1 ELSE 0 END) AS cost_usd_rows,
            COUNT(*) AS requests
        FROM usage_events
        {where}
        GROUP BY
            COALESCE(NULLIF(TRIM(project_id), ''), '{UNLABELED_PROJECT}'),
            COALESCE(NULLIF(TRIM(account_id), ''), '{UNLABELED_ACCOUNT}'),
            COALESCE(NULLIF(TRIM(tool), ''), 'unknown'),
            {pexpr},
            model
    """
    filter_extras: list[tuple[str, Any]] = []
    dropdown_account = _dropdown_account(principal, account_id)
    if dropdown_account:
        filter_extras.append(("account_id = ?", dropdown_account))
    if seed_clause:
        filter_extras.append((seed_clause, seed_val))
    filter_where, filter_params = _and_filters("", [], filter_extras)
    with db() as conn:
        rows = conn.execute(sql, params).fetchall()
        filter_rows = conn.execute(
            f"""
            SELECT DISTINCT
                COALESCE(NULLIF(TRIM(tool), ''), 'unknown') AS tool,
                COALESCE(NULLIF(TRIM(account_id), ''), '') AS account_id,
                COALESCE(NULLIF(TRIM(project_id), ''), '') AS project_id
            FROM usage_events
            {filter_where}
            """,
            filter_params,
        ).fetchall()

    # Count unlabeled projects in same filters but without hide_unlabeled
    unlab_where, unlab_params = _where_ts(since, until)
    unlab_extras: list[tuple[str, Any]] = []
    if tool:
        unlab_extras.append(("tool = ?", normalize_tool(tool)))
    if account_id:
        unlab_extras.append(("account_id = ?", account_id))
    if project_id:
        unlab_extras.append(("project_id = ?", project_id))
    if team:
        unlab_extras.append(("team = ?", team))
    if participant:
        unlab_extras.append((f"{_participant_expr()} = ?", participant))
    if seed_clause:
        unlab_extras.append((seed_clause, seed_val))
    unlab_extras.append(("COALESCE(NULLIF(TRIM(project_id), ''), '') = ?", ""))
    unlab_where, unlab_params = _and_filters(unlab_where, unlab_params, unlab_extras)
    with db() as conn:
        unlab_row = conn.execute(
            f"SELECT COUNT(*) AS c FROM usage_events {unlab_where}", unlab_params
        ).fetchone()
    unlabeled_events = int(unlab_row["c"] or 0)

    agg: dict[tuple[str, str], dict[str, Any]] = {}
    for r in rows:
        key = (r["account_id"], r["project_id"])
        if key not in agg:
            agg[key] = {
                "total_tokens": 0,
                "requests": 0,
                "cost": 0.0,
                "people": set(),
                "tools": Counter(),
            }
        a = agg[key]
        a["total_tokens"] += int(r["total_tokens"])
        a["requests"] += int(r["requests"])
        if int(r["cost_usd_rows"] or 0) == int(r["requests"] or 0) and int(r["requests"] or 0) > 0:
            a["cost"] += float(r["cost_usd_sum"] or 0)
        else:
            a["cost"] += estimate_cost(
                r["model"], int(r["prompt_tokens"]), int(r["completion_tokens"])
            )
        a["people"].add(r["participant"])
        a["tools"][r["tool"]] += int(r["total_tokens"])

    ordered = sorted(agg.items(), key=lambda kv: kv[1]["total_tokens"], reverse=True)
    grand_tokens = sum(v["total_tokens"] for _, v in ordered)
    grand_cost = sum(v["cost"] for _, v in ordered)
    basis = summarize_cost_basis(where, params)

    items: list[ProjectRow] = []
    for idx, ((acct, pid), v) in enumerate(ordered, start=1):
        share = (v["total_tokens"] / grand_tokens * 100.0) if grand_tokens else 0.0
        primary_tool = (
            tool_display(v["tools"].most_common(1)[0][0]) if v["tools"] else None
        )
        unlabeled = is_unlabeled_name(pid) or acct == UNLABELED_ACCOUNT
        label = f"{acct} · {pid}"
        items.append(
            ProjectRow(
                rank=idx,
                project_id=pid,
                account_id=None if acct == UNLABELED_ACCOUNT else acct,
                label=label,
                tool=primary_tool,
                participants=len(v["people"]),
                requests=v["requests"],
                total_tokens=v["total_tokens"],
                estimated_cost_usd=round(v["cost"], 6),
                share_pct=round(share, 2),
                cost_basis=basis["primary"],
                cost_basis_label=basis["label"],
                unlabeled=unlabeled,
            )
        )

    tools = sorted({normalize_tool(r["tool"]) for r in filter_rows if r["tool"]})
    accounts = sorted({r["account_id"] for r in filter_rows if r["account_id"]})
    projects = sorted({r["project_id"] for r in filter_rows if r["project_id"]})
    labels = tool_labels_map(tools)

    return ProjectRankingsResponse(
        window=window,
        generated_at=_utc_now().isoformat(),
        totals={
            "projects": len(items),
            "accounts": len({it.account_id for it in items if it.account_id}),
            "total_tokens": grand_tokens,
            "estimated_cost_usd": round(grand_cost, 6),
            "requests": sum(v["requests"] for _, v in ordered),
            "breakdown": "account_id × project_id",
            "cost_basis": basis["primary"],
            "cost_basis_label": basis["label"],
            "cost_basis_note": basis["note"],
            "cost_basis_detail": basis,
            "unlabeled_events_hidden": unlabeled_events if hide_unlabeled else 0,
            "unlabeled_events": unlabeled_events,
            "hide_unlabeled": hide_unlabeled,
        },
        items=items,
        filters={
            "tools": tools,
            "tool_labels": labels,
            "accounts": accounts,
            "projects": projects,
            "hide_unlabeled": hide_unlabeled,
        },
    )


@app.get("/api/v1/rankings/agents", response_model=AgentRankingsResponse)
def get_agent_rankings(
    window: str = Query("7d"),
    account_id: Optional[str] = Query(
        None, description="filter to one account — 'my agents' view"
    ),
    team: Optional[str] = Query(None),
    tool: Optional[str] = Query(None),
    participant: Optional[str] = Query(None),
    project_id: Optional[str] = Query(None),
    hide_unlabeled: bool = Query(
        True, description="hide events without agent_id (未标注)"
    ),
    include_seed: bool = Query(False, description="include source=seed demo rows"),
    principal: Optional[Principal] = Depends(optional_principal),
) -> AgentRankingsResponse:
    """Rank by account_id × agent_id so the same agent on two accounts stays split."""
    account_id = _scoped_account(principal, account_id)
    since, until = _parse_window(window)
    where, params = _where_ts(since, until)
    extras: list[tuple[str, Any]] = []
    if hide_unlabeled:
        extras.append(("COALESCE(NULLIF(TRIM(agent_id), ''), '') != ?", ""))
    if account_id:
        extras.append(("account_id = ?", account_id))
    if team:
        extras.append(("team = ?", team))
    if tool:
        extras.append(("tool = ?", normalize_tool(tool)))
    if participant:
        extras.append((f"{_participant_expr()} = ?", participant))
    if project_id:
        extras.append(("project_id = ?", project_id))
    seed_clause, seed_val = _seed_filter(include_seed)
    if seed_clause:
        extras.append((seed_clause, seed_val))
    where, params = _and_filters(where, params, extras)
    pexpr = _participant_expr()

    sql = f"""
        SELECT
            COALESCE(NULLIF(TRIM(agent_id), ''), '{UNLABELED_AGENT}') AS agent_id,
            COALESCE(NULLIF(TRIM(agent_name), ''), '') AS agent_name,
            COALESCE(NULLIF(TRIM(account_id), ''), '') AS account_id,
            COALESCE(NULLIF(TRIM(project_id), ''), '') AS project_id,
            COALESCE(NULLIF(TRIM(team), ''), '') AS team,
            COALESCE(NULLIF(TRIM(tool), ''), 'unknown') AS tool,
            {pexpr} AS participant,
            model,
            COALESCE(SUM(prompt_tokens), 0) AS prompt_tokens,
            COALESCE(SUM(completion_tokens), 0) AS completion_tokens,
            COALESCE(SUM(total_tokens), 0) AS total_tokens,
            COALESCE(SUM(cost_usd), 0) AS cost_usd_sum,
            SUM(CASE WHEN cost_usd IS NOT NULL THEN 1 ELSE 0 END) AS cost_usd_rows,
            COUNT(*) AS requests
        FROM usage_events
        {where}
        GROUP BY
            COALESCE(NULLIF(TRIM(agent_id), ''), '{UNLABELED_AGENT}'),
            COALESCE(NULLIF(TRIM(agent_name), ''), ''),
            COALESCE(NULLIF(TRIM(account_id), ''), ''),
            COALESCE(NULLIF(TRIM(project_id), ''), ''),
            COALESCE(NULLIF(TRIM(team), ''), ''),
            COALESCE(NULLIF(TRIM(tool), ''), 'unknown'),
            {pexpr},
            model
    """
    filter_extras: list[tuple[str, Any]] = []
    dropdown_account = _dropdown_account(principal, account_id)
    if dropdown_account:
        filter_extras.append(("account_id = ?", dropdown_account))
    if hide_unlabeled:
        filter_extras.append(("COALESCE(NULLIF(TRIM(agent_id), ''), '') != ?", ""))
    if seed_clause:
        filter_extras.append((seed_clause, seed_val))
    filter_where, filter_params = _and_filters("", [], filter_extras)

    # unlabeled event count (same filters, no hide)
    unlab_where, unlab_params = _where_ts(since, until)
    unlab_extras: list[tuple[str, Any]] = [
        ("COALESCE(NULLIF(TRIM(agent_id), ''), '') = ?", ""),
    ]
    if account_id:
        unlab_extras.append(("account_id = ?", account_id))
    if team:
        unlab_extras.append(("team = ?", team))
    if tool:
        unlab_extras.append(("tool = ?", normalize_tool(tool)))
    if participant:
        unlab_extras.append((f"{_participant_expr()} = ?", participant))
    if project_id:
        unlab_extras.append(("project_id = ?", project_id))
    if seed_clause:
        unlab_extras.append((seed_clause, seed_val))
    unlab_where, unlab_params = _and_filters(unlab_where, unlab_params, unlab_extras)

    with db() as conn:
        rows = conn.execute(sql, params).fetchall()
        filter_rows = conn.execute(
            f"""
            SELECT DISTINCT
                COALESCE(NULLIF(TRIM(tool), ''), 'unknown') AS tool,
                COALESCE(NULLIF(TRIM(account_id), ''), '') AS account_id,
                COALESCE(NULLIF(TRIM(project_id), ''), '') AS project_id,
                COALESCE(NULLIF(TRIM(team), ''), '') AS team
            FROM usage_events
            {filter_where}
            """,
            filter_params,
        ).fetchall()
        unlab_row = conn.execute(
            f"SELECT COUNT(*) AS c FROM usage_events {unlab_where}", unlab_params
        ).fetchone()
    unlabeled_events = int(unlab_row["c"] or 0)

    agg: dict[tuple[str, str], dict[str, Any]] = {}
    for r in rows:
        aid = r["agent_id"]
        acct_key = (r["account_id"] or "").strip() or UNLABELED_ACCOUNT
        key = (acct_key, aid)
        if key not in agg:
            agg[key] = {
                "total_tokens": 0,
                "requests": 0,
                "cost": 0.0,
                "names": Counter(),
                "teams": Counter(),
                "tools": Counter(),
                "people": Counter(),
                "projects": Counter(),
            }
        a = agg[key]
        tok = int(r["total_tokens"])
        a["total_tokens"] += tok
        a["requests"] += int(r["requests"])
        if int(r["cost_usd_rows"] or 0) == int(r["requests"] or 0) and int(r["requests"] or 0) > 0:
            a["cost"] += float(r["cost_usd_sum"] or 0)
        else:
            a["cost"] += estimate_cost(
                r["model"], int(r["prompt_tokens"]), int(r["completion_tokens"])
            )
        if r["agent_name"]:
            a["names"][r["agent_name"]] += tok
        if r["team"]:
            a["teams"][r["team"]] += tok
        if r["project_id"]:
            a["projects"][r["project_id"]] += tok
        a["tools"][r["tool"]] += tok
        a["people"][r["participant"]] += tok

    ordered = sorted(agg.items(), key=lambda kv: kv[1]["total_tokens"], reverse=True)
    grand_tokens = sum(v["total_tokens"] for _, v in ordered)
    grand_cost = sum(v["cost"] for _, v in ordered)
    basis = summarize_cost_basis(where, params)

    items: list[AgentRow] = []
    for idx, ((acct_key, aid), v) in enumerate(ordered, start=1):
        share = (v["total_tokens"] / grand_tokens * 100.0) if grand_tokens else 0.0
        aname = v["names"].most_common(1)[0][0] if v["names"] else None
        primary_team = v["teams"].most_common(1)[0][0] if v["teams"] else None
        primary_tool = (
            tool_display(v["tools"].most_common(1)[0][0]) if v["tools"] else None
        )
        primary_person = v["people"].most_common(1)[0][0] if v["people"] else None
        unlabeled = is_unlabeled_name(aid)
        project_names = [name for name, _tok in v["projects"].most_common()]
        label = aname or aid
        items.append(
            AgentRow(
                rank=idx,
                agent_id=aid,
                agent_name=aname,
                label=label,
                account_id=None if acct_key == UNLABELED_ACCOUNT else acct_key,
                team=primary_team,
                tool=primary_tool,
                participant=primary_person,
                requests=v["requests"],
                total_tokens=v["total_tokens"],
                estimated_cost_usd=round(v["cost"], 6),
                share_pct=round(share, 2),
                project_count=len(project_names),
                projects=project_names,
                cost_basis=basis["primary"],
                cost_basis_label=basis["label"],
                unlabeled=unlabeled,
            )
        )

    tools = sorted({normalize_tool(r["tool"]) for r in filter_rows if r["tool"]})
    accounts = sorted({r["account_id"] for r in filter_rows if r["account_id"]})
    teams = sorted({r["team"] for r in filter_rows if r["team"]})
    projects = sorted({r["project_id"] for r in filter_rows if r["project_id"]})
    labels = tool_labels_map(tools)

    return AgentRankingsResponse(
        window=window,
        generated_at=_utc_now().isoformat(),
        totals={
            "agents": len(items),
            "total_tokens": grand_tokens,
            "estimated_cost_usd": round(grand_cost, 6),
            "requests": sum(v["requests"] for _, v in ordered),
            "breakdown": "account_id × agent_id",
            "cost_basis": basis["primary"],
            "cost_basis_label": basis["label"],
            "cost_basis_note": basis["note"],
            "cost_basis_detail": basis,
            "unlabeled_events_hidden": unlabeled_events if hide_unlabeled else 0,
            "unlabeled_events": unlabeled_events,
            "hide_unlabeled": hide_unlabeled,
        },
        items=items,
        filters={
            "tools": tools,
            "tool_labels": labels,
            "accounts": accounts,
            "teams": teams,
            "projects": projects,
            "hide_unlabeled": hide_unlabeled,
        },
    )



@app.get("/api/v1/me")
def get_me(
    account_id: Optional[str] = Query(
        None, description="your account_id (members: optional, forced to own)"
    ),
    window: str = Query("7d"),
    include_seed: bool = Query(False),
    principal: Optional[Principal] = Depends(optional_principal),
) -> dict[str, Any]:
    """Personal self-service: agents / projects / tools for one account.

    - Anonymous (when RANKINGS_REQUIRE_AUTH off): must pass account_id (demo UI).
    - Member key: forced to own account_id (cannot peek others).
    - Admin/service key: may pass any account_id.
    """
    scoped = _scoped_account(principal, account_id)
    if not scoped:
        raise HTTPException(
            status_code=400,
            detail={
                "error": "account_id_required",
                "message": (
                    "Pass account_id, or call with a member API key "
                    "(mapped account is used automatically)"
                ),
            },
        )
    account_id = scoped
    since, until = _parse_window(window)
    where, params = _where_ts(since, until)
    seed_clause, seed_val = _seed_filter(include_seed)
    extras: list[tuple[str, Any]] = [("account_id = ?", account_id)]
    if seed_clause:
        extras.append((seed_clause, seed_val))
    where, params = _and_filters(where, params, extras)

    with db() as conn:
        total_row = conn.execute(
            f"""
            SELECT
                COUNT(*) AS requests,
                COALESCE(SUM(total_tokens), 0) AS total_tokens,
                COALESCE(SUM(prompt_tokens), 0) AS prompt_tokens,
                COALESCE(SUM(completion_tokens), 0) AS completion_tokens,
                COALESCE(SUM(cost_usd), 0) AS cost_usd_sum,
                SUM(CASE WHEN cost_usd IS NOT NULL THEN 1 ELSE 0 END) AS cost_usd_rows
            FROM usage_events
            {where}
            """,
            params,
        ).fetchone()
        tool_rows = conn.execute(
            f"""
            SELECT
                COALESCE(NULLIF(TRIM(tool), ''), 'unknown') AS tool,
                COALESCE(SUM(total_tokens), 0) AS total_tokens,
                COUNT(*) AS requests
            FROM usage_events
            {where}
            GROUP BY COALESCE(NULLIF(TRIM(tool), ''), 'unknown')
            ORDER BY total_tokens DESC
            """,
            params,
        ).fetchall()
        agent_rows = conn.execute(
            f"""
            SELECT
                COALESCE(NULLIF(TRIM(agent_id), ''), '{UNLABELED_AGENT}') AS agent_id,
                COALESCE(NULLIF(TRIM(agent_name), ''), '') AS agent_name,
                COALESCE(SUM(total_tokens), 0) AS total_tokens,
                COUNT(*) AS requests,
                COALESCE(SUM(cost_usd), 0) AS cost_usd_sum,
                SUM(CASE WHEN cost_usd IS NOT NULL THEN 1 ELSE 0 END) AS cost_usd_rows,
                model,
                COALESCE(SUM(prompt_tokens), 0) AS prompt_tokens,
                COALESCE(SUM(completion_tokens), 0) AS completion_tokens
            FROM usage_events
            {where}
            GROUP BY
                COALESCE(NULLIF(TRIM(agent_id), ''), '{UNLABELED_AGENT}'),
                COALESCE(NULLIF(TRIM(agent_name), ''), ''),
                model
            """,
            params,
        ).fetchall()
        project_rows = conn.execute(
            f"""
            SELECT
                COALESCE(NULLIF(TRIM(project_id), ''), '{UNLABELED_PROJECT}') AS project_id,
                COALESCE(SUM(total_tokens), 0) AS total_tokens,
                COUNT(*) AS requests,
                COALESCE(SUM(cost_usd), 0) AS cost_usd_sum,
                SUM(CASE WHEN cost_usd IS NOT NULL THEN 1 ELSE 0 END) AS cost_usd_rows,
                model,
                COALESCE(SUM(prompt_tokens), 0) AS prompt_tokens,
                COALESCE(SUM(completion_tokens), 0) AS completion_tokens
            FROM usage_events
            {where}
            GROUP BY
                COALESCE(NULLIF(TRIM(project_id), ''), '{UNLABELED_PROJECT}'),
                model
            """,
            params,
        ).fetchall()
        if principal and principal.role == "member":
            accounts = [account_id] if account_id else []
        else:
            accounts = [
                r["account_id"]
                for r in conn.execute(
                    """
                    SELECT DISTINCT account_id FROM usage_events
                    WHERE COALESCE(NULLIF(TRIM(account_id), ''), '') != ''
                      AND COALESCE(source, '') != 'seed'
                    ORDER BY account_id
                    """
                ).fetchall()
                if r["account_id"]
            ]

    basis = summarize_cost_basis(where, params)
    total_tokens = int(total_row["total_tokens"] or 0)
    requests = int(total_row["requests"] or 0)

    # Tool distribution
    tools_dist = []
    for r in tool_rows:
        tok = int(r["total_tokens"])
        tools_dist.append(
            {
                "tool": normalize_tool(r["tool"]),
                "label": tool_display(r["tool"]),
                "total_tokens": tok,
                "requests": int(r["requests"]),
                "share_pct": round(tok / total_tokens * 100.0, 1) if total_tokens else 0.0,
            }
        )

    # Agents top (collapse models)
    agent_agg: dict[str, dict[str, Any]] = {}
    for r in agent_rows:
        aid = r["agent_id"]
        if aid not in agent_agg:
            agent_agg[aid] = {
                "agent_id": aid,
                "agent_name": r["agent_name"] or None,
                "total_tokens": 0,
                "requests": 0,
                "cost": 0.0,
            }
        a = agent_agg[aid]
        a["total_tokens"] += int(r["total_tokens"])
        a["requests"] += int(r["requests"])
        if int(r["cost_usd_rows"] or 0) == int(r["requests"] or 0) and int(r["requests"] or 0) > 0:
            a["cost"] += float(r["cost_usd_sum"] or 0)
        else:
            a["cost"] += estimate_cost(
                r["model"], int(r["prompt_tokens"]), int(r["completion_tokens"])
            )
        if r["agent_name"] and not a["agent_name"]:
            a["agent_name"] = r["agent_name"]
    agents_top = sorted(agent_agg.values(), key=lambda x: x["total_tokens"], reverse=True)
    all_agent_cost = sum(float(a["cost"]) for a in agents_top)
    # Prefer labeled agents first in "my" view; keep unlabeled at end
    labeled = [a for a in agents_top if a["agent_id"] != UNLABELED_AGENT]
    unlabeled_agents = [a for a in agents_top if a["agent_id"] == UNLABELED_AGENT]
    agents_top = (labeled + unlabeled_agents)[:10]
    for i, a in enumerate(agents_top, 1):
        a["rank"] = i
        a["estimated_cost_usd"] = round(a.pop("cost"), 6)
        a["label"] = a["agent_name"] or a["agent_id"]
        a["unlabeled"] = a["agent_id"] == UNLABELED_AGENT
        a["share_pct"] = (
            round(a["total_tokens"] / total_tokens * 100.0, 1) if total_tokens else 0.0
        )

    project_agg: dict[str, dict[str, Any]] = {}
    for r in project_rows:
        pid = r["project_id"]
        if pid not in project_agg:
            project_agg[pid] = {
                "project_id": pid,
                "total_tokens": 0,
                "requests": 0,
                "cost": 0.0,
            }
        p = project_agg[pid]
        p["total_tokens"] += int(r["total_tokens"])
        p["requests"] += int(r["requests"])
        if int(r["cost_usd_rows"] or 0) == int(r["requests"] or 0) and int(r["requests"] or 0) > 0:
            p["cost"] += float(r["cost_usd_sum"] or 0)
        else:
            p["cost"] += estimate_cost(
                r["model"], int(r["prompt_tokens"]), int(r["completion_tokens"])
            )
    projects_top = sorted(project_agg.values(), key=lambda x: x["total_tokens"], reverse=True)
    labeled_p = [p for p in projects_top if p["project_id"] != UNLABELED_PROJECT]
    unlabeled_p = [p for p in projects_top if p["project_id"] == UNLABELED_PROJECT]
    projects_top = (labeled_p + unlabeled_p)[:10]
    for i, p in enumerate(projects_top, 1):
        p["rank"] = i
        p["estimated_cost_usd"] = round(p.pop("cost"), 6)
        p["unlabeled"] = p["project_id"] == UNLABELED_PROJECT
        p["share_pct"] = (
            round(p["total_tokens"] / total_tokens * 100.0, 1) if total_tokens else 0.0
        )

    # Estimate blended cost for overview (full account, not top-10 slice)
    if int(total_row["cost_usd_rows"] or 0) == requests and requests > 0:
        overview_cost = float(total_row["cost_usd_sum"] or 0)
    else:
        overview_cost = all_agent_cost
        if overview_cost <= 0:
            overview_cost = sum(float(p.get("estimated_cost_usd") or p.get("cost") or 0) for p in project_agg.values())

    top_agent = next((a for a in agents_top if not a["unlabeled"]), agents_top[0] if agents_top else None)
    top_project = next((p for p in projects_top if not p["unlabeled"]), projects_top[0] if projects_top else None)
    tool_bits = " · ".join(
        f"{t['label']} {t['share_pct']}%" for t in tools_dist[:3]
    ) or "—"

    def _fmt_tok(n: int) -> str:
        abs_n = abs(n)
        if abs_n >= 1e8:
            return f"{n/1e8:.2f}".rstrip("0").rstrip(".") + "亿"
        if abs_n >= 1e4:
            return f"{n/1e4:.1f}".rstrip("0").rstrip(".") + "万"
        return f"{n:,}"

    window_label = {
        "today": "今天",
        "yesterday": "昨天",
        "7d": "近7天",
        "30d": "近30天",
        "90d": "近90天",
        "all": "全部",
    }.get(window, window)

    share_text = (
        f"【Token 周报】账号 {account_id} · {window_label}\n"
        f"合计 {_fmt_tok(total_tokens)} Tokens · "
        f"{'≈ ' if basis['primary'] in ('estimate', 'mixed') else ''}"
        f"${overview_cost:.2f}（{basis['label']}）\n"
        f"Top Agent：{(top_agent or {}).get('label', '—')}"
        f" ({_fmt_tok(int((top_agent or {}).get('total_tokens') or 0))})\n"
        f"Top 项目：{(top_project or {}).get('project_id', '—')}\n"
        f"工具：{tool_bits}\n"
        f"— Token 消耗排行榜"
    )

    how_to = (
        "上报 agent_id：写接口带 TOKEN_RANKINGS_API_KEY"
        "（Authorization: Bearer … 或 X-API-Key）。\n"
        "LiteLLM tags 例：agent_id:my-bot、agent_name:增长Copilot；"
        "或 metadata.agent_id。\n"
        "Cursor Cloud Agent 用 cloudAgentId。\n"
        "通用：POST /api/v1/usage JSON 含 account_id、agent_id、project_id。"
    )

    return {
        "account_id": account_id,
        "window": window,
        "generated_at": _utc_now().isoformat(),
        "overview": {
            "total_tokens": total_tokens,
            "requests": requests,
            "estimated_cost_usd": round(overview_cost, 6),
            "cost_basis": basis["primary"],
            "cost_basis_label": basis["label"],
            "cost_basis_note": basis["note"],
            "agents": len(labeled),
            "projects": len(labeled_p),
            "tools": len(tools_dist),
        },
        "agents": agents_top,
        "projects": projects_top,
        "tools": tools_dist,
        "share_text": share_text,
        "how_to_report_agent_id": how_to,
        "filters": {"accounts": accounts},
        "cost_basis_detail": basis,
    }




FINOPS_NOTE = (
    "按账号、项目、模型、工具拆开，供内部 FinOps 复核。"
    "未知模型保留原名；没有入库 cost_usd 时按价目表估算，"
    "不在价目表里的模型用 default 档，estimate 为 true。这不是发票。"
)
FINOPS_COLUMNS = [
    "account_id",
    "project_id",
    "model",
    "tool",
    "requests",
    "prompt_tokens",
    "completion_tokens",
    "total_tokens",
    "estimated_cost_usd",
    "price_tier",
    "estimate",
    "cost_basis",
]


def _finops_items(where: str, params: list[Any]) -> list[dict[str, Any]]:
    """Aggregate usage for review. One row per account × project × model × tool."""
    sql = f"""
        SELECT
            COALESCE(NULLIF(TRIM(account_id), ''), '{UNLABELED_ACCOUNT}') AS account_id,
            COALESCE(NULLIF(TRIM(project_id), ''), '{UNLABELED_PROJECT}') AS project_id,
            COALESCE(NULLIF(TRIM(model), ''), '') AS model,
            COALESCE(NULLIF(TRIM(tool), ''), 'unknown') AS tool,
            COALESCE(SUM(prompt_tokens), 0) AS prompt_tokens,
            COALESCE(SUM(completion_tokens), 0) AS completion_tokens,
            COALESCE(SUM(total_tokens), 0) AS total_tokens,
            COALESCE(SUM(cost_usd), 0) AS cost_usd_sum,
            SUM(CASE WHEN cost_usd IS NOT NULL THEN 1 ELSE 0 END) AS cost_usd_rows,
            COUNT(*) AS requests
        FROM usage_events
        {where}
        GROUP BY
            COALESCE(NULLIF(TRIM(account_id), ''), '{UNLABELED_ACCOUNT}'),
            COALESCE(NULLIF(TRIM(project_id), ''), '{UNLABELED_PROJECT}'),
            COALESCE(NULLIF(TRIM(model), ''), ''),
            COALESCE(NULLIF(TRIM(tool), ''), 'unknown')
        ORDER BY account_id, project_id, total_tokens DESC
    """
    with db() as conn:
        rows = conn.execute(sql, params).fetchall()
    items: list[dict[str, Any]] = []
    for row in rows:
        requests = int(row["requests"] or 0)
        stored_rows = int(row["cost_usd_rows"] or 0)
        model_name = row["model"] or ""
        tier = price_tier(model_name) if model_name else "default"
        if requests > 0 and stored_rows == requests:
            cost = round(float(row["cost_usd_sum"] or 0), 6)
            estimate = False
            basis = "stored"
        else:
            cost = round(
                estimate_cost(
                    model_name,
                    int(row["prompt_tokens"] or 0),
                    int(row["completion_tokens"] or 0),
                ),
                6,
            )
            estimate = True
            basis = "estimate"
        items.append(
            {
                "account_id": row["account_id"],
                "project_id": row["project_id"],
                "model": model_name,
                "tool": row["tool"],
                "requests": requests,
                "prompt_tokens": int(row["prompt_tokens"] or 0),
                "completion_tokens": int(row["completion_tokens"] or 0),
                "total_tokens": int(row["total_tokens"] or 0),
                "estimated_cost_usd": cost,
                "price_tier": tier,
                "estimate": estimate,
                "cost_basis": basis,
            }
        )
    return items


def _write_finops_csv(items: list[dict[str, Any]]) -> str:
    buf = io.StringIO()
    writer = csv.DictWriter(buf, fieldnames=FINOPS_COLUMNS, extrasaction="ignore")
    writer.writeheader()
    for item in items:
        row = dict(item)
        row["estimate"] = "true" if item["estimate"] else "false"
        writer.writerow(row)
    return buf.getvalue()


@app.get("/api/v1/export/usage.csv")
def export_usage_csv(
    window: str = Query("7d"),
    account_id: Optional[str] = Query(None),
    team: Optional[str] = Query(None),
    tool: Optional[str] = Query(None),
    project_id: Optional[str] = Query(None),
    model: Optional[str] = Query(None),
    grain: Literal["events", "finops"] = Query(
        "events",
        description="finops aggregates account_id × project_id × model × tool",
    ),
    include_seed: bool = Query(False),
    limit: int = Query(10000, ge=1, le=100000),
    principal: Principal = Depends(require_principal),
) -> Response:
    """Export usage as CSV. Auth required; members scoped to own account. Audited.

    grain=events is one row per event. grain=finops is the review table.
    """
    account_id = _scoped_account(principal, account_id)
    since, until = _parse_window(window)
    where, params = _where_ts(since, until)
    seed_clause, seed_val = _seed_filter(include_seed)
    extras: list[tuple[str, Any]] = []
    if account_id:
        extras.append(("account_id = ?", account_id))
    if team:
        extras.append(("team = ?", team))
    if tool:
        extras.append(("tool = ?", normalize_tool(tool)))
    if project_id:
        extras.append(("project_id = ?", project_id))
    if model:
        extras.append(("model = ?", model))
    if seed_clause:
        extras.append((seed_clause, seed_val))
    where, params = _and_filters(where, params, extras)

    filters = {
        "window": window,
        "account_id": account_id,
        "team": team,
        "tool": normalize_tool(tool) if tool else None,
        "project_id": project_id,
        "model": model,
        "grain": grain,
        "include_seed": include_seed,
        "limit": limit,
        "format": "csv",
    }
    if grain == "finops":
        items = _finops_items(where, params)[:limit]
        insert_audit(
            actor_key_hint=principal.key_hint,
            actor_name=principal.name,
            actor_role=principal.role,
            actor_account_id=principal.account_id,
            action="export.usage.csv",
            filters=filters,
            row_count=len(items),
        )
        filename = f"finops_{window}_{account_id or 'all'}.csv".replace("/", "_")
        return Response(
            content=_write_finops_csv(items),
            media_type="text/csv; charset=utf-8",
            headers={"Content-Disposition": f'attachment; filename="{filename}"'},
        )

    sql = f"""
        SELECT id, ts, model, prompt_tokens, completion_tokens, total_tokens,
               cache_read_tokens, cache_write_tokens, reasoning_tokens,
               team, participant, tool, account_id, project_id,
               agent_id, agent_name, request_id, source, cost_usd, cost_basis
        FROM usage_events
        {where}
        ORDER BY ts DESC
        LIMIT ?
    """
    with db() as conn:
        rows = conn.execute(sql, params + [limit]).fetchall()

    buf = io.StringIO()
    cols = [
        "id", "ts", "model", "prompt_tokens", "completion_tokens", "total_tokens",
        "cache_read_tokens", "cache_write_tokens", "reasoning_tokens",
        "team", "participant", "tool", "account_id", "project_id",
        "agent_id", "agent_name", "request_id", "source", "cost_usd", "cost_basis",
    ]
    writer = csv.DictWriter(buf, fieldnames=cols, extrasaction="ignore")
    writer.writeheader()
    for r in rows:
        writer.writerow({c: r[c] for c in cols})

    insert_audit(
        actor_key_hint=principal.key_hint,
        actor_name=principal.name,
        actor_role=principal.role,
        actor_account_id=principal.account_id,
        action="export.usage.csv",
        filters=filters,
        row_count=len(rows),
    )
    filename = f"usage_{window}_{account_id or 'all'}.csv".replace("/", "_")
    return Response(
        content=buf.getvalue(),
        media_type="text/csv; charset=utf-8",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


@app.get("/api/v1/export/usage.json")
def export_usage_json(
    window: str = Query("7d"),
    account_id: Optional[str] = Query(None),
    team: Optional[str] = Query(None),
    tool: Optional[str] = Query(None),
    project_id: Optional[str] = Query(None),
    model: Optional[str] = Query(None),
    grain: Literal["events", "finops"] = Query(
        "events",
        description="finops aggregates account_id × project_id × model × tool",
    ),
    include_seed: bool = Query(False),
    limit: int = Query(10000, ge=1, le=100000),
    principal: Principal = Depends(require_principal),
) -> dict[str, Any]:
    """Export usage as JSON. Auth required; members scoped; audited."""
    account_id = _scoped_account(principal, account_id)
    since, until = _parse_window(window)
    where, params = _where_ts(since, until)
    seed_clause, seed_val = _seed_filter(include_seed)
    extras: list[tuple[str, Any]] = []
    if account_id:
        extras.append(("account_id = ?", account_id))
    if team:
        extras.append(("team = ?", team))
    if tool:
        extras.append(("tool = ?", normalize_tool(tool)))
    if project_id:
        extras.append(("project_id = ?", project_id))
    if model:
        extras.append(("model = ?", model))
    if seed_clause:
        extras.append((seed_clause, seed_val))
    where, params = _and_filters(where, params, extras)

    filters = {
        "window": window,
        "account_id": account_id,
        "team": team,
        "tool": normalize_tool(tool) if tool else None,
        "project_id": project_id,
        "model": model,
        "grain": grain,
        "include_seed": include_seed,
        "limit": limit,
        "format": "json",
    }
    if grain == "finops":
        items = _finops_items(where, params)[:limit]
        row_count = len(items)
        payload: dict[str, Any] = {
            "generated_at": _utc_now().isoformat(),
            "grain": "finops",
            "dimensions": ["account_id", "project_id", "model", "tool"],
            "note": FINOPS_NOTE,
            "filters": filters,
            "count": row_count,
            "items": items,
            "actor": {
                "name": principal.name,
                "role": principal.role,
                "account_id": principal.account_id,
                "key_hint": principal.key_hint,
            },
        }
    else:
        sql = f"""
            SELECT id, ts, model, prompt_tokens, completion_tokens, total_tokens,
                   cache_read_tokens, cache_write_tokens, reasoning_tokens,
                   team, participant, tool, account_id, project_id,
                   agent_id, agent_name, request_id, source, cost_usd, cost_basis
            FROM usage_events
            {where}
            ORDER BY ts DESC
            LIMIT ?
        """
        with db() as conn:
            rows = conn.execute(sql, params + [limit]).fetchall()
        items = [dict(r) for r in rows]
        row_count = len(items)
        payload = {
            "generated_at": _utc_now().isoformat(),
            "grain": "events",
            "filters": filters,
            "count": row_count,
            "items": items,
            "actor": {
                "name": principal.name,
                "role": principal.role,
                "account_id": principal.account_id,
                "key_hint": principal.key_hint,
            },
        }
    insert_audit(
        actor_key_hint=principal.key_hint,
        actor_name=principal.name,
        actor_role=principal.role,
        actor_account_id=principal.account_id,
        action="export.usage.json",
        filters=filters,
        row_count=row_count,
    )
    return payload


@app.get("/api/v1/admin/audit")
def admin_list_audit(
    limit: int = Query(50, ge=1, le=500),
    principal: Principal = Depends(require_admin),
) -> dict[str, Any]:
    """Admin: recent export / sensitive-action audit log."""
    items = list_audit(limit=limit)
    return {
        "count": len(items),
        "items": items,
        "viewer": {
            "name": principal.name,
            "role": principal.role,
            "key_hint": principal.key_hint,
        },
    }


@app.get("/api/v1/whoami")
def whoami(
    principal: Optional[Principal] = Depends(optional_principal),
) -> dict[str, Any]:
    """Return current principal for the static UI (localStorage API key)."""
    if principal is None:
        return {
            "authenticated": False,
            "role": None,
            "account_id": None,
            "name": None,
            "key_hint": None,
            "kind": None,
        }
    return {
        "authenticated": True,
        "role": principal.role,
        "account_id": principal.account_id,
        "name": principal.name,
        "key_hint": principal.key_hint,
        "kind": principal.kind,
    }


@app.get("/api/v1/alerts/low-usage")
def alerts_low_usage(
    window: str = Query("7d"),
    threshold_tokens: int = Query(
        10000,
        ge=0,
        le=10_000_000_000,
        description="Alert when account total_tokens in window is below this",
    ),
    notify: bool = Query(
        False,
        description="If true and LOW_USAGE_WEBHOOK_URL is set, POST the alert payload",
    ),
    principal: Optional[Principal] = Depends(optional_principal),
) -> dict[str, Any]:
    """Low-usage alerts: admin/anon = all known accounts; member = self only.

    Constructive narrative: low spend may mean the AI workflow is not yet rebuilt.
    """
    since, until = _parse_window(window)
    since_iso = since.isoformat() if since else None
    until_iso = until.isoformat() if until else None
    payload = build_low_usage_alerts(
        principal=principal,
        window=window,
        since_iso=since_iso,
        until_iso=until_iso,
        threshold_tokens=threshold_tokens,
    )
    # Member without key who somehow got here with scope=all is fine for demo;
    # if they have a member key, scope=self.
    webhook = maybe_post_webhook(payload, notify=notify)
    payload["webhook"] = webhook
    return payload


@app.get("/api/v1/alerts")
def alerts_pilot(
    window: str = Query("7d"),
    threshold_tokens: Optional[int] = Query(
        None,
        ge=0,
        le=10_000_000_000,
        description="Override LOW_USAGE_THRESHOLD_TOKENS for this request",
    ),
    spend_threshold_usd: Optional[float] = Query(
        None,
        ge=0,
        description="Per-account spend ceiling in USD. Omit to use ALERT_SPEND_THRESHOLD_USD",
    ),
    spike_ratio: Optional[float] = Query(
        None,
        gt=0,
        description="Override ALERT_SPEND_SPIKE_RATIO",
    ),
    spike_min_usd: Optional[float] = Query(
        None,
        ge=0,
        description="Override ALERT_SPEND_SPIKE_MIN_USD",
    ),
    notify: bool = Query(
        False,
        description="If true and LOW_USAGE_WEBHOOK_URL is set, POST the alert payload",
    ),
    principal: Optional[Principal] = Depends(optional_principal),
) -> dict[str, Any]:
    """Pilot rules: idle, low usage, spend spike, and per-account spend.

    Each account includes its project splits. Members only see their account.
    """
    since, until = _parse_window(window)
    prev_since, prev_until, spike_ok = _previous_bounds(window)
    payload = build_pilot_alerts(
        principal=principal,
        window=window,
        since_iso=since.isoformat() if since else None,
        until_iso=until.isoformat() if until else None,
        previous_since_iso=prev_since,
        previous_until_iso=prev_until,
        spike_comparable=spike_ok,
        threshold_tokens=threshold_tokens,
        spend_threshold_usd=spend_threshold_usd,
        spike_ratio=spike_ratio,
        spike_min_usd=spike_min_usd,
    )
    payload["webhook"] = maybe_post_webhook(
        payload, notify=notify, event_type="pilot_alerts"
    )
    return payload



class ForumPostIn(BaseModel):
    title: str = Field(..., min_length=1, max_length=TITLE_MAX)
    body: str = Field(..., min_length=1, max_length=BODY_MAX)


class ForumPostOut(BaseModel):
    id: int
    title: str
    body: str
    author_name: str
    author_role: Optional[str] = None
    author_account_id: Optional[str] = None
    author_key_hint: Optional[str] = None
    likes: int = 0
    created_at: str


class ForumListResponse(BaseModel):
    disclaimer: str
    totals: dict[str, Any]
    items: list[ForumPostOut]
    title_max: int = TITLE_MAX
    body_max: int = BODY_MAX


@app.get("/api/v1/forum/posts", response_model=ForumListResponse)
def forum_list_posts(
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
    _principal: Optional[Principal] = Depends(optional_principal),
) -> ForumListResponse:
    """Anonymous read OK. Newest first."""
    items = list_forum_posts(limit=limit, offset=offset)
    return ForumListResponse(
        disclaimer=forum_disclaimer(),
        totals={"posts": count_forum_posts(), "returned": len(items)},
        items=[ForumPostOut(**x) for x in items],
    )


@app.post("/api/v1/forum/posts", response_model=ForumPostOut)
def forum_create_post(
    body: ForumPostIn,
    principal: Principal = Depends(require_principal),
) -> ForumPostOut:
    """Posting requires member/admin (or service) user API key."""
    if principal.role not in ("admin", "member"):
        raise HTTPException(
            status_code=403,
            detail={
                "error": "forum_role_forbidden",
                "message": "发帖需要 member 或 admin 用户 Key",
            },
        )
    title, text = validate_post_content(body.title, body.body)
    post_id = insert_forum_post(
        title=title,
        body=text,
        author_name=principal.name or "用户",
        author_role=principal.role,
        author_account_id=principal.account_id,
        author_key_hint=principal.key_hint,
    )
    try:
        insert_audit(
            actor_key_hint=principal.key_hint,
            actor_name=principal.name,
            actor_role=principal.role,
            actor_account_id=principal.account_id,
            action="forum_create_post",
            filters={"post_id": post_id, "title_len": len(title), "body_len": len(text)},
            row_count=1,
        )
    except Exception:
        pass
    items = list_forum_posts(limit=1, offset=0)
    # Prefer fetch by scanning — list is newest first so id should match
    for it in list_forum_posts(limit=20, offset=0):
        if it["id"] == post_id:
            return ForumPostOut(**it)
    return ForumPostOut(
        id=post_id,
        title=title,
        body=text,
        author_name=principal.name or "用户",
        author_role=principal.role,
        author_account_id=principal.account_id,
        author_key_hint=principal.key_hint,
        likes=0,
        created_at=datetime.now(timezone.utc).isoformat(),
    )


@app.post("/api/v1/forum/posts/{post_id}/like", response_model=ForumPostOut)
def forum_like_post(
    post_id: int,
    _principal: Optional[Principal] = Depends(optional_principal),
) -> ForumPostOut:
    """Simple like counter; anonymous OK."""
    row = like_forum_post(post_id)
    if not row:
        raise HTTPException(
            status_code=404,
            detail={"error": "post_not_found", "message": f"帖子 {post_id} 不存在"},
        )
    return ForumPostOut(**row)


@app.post("/api/v1/forum/seed")
def forum_seed(
    force: bool = Query(False),
    _auth: Principal = Depends(require_admin),
) -> dict[str, Any]:
    n = seed_forum_if_empty(force=force)
    return {"seeded": n, "posts": count_forum_posts()}


@app.get("/api/v1/pricing")
def get_pricing() -> dict[str, Any]:
    return load_pricing()


@app.post("/api/v1/seed")
def seed_endpoint(
    force: bool = False,
    _auth: str = Depends(require_write_api_key),
) -> dict[str, Any]:
    """Insert demo rows with source=seed (hidden from rankings by default)."""
    n = seed(force=force)
    return {
        "inserted": n,
        "total_events": count_events(),
        "non_seed_events": count_events(exclude_seed=True),
        "note": "seed rows are hidden unless include_seed=true",
    }


@app.post("/api/v1/admin/clear")
def admin_clear(
    scope: Literal["all", "seed"] = Query(
        "seed", description="seed=only demo; all=wipe usage_events"
    ),
    _auth: str = Depends(require_write_api_key),
) -> dict[str, Any]:
    """Clear usage events. Default scope=seed keeps real ingest data."""
    deleted = clear_events(scope=scope)
    return {
        "deleted": deleted,
        "scope": scope,
        "total_events": count_events(),
        "non_seed_events": count_events(exclude_seed=True),
    }


@app.get("/api/v1/tools")
def list_tools() -> dict[str, Any]:
    from app.tools import KNOWN_TOOLS, TOOL_DISPLAY

    return {
        "tools": list(KNOWN_TOOLS),
        "labels": dict(TOOL_DISPLAY),
    }



def _markdown_doc(name: str) -> FileResponse:
    p = DOCS_DIR / name
    if not p.is_file():
        raise HTTPException(status_code=404, detail=f"docs/{name} missing")
    return FileResponse(p, media_type="text/markdown; charset=utf-8")


@app.get("/docs/INSTALL.md")
def install_doc() -> FileResponse:
    return _markdown_doc("INSTALL.md")


@app.get("/docs/ACCEPTANCE.md")
def acceptance_doc() -> FileResponse:
    return _markdown_doc("ACCEPTANCE.md")


@app.get("/docs/TROUBLESHOOTING.md")
def troubleshooting_doc() -> FileResponse:
    return _markdown_doc("TROUBLESHOOTING.md")


@app.get("/docs/SECURITY.md")
def security_doc() -> FileResponse:
    return _markdown_doc("SECURITY.md")


@app.get("/docs/probe.md")
def probe_doc() -> FileResponse:
    """On-machine probe checklist (WorkBuddy / Cursor / ProCode)."""
    return _markdown_doc("probe.md")


@app.get("/docs/NON_GOALS.md")
def non_goals_doc() -> FileResponse:
    return _markdown_doc("NON_GOALS.md")


@app.get("/docs/collector.md")
def collector_doc() -> FileResponse:
    """Local collector install notes (token counts only)."""
    path = Path(__file__).resolve().parent.parent / "collector" / "INSTALL.md"
    if not path.is_file():
        raise HTTPException(status_code=404, detail="collector/INSTALL.md missing")
    return FileResponse(path, media_type="text/markdown; charset=utf-8")


@app.get("/")
@app.get("/people")
@app.get("/orgs")
@app.get("/projects")
@app.get("/agents")
@app.get("/me")
@app.get("/alerts")
@app.get("/forum")
@app.get("/procode")
def index() -> FileResponse:
    index_path = STATIC_DIR / "index.html"
    if not index_path.is_file():
        raise HTTPException(status_code=404, detail="index.html missing")
    return FileResponse(index_path)


if STATIC_DIR.is_dir():
    app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")
