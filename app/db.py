"""SQLite / Postgres helpers for usage events + audit log.

- Default: SQLite at DATA_DIR/rankings.db (demo one-click).
- Production: set DATABASE_URL=postgresql://… (or postgres://…) to switch.
Callers keep using ``?`` placeholders; Postgres adapts to ``%s``.
"""
from __future__ import annotations

import json
import os
import sqlite3
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator, Optional, Sequence, Union

DATA_DIR = Path(os.environ.get("DATA_DIR", "/data"))
DB_PATH = DATA_DIR / "rankings.db"

Params = Union[Sequence[Any], None]


def ensure_data_dir() -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)


def _database_url() -> str:
    return (os.environ.get("DATABASE_URL") or "").strip()


def is_postgres() -> bool:
    url = _database_url().lower()
    return url.startswith("postgres://") or url.startswith("postgresql://")


def backend_name() -> str:
    return "postgres" if is_postgres() else "sqlite"


def _redact_url_host(url: str) -> str:
    try:
        after = url.split("://", 1)[1]
        hostpart = after.split("@")[-1] if "@" in after else after
        return hostpart.split("/")[0]
    except Exception:
        return "(redacted)"


def _adapt_sql(sql: str) -> str:
    """Translate SQLite-style ``?`` placeholders to psycopg ``%s``."""
    if not is_postgres():
        return sql
    return sql.replace("?", "%s")


class _PgCursor:
    """Thin cursor wrapper with sqlite-like lastrowid / fetch API."""

    def __init__(self, cur: Any, lastrowid: Optional[int] = None):
        self._cur = cur
        self.rowcount = int(cur.rowcount or 0)
        self.lastrowid = lastrowid

    def fetchone(self) -> Optional[dict[str, Any]]:
        row = self._cur.fetchone()
        return dict(row) if row is not None else None

    def fetchall(self) -> list[dict[str, Any]]:
        return [dict(r) for r in self._cur.fetchall()]


class _PgConnection:
    def __init__(self, raw: Any):
        self._raw = raw

    def execute(self, sql: str, params: Params = None) -> _PgCursor:
        adapted = _adapt_sql(sql)
        if params is None:
            cur = self._raw.execute(adapted)
        else:
            cur = self._raw.execute(adapted, tuple(params))
        return _PgCursor(cur)

    def commit(self) -> None:
        self._raw.commit()

    def rollback(self) -> None:
        self._raw.rollback()

    def close(self) -> None:
        self._raw.close()


def get_connection() -> Any:
    if is_postgres():
        try:
            import psycopg
            from psycopg.rows import dict_row
        except ImportError as exc:  # pragma: no cover
            raise RuntimeError(
                "DATABASE_URL is Postgres but psycopg is not installed. "
                "pip install 'psycopg[binary]'"
            ) from exc
        url = _database_url()
        # psycopg accepts postgresql://; normalize postgres://
        if url.lower().startswith("postgres://"):
            url = "postgresql://" + url.split("://", 1)[1]
        raw = psycopg.connect(url, row_factory=dict_row, autocommit=False)
        return _PgConnection(raw)

    ensure_data_dir()
    conn = sqlite3.connect(str(DB_PATH), check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    return conn


@contextmanager
def db() -> Iterator[Any]:
    conn = get_connection()
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def _table_columns(conn: Any, table: str) -> set[str]:
    if is_postgres():
        rows = conn.execute(
            """
            SELECT column_name AS name
            FROM information_schema.columns
            WHERE table_schema = 'public' AND table_name = ?
            """,
            (table,),
        ).fetchall()
        return {r["name"] for r in rows}
    rows = conn.execute(f"PRAGMA table_info({table})").fetchall()
    return {r["name"] for r in rows}


def database_status() -> dict[str, Any]:
    """Report active backend (sqlite or postgres)."""
    url = _database_url()
    status: dict[str, Any] = {
        "backend": backend_name(),
        "sqlite_path": str(DB_PATH),
        "database_url_set": bool(url),
        "postgres_ready": is_postgres(),
    }
    if not url:
        status["message"] = (
            "Using SQLite (default). Set DATABASE_URL=postgresql://… for production Postgres."
        )
        return status

    lowered = url.lower()
    if lowered.startswith("postgres://") or lowered.startswith("postgresql://"):
        status["database_url_scheme"] = "postgresql"
        status["database_url_host"] = _redact_url_host(url)
        # Probe connectivity
        try:
            with db() as conn:
                row = conn.execute("SELECT 1 AS ok").fetchone()
            ok = bool(row and (row["ok"] == 1 or row["ok"] is True))
            status["connected"] = ok
            status["message"] = (
                "Using Postgres (DATABASE_URL)."
                if ok
                else "DATABASE_URL set but SELECT 1 failed."
            )
        except Exception as exc:
            status["connected"] = False
            status["message"] = f"Postgres connection error: {type(exc).__name__}"
        return status

    status["postgres_ready"] = False
    status["message"] = (
        f"DATABASE_URL scheme unsupported; falling back is not automatic — "
        f"unset it to use SQLite, or use postgresql://."
    )
    # If someone set a weird URL, we still only switch on postgres schemes;
    # backend_name() already returns sqlite when not postgres.
    status["backend"] = "sqlite"
    return status


def init_db() -> None:
    if is_postgres():
        _init_postgres()
    else:
        _init_sqlite()


def _init_sqlite() -> None:
    with db() as conn:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS usage_events (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                model TEXT NOT NULL,
                prompt_tokens INTEGER NOT NULL DEFAULT 0,
                completion_tokens INTEGER NOT NULL DEFAULT 0,
                total_tokens INTEGER NOT NULL DEFAULT 0,
                cache_read_tokens INTEGER NOT NULL DEFAULT 0,
                cache_write_tokens INTEGER NOT NULL DEFAULT 0,
                reasoning_tokens INTEGER NOT NULL DEFAULT 0,
                team TEXT,
                api_key TEXT,
                tag TEXT,
                participant TEXT,
                tool TEXT,
                account_id TEXT,
                project_id TEXT,
                agent_id TEXT,
                agent_name TEXT,
                request_id TEXT,
                source TEXT,
                cost_usd REAL,
                cost_basis TEXT,
                ts TEXT NOT NULL,
                created_at TEXT NOT NULL DEFAULT (datetime('now'))
            )
            """
        )
        cols = _table_columns(conn, "usage_events")
        for col, ddl in (
            ("participant", "TEXT"),
            ("tool", "TEXT"),
            ("account_id", "TEXT"),
            ("project_id", "TEXT"),
            ("agent_id", "TEXT"),
            ("agent_name", "TEXT"),
            ("cache_read_tokens", "INTEGER NOT NULL DEFAULT 0"),
            ("cache_write_tokens", "INTEGER NOT NULL DEFAULT 0"),
            ("reasoning_tokens", "INTEGER NOT NULL DEFAULT 0"),
            ("request_id", "TEXT"),
            ("source", "TEXT"),
            ("cost_usd", "REAL"),
            ("cost_basis", "TEXT"),
        ):
            if col not in cols:
                conn.execute(f"ALTER TABLE usage_events ADD COLUMN {col} {ddl}")
        for idx_sql in (
            "CREATE INDEX IF NOT EXISTS idx_usage_ts ON usage_events(ts)",
            "CREATE INDEX IF NOT EXISTS idx_usage_model ON usage_events(model)",
            "CREATE INDEX IF NOT EXISTS idx_usage_team ON usage_events(team)",
            "CREATE INDEX IF NOT EXISTS idx_usage_participant ON usage_events(participant)",
            "CREATE INDEX IF NOT EXISTS idx_usage_project ON usage_events(project_id)",
            "CREATE INDEX IF NOT EXISTS idx_usage_account ON usage_events(account_id)",
            "CREATE INDEX IF NOT EXISTS idx_usage_tool ON usage_events(tool)",
            "CREATE INDEX IF NOT EXISTS idx_usage_agent ON usage_events(agent_id)",
            "CREATE INDEX IF NOT EXISTS idx_usage_source ON usage_events(source)",
            """
            CREATE UNIQUE INDEX IF NOT EXISTS idx_usage_source_request
            ON usage_events(source, request_id)
            WHERE request_id IS NOT NULL AND TRIM(request_id) != ''
            """,
        ):
            conn.execute(idx_sql)

        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS audit_log (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                actor_key_hint TEXT,
                actor_name TEXT,
                actor_role TEXT,
                actor_account_id TEXT,
                action TEXT NOT NULL,
                filters_json TEXT,
                row_count INTEGER NOT NULL DEFAULT 0,
                created_at TEXT NOT NULL DEFAULT (datetime('now'))
            )
            """
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_audit_created ON audit_log(created_at)"
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_audit_action ON audit_log(action)"
        )
        _ensure_forum_tables(conn)


def _init_postgres() -> None:
    with db() as conn:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS usage_events (
                id BIGSERIAL PRIMARY KEY,
                model TEXT NOT NULL,
                prompt_tokens INTEGER NOT NULL DEFAULT 0,
                completion_tokens INTEGER NOT NULL DEFAULT 0,
                total_tokens INTEGER NOT NULL DEFAULT 0,
                cache_read_tokens INTEGER NOT NULL DEFAULT 0,
                cache_write_tokens INTEGER NOT NULL DEFAULT 0,
                reasoning_tokens INTEGER NOT NULL DEFAULT 0,
                team TEXT,
                api_key TEXT,
                tag TEXT,
                participant TEXT,
                tool TEXT,
                account_id TEXT,
                project_id TEXT,
                agent_id TEXT,
                agent_name TEXT,
                request_id TEXT,
                source TEXT,
                cost_usd DOUBLE PRECISION,
                cost_basis TEXT,
                ts TEXT NOT NULL,
                created_at TEXT NOT NULL DEFAULT ((CURRENT_TIMESTAMP AT TIME ZONE 'UTC')::text)
            )
            """
        )
        cols = _table_columns(conn, "usage_events")
        for col, ddl in (
            ("participant", "TEXT"),
            ("tool", "TEXT"),
            ("account_id", "TEXT"),
            ("project_id", "TEXT"),
            ("agent_id", "TEXT"),
            ("agent_name", "TEXT"),
            ("cache_read_tokens", "INTEGER NOT NULL DEFAULT 0"),
            ("cache_write_tokens", "INTEGER NOT NULL DEFAULT 0"),
            ("reasoning_tokens", "INTEGER NOT NULL DEFAULT 0"),
            ("request_id", "TEXT"),
            ("source", "TEXT"),
            ("cost_usd", "DOUBLE PRECISION"),
            ("cost_basis", "TEXT"),
        ):
            if col not in cols:
                conn.execute(f"ALTER TABLE usage_events ADD COLUMN {col} {ddl}")
        for idx_sql in (
            "CREATE INDEX IF NOT EXISTS idx_usage_ts ON usage_events(ts)",
            "CREATE INDEX IF NOT EXISTS idx_usage_model ON usage_events(model)",
            "CREATE INDEX IF NOT EXISTS idx_usage_team ON usage_events(team)",
            "CREATE INDEX IF NOT EXISTS idx_usage_participant ON usage_events(participant)",
            "CREATE INDEX IF NOT EXISTS idx_usage_project ON usage_events(project_id)",
            "CREATE INDEX IF NOT EXISTS idx_usage_account ON usage_events(account_id)",
            "CREATE INDEX IF NOT EXISTS idx_usage_tool ON usage_events(tool)",
            "CREATE INDEX IF NOT EXISTS idx_usage_agent ON usage_events(agent_id)",
            "CREATE INDEX IF NOT EXISTS idx_usage_source ON usage_events(source)",
            """
            CREATE UNIQUE INDEX IF NOT EXISTS idx_usage_source_request
            ON usage_events(source, request_id)
            WHERE request_id IS NOT NULL AND TRIM(request_id) <> ''
            """,
        ):
            conn.execute(idx_sql)

        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS audit_log (
                id BIGSERIAL PRIMARY KEY,
                actor_key_hint TEXT,
                actor_name TEXT,
                actor_role TEXT,
                actor_account_id TEXT,
                action TEXT NOT NULL,
                filters_json TEXT,
                row_count INTEGER NOT NULL DEFAULT 0,
                created_at TEXT NOT NULL DEFAULT ((CURRENT_TIMESTAMP AT TIME ZONE 'UTC')::text)
            )
            """
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_audit_created ON audit_log(created_at)"
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_audit_action ON audit_log(action)"
        )
        _ensure_forum_tables(conn)


def count_events(exclude_seed: bool = False) -> int:
    with db() as conn:
        if exclude_seed:
            row = conn.execute(
                "SELECT COUNT(*) AS c FROM usage_events WHERE COALESCE(source, '') != 'seed'"
            ).fetchone()
        else:
            row = conn.execute("SELECT COUNT(*) AS c FROM usage_events").fetchone()
        return int(row["c"]) if row else 0


def clear_events(scope: str = "all") -> int:
    """Delete events. scope=all|seed. Returns deleted count."""
    with db() as conn:
        if scope == "seed":
            cur = conn.execute(
                "DELETE FROM usage_events WHERE COALESCE(source, '') = 'seed'"
            )
        else:
            cur = conn.execute("DELETE FROM usage_events")
        return int(cur.rowcount or 0)


def insert_audit(
    *,
    actor_key_hint: Optional[str],
    actor_name: Optional[str],
    actor_role: Optional[str],
    actor_account_id: Optional[str],
    action: str,
    filters: Optional[dict[str, Any]] = None,
    row_count: int = 0,
) -> int:
    values = (
        actor_key_hint,
        actor_name,
        actor_role,
        actor_account_id,
        action,
        json.dumps(filters or {}, ensure_ascii=False),
        int(row_count),
    )
    with db() as conn:
        if is_postgres():
            row = conn.execute(
                """
                INSERT INTO audit_log
                (actor_key_hint, actor_name, actor_role, actor_account_id,
                 action, filters_json, row_count)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                RETURNING id
                """,
                values,
            ).fetchone()
            return int(row["id"]) if row else 0
        cur = conn.execute(
            """
            INSERT INTO audit_log
            (actor_key_hint, actor_name, actor_role, actor_account_id,
             action, filters_json, row_count)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            values,
        )
        return int(cur.lastrowid or 0)


def list_audit(limit: int = 50) -> list[dict[str, Any]]:
    limit = max(1, min(int(limit), 500))
    with db() as conn:
        rows = conn.execute(
            """
            SELECT id, actor_key_hint, actor_name, actor_role, actor_account_id,
                   action, filters_json, row_count, created_at
            FROM audit_log
            ORDER BY id DESC
            LIMIT ?
            """,
            (limit,),
        ).fetchall()
    out: list[dict[str, Any]] = []
    for r in rows:
        filters: Any = {}
        raw = r["filters_json"]
        if raw:
            try:
                filters = json.loads(raw)
            except json.JSONDecodeError:
                filters = {"_raw": raw}
        out.append(
            {
                "id": int(r["id"]),
                "actor_key_hint": r["actor_key_hint"],
                "actor_name": r["actor_name"],
                "actor_role": r["actor_role"],
                "actor_account_id": r["actor_account_id"],
                "action": r["action"],
                "filters": filters,
                "row_count": int(r["row_count"] or 0),
                "created_at": r["created_at"],
            }
        )
    return out


def insert_usage_rows(rows: list[dict]) -> dict[str, Any]:
    """Bulk insert normalized usage rows with source+request_id idempotency.

    Returns {"ids": [...], "accepted": n, "skipped": n}.
    Duplicate (source, request_id) rows are skipped (not stacked).
    """
    if not rows:
        return {"ids": [], "accepted": 0, "skipped": 0}
    ids: list[int] = []
    accepted = 0
    skipped = 0
    pg = is_postgres()
    with db() as conn:
        for r in rows:
            request_id = r.get("request_id")
            if isinstance(request_id, str):
                request_id = request_id.strip() or None
            elif request_id is not None:
                request_id = str(request_id).strip() or None

            source = r.get("source")
            if isinstance(source, str):
                source = source.strip() or None
            elif source is not None:
                source = str(source).strip() or None

            cost_usd = r.get("cost_usd")
            if cost_usd is not None:
                try:
                    cost_usd = float(cost_usd)
                except (TypeError, ValueError):
                    cost_usd = None

            cost_basis = r.get("cost_basis")
            if isinstance(cost_basis, str):
                cost_basis = cost_basis.strip() or None

            values = (
                r["model"],
                int(r.get("prompt_tokens") or 0),
                int(r.get("completion_tokens") or 0),
                int(r.get("total_tokens") or 0),
                int(r.get("cache_read_tokens") or 0),
                int(r.get("cache_write_tokens") or 0),
                int(r.get("reasoning_tokens") or 0),
                r.get("team"),
                r.get("api_key"),
                r.get("tag"),
                r.get("participant"),
                r.get("tool"),
                r.get("account_id"),
                r.get("project_id"),
                r.get("agent_id"),
                r.get("agent_name"),
                request_id,
                source,
                cost_usd,
                cost_basis,
                r["ts"],
            )

            if pg:
                row = conn.execute(
                    """
                    INSERT INTO usage_events
                    (model, prompt_tokens, completion_tokens, total_tokens,
                     cache_read_tokens, cache_write_tokens, reasoning_tokens,
                     team, api_key, tag, participant, tool, account_id, project_id,
                     agent_id, agent_name,
                     request_id, source, cost_usd, cost_basis, ts)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT (source, request_id)
                    WHERE request_id IS NOT NULL AND TRIM(request_id) <> ''
                    DO NOTHING
                    RETURNING id
                    """,
                    values,
                ).fetchone()
                if row and row.get("id") is not None:
                    ids.append(int(row["id"]))
                    accepted += 1
                else:
                    skipped += 1
            else:
                cur = conn.execute(
                    """
                    INSERT OR IGNORE INTO usage_events
                    (model, prompt_tokens, completion_tokens, total_tokens,
                     cache_read_tokens, cache_write_tokens, reasoning_tokens,
                     team, api_key, tag, participant, tool, account_id, project_id,
                     agent_id, agent_name,
                     request_id, source, cost_usd, cost_basis, ts)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    values,
                )
                if cur.rowcount and cur.lastrowid:
                    ids.append(int(cur.lastrowid))
                    accepted += 1
                else:
                    skipped += 1
    return {"ids": ids, "accepted": accepted, "skipped": skipped}


def _ensure_forum_tables(conn: Any) -> None:
    """Create forum_posts for SQLite or Postgres."""
    if is_postgres():
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS forum_posts (
                id BIGSERIAL PRIMARY KEY,
                title TEXT NOT NULL,
                body TEXT NOT NULL,
                author_name TEXT NOT NULL,
                author_role TEXT,
                author_account_id TEXT,
                author_key_hint TEXT,
                likes INTEGER NOT NULL DEFAULT 0,
                created_at TEXT NOT NULL DEFAULT ((CURRENT_TIMESTAMP AT TIME ZONE 'UTC')::text)
            )
            """
        )
    else:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS forum_posts (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                title TEXT NOT NULL,
                body TEXT NOT NULL,
                author_name TEXT NOT NULL,
                author_role TEXT,
                author_account_id TEXT,
                author_key_hint TEXT,
                likes INTEGER NOT NULL DEFAULT 0,
                created_at TEXT NOT NULL DEFAULT (datetime('now'))
            )
            """
        )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_forum_created ON forum_posts(created_at)"
    )


def insert_forum_post(
    *,
    title: str,
    body: str,
    author_name: str,
    author_role: Optional[str] = None,
    author_account_id: Optional[str] = None,
    author_key_hint: Optional[str] = None,
    likes: int = 0,
    created_at: Optional[str] = None,
) -> int:
    values = (
        title,
        body,
        author_name,
        author_role,
        author_account_id,
        author_key_hint,
        int(likes or 0),
    )
    with db() as conn:
        _ensure_forum_tables(conn)
        if created_at:
            if is_postgres():
                row = conn.execute(
                    """
                    INSERT INTO forum_posts
                    (title, body, author_name, author_role, author_account_id,
                     author_key_hint, likes, created_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                    RETURNING id
                    """,
                    values + (created_at,),
                ).fetchone()
                return int(row["id"]) if row else 0
            cur = conn.execute(
                """
                INSERT INTO forum_posts
                (title, body, author_name, author_role, author_account_id,
                 author_key_hint, likes, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                values + (created_at,),
            )
            return int(cur.lastrowid or 0)

        if is_postgres():
            row = conn.execute(
                """
                INSERT INTO forum_posts
                (title, body, author_name, author_role, author_account_id,
                 author_key_hint, likes)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                RETURNING id
                """,
                values,
            ).fetchone()
            return int(row["id"]) if row else 0
        cur = conn.execute(
            """
            INSERT INTO forum_posts
            (title, body, author_name, author_role, author_account_id,
             author_key_hint, likes)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            values,
        )
        return int(cur.lastrowid or 0)


def list_forum_posts(limit: int = 50, offset: int = 0) -> list[dict[str, Any]]:
    limit = max(1, min(int(limit), 200))
    offset = max(0, int(offset))
    with db() as conn:
        _ensure_forum_tables(conn)
        rows = conn.execute(
            """
            SELECT id, title, body, author_name, author_role, author_account_id,
                   author_key_hint, likes, created_at
            FROM forum_posts
            ORDER BY created_at DESC, id DESC
            LIMIT ? OFFSET ?
            """,
            (limit, offset),
        ).fetchall()
    return [
        {
            "id": int(r["id"]),
            "title": r["title"],
            "body": r["body"],
            "author_name": r["author_name"],
            "author_role": r["author_role"],
            "author_account_id": r["author_account_id"],
            "author_key_hint": r["author_key_hint"],
            "likes": int(r["likes"] or 0),
            "created_at": r["created_at"],
        }
        for r in rows
    ]


def count_forum_posts() -> int:
    with db() as conn:
        _ensure_forum_tables(conn)
        row = conn.execute("SELECT COUNT(*) AS c FROM forum_posts").fetchone()
        return int(row["c"]) if row else 0


def like_forum_post(post_id: int) -> Optional[dict[str, Any]]:
    with db() as conn:
        _ensure_forum_tables(conn)
        cur = conn.execute(
            "UPDATE forum_posts SET likes = likes + 1 WHERE id = ?",
            (int(post_id),),
        )
        if not cur.rowcount:
            return None
        row = conn.execute(
            """
            SELECT id, title, body, author_name, author_role, author_account_id,
                   author_key_hint, likes, created_at
            FROM forum_posts WHERE id = ?
            """,
            (int(post_id),),
        ).fetchone()
    if not row:
        return None
    return {
        "id": int(row["id"]),
        "title": row["title"],
        "body": row["body"],
        "author_name": row["author_name"],
        "author_role": row["author_role"],
        "author_account_id": row["author_account_id"],
        "author_key_hint": row["author_key_hint"],
        "likes": int(row["likes"] or 0),
        "created_at": row["created_at"],
    }
