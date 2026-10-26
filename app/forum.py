"""Minimal internal forum: posts + likes. No full community features."""
from __future__ import annotations

import json
import os
import re
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Optional

from fastapi import HTTPException

from app.db import count_forum_posts, insert_forum_post, list_forum_posts

TITLE_MAX = 120
BODY_MAX = 2000

# Reject posts that look like they paste secrets / keys / conversation dumps
_SECRET_PATTERNS = [
    re.compile(r"\bsk-[a-zA-Z0-9_\-]{8,}\b"),
    re.compile(r"\bsk-proj-[a-zA-Z0-9_\-]{8,}\b"),
    re.compile(r"\bBearer\s+[a-zA-Z0-9._\-]{12,}\b", re.I),
    re.compile(r"\bTOKEN_RANKINGS_API_KEY\b"),
    re.compile(r"\b(api[_-]?key|secret[_-]?key|access[_-]?token)\s*[:=]\s*\S+", re.I),
    re.compile(r"\b(xox[baprs]-|ghp_|gho_|github_pat_)[a-zA-Z0-9_\-]{8,}\b"),
    re.compile(r"-----BEGIN (RSA |EC |OPENSSH )?PRIVATE KEY-----"),
]


def looks_like_secret(text: str) -> Optional[str]:
    if not text:
        return None
    for pat in _SECRET_PATTERNS:
        if pat.search(text):
            return pat.pattern
    return None


def validate_post_content(title: str, body: str) -> tuple[str, str]:
    title = (title or "").strip()
    body = (body or "").strip()
    if not title:
        raise HTTPException(
            status_code=400,
            detail={"error": "title_required", "message": "标题不能为空"},
        )
    if not body:
        raise HTTPException(
            status_code=400,
            detail={"error": "body_required", "message": "正文不能为空"},
        )
    if len(title) > TITLE_MAX:
        raise HTTPException(
            status_code=400,
            detail={
                "error": "title_too_long",
                "message": f"标题最多 {TITLE_MAX} 字",
                "max": TITLE_MAX,
            },
        )
    if len(body) > BODY_MAX:
        raise HTTPException(
            status_code=400,
            detail={
                "error": "body_too_long",
                "message": f"正文最多 {BODY_MAX} 字",
                "max": BODY_MAX,
            },
        )
    for label, text in (("title", title), ("body", body)):
        hit = looks_like_secret(text)
        if hit:
            raise HTTPException(
                status_code=400,
                detail={
                    "error": "secrets_forbidden",
                    "message": (
                        "禁止在交流区粘贴 API Key、Token、私钥或疑似密钥内容。"
                        "企业内部讨论只谈用量与工作流，不存敏感 Key / 对话原文。"
                    ),
                    "field": label,
                },
            )
    return title, body


def seed_forum_if_empty(force: bool = False) -> int:
    """Load fixtures/forum_posts.json when table empty (or force)."""
    if not force and count_forum_posts() > 0:
        return 0
    path = Path(
        os.environ.get("FORUM_SEED_FILE")
        or Path(__file__).resolve().parent.parent / "fixtures" / "forum_posts.json"
    )
    if not path.is_file():
        return 0
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return 0
    if not isinstance(data, list):
        return 0

    # Clear only when force and already has posts
    if force and count_forum_posts() > 0:
        from app.db import db, _ensure_forum_tables

        with db() as conn:
            _ensure_forum_tables(conn)
            conn.execute("DELETE FROM forum_posts")

    now = datetime.now(timezone.utc)
    n = 0
    for i, item in enumerate(data):
        if not isinstance(item, dict):
            continue
        title = str(item.get("title") or "").strip()
        body = str(item.get("body") or "").strip()
        if not title or not body:
            continue
        author = str(item.get("author_name") or item.get("author") or "演示用户").strip()
        likes = int(item.get("likes") or 0)
        hours_ago = item.get("hours_ago")
        if hours_ago is not None:
            created_at = (now - timedelta(hours=float(hours_ago))).isoformat()
        else:
            created_at = item.get("created_at") or (
                now - timedelta(hours=i + 1)
            ).isoformat()
        insert_forum_post(
            title=title[:TITLE_MAX],
            body=body[:BODY_MAX],
            author_name=author[:128],
            author_role=str(item.get("author_role") or "member")[:32],
            author_account_id=(
                str(item["author_account_id"]).strip()
                if item.get("author_account_id")
                else None
            ),
            author_key_hint=str(item.get("author_key_hint") or "seed")[:32],
            likes=likes,
            created_at=str(created_at),
        )
        n += 1
    return n


def forum_disclaimer() -> str:
    return (
        "企业内部讨论区（最小闭环）：可匿名浏览、登录后发帖与点赞。"
        "请勿粘贴 API Key、Token、私钥或对话原文；本区不存敏感密钥。"
        "不是全功能社区，不做公网热度榜。"
    )
