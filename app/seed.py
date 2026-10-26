"""Insert demo usage events for local MVP demos (source=seed)."""
from __future__ import annotations

import random
from datetime import datetime, timedelta, timezone

from app.db import clear_events, db, init_db
from app.tools import normalize_tool

MODELS = [
    "gpt-4o",
    "gpt-4o-mini",
    "claude-3-5-sonnet",
    "claude-3-5-haiku",
    "gemini-1.5-flash",
    "deepseek-chat",
    "qwen-plus",
    "gpt-3.5-turbo",
]

# Demo departments (org board)
TEAMS = ["平台研发", "客户成功", "算法研究", "销售赋能", "运维效能", "产品设计"]

# Demo participants (personal board) — fictional, not copied from references
PARTICIPANTS = [
    "小陈",
    "Alex",
    "阿宁",
    "Mira",
    "老周",
    "Kai",
    "小林",
    "Nova",
    "阿文",
    "Sam",
    "阿哲",
    "Rita",
]

# Canonical tool slugs (must match app.tools)
TOOLS = [
    "cursor",
    "claude_code",
    "continue",
    "openwebui",
    "litellm",
    "custom_script",
]

# Prefer certain model/tool per participant for stable "primary" columns
PARTICIPANT_PREFS = {
    "小陈": ("cursor", "claude-3-5-sonnet", "平台研发", "demo-chatbot"),
    "Alex": ("claude_code", "gpt-4o", "算法研究", "demo-eval"),
    "阿宁": ("cursor", "gpt-4o-mini", "平台研发", "demo-rag"),
    "Mira": ("continue", "deepseek-chat", "客户成功", "demo-cs"),
    "老周": ("litellm", "qwen-plus", "运维效能", "demo-ops"),
    "Kai": ("claude_code", "claude-3-5-sonnet", "算法研究", "demo-train"),
    "小林": ("openwebui", "gemini-1.5-flash", "销售赋能", "demo-sales"),
    "Nova": ("cursor", "gpt-4o-mini", "产品设计", "demo-design"),
    "阿文": ("custom_script", "deepseek-chat", "运维效能", "demo-batch"),
    "Sam": ("continue", "claude-3-5-haiku", "客户成功", "demo-kb"),
    "阿哲": ("claude_code", "gpt-4o", "平台研发", "demo-agent"),
    "Rita": ("cursor", "qwen-plus", "产品设计", "demo-proto"),
}

TAGS = ["chatbot", "rag", "summarize", "coding", "batch"]

# Demo agent teams — some people run multiple agents
AGENT_POOL = [
    ("seed-agent-planner", "规划官"),
    ("seed-agent-coder", "编码特工"),
    ("seed-agent-reviewer", "评审官"),
    ("seed-agent-ops", "运维哨兵"),
    ("seed-agent-writer", "文档写作"),
]


def seed(force: bool = False, n: int = 280) -> int:
    init_db()
    with db() as conn:
        row = conn.execute(
            "SELECT COUNT(*) AS c FROM usage_events WHERE COALESCE(source, '') = 'seed'"
        ).fetchone()
        seed_existing = int(row["c"]) if row else 0
    if seed_existing > 0 and not force:
        return 0
    if force and seed_existing > 0:
        # Only remove previous demo rows — keep real ingest
        clear_events(scope="seed")

    now = datetime.now(timezone.utc)
    rows = []
    rng = random.Random(42)

    for i in range(n):
        participant = rng.choice(PARTICIPANTS)
        # Bias top users
        if i % 4 == 0:
            participant = "小陈"
        elif i % 6 == 0:
            participant = "Alex"
        elif i % 9 == 0:
            participant = "Kai"

        pref_tool, pref_model, pref_team, pref_project = PARTICIPANT_PREFS[participant]
        tool = normalize_tool(pref_tool if rng.random() < 0.72 else rng.choice(TOOLS))
        model = pref_model if rng.random() < 0.65 else rng.choice(MODELS)
        team = pref_team if rng.random() < 0.85 else rng.choice(TEAMS)
        project_id = pref_project if rng.random() < 0.8 else f"demo-misc-{rng.randint(1, 3)}"
        account_id = f"seed-{PARTICIPANTS.index(participant) + 1:02d}"

        prompt = rng.randint(400, 12000)
        completion = rng.randint(80, 3500)

        # Spread across calendar today / yesterday / last 30d (UTC)
        today0 = now.replace(hour=0, minute=0, second=0, microsecond=0)
        yesterday0 = today0 - timedelta(days=1)
        bucket = rng.random()
        if bucket < 0.30:
            span = max(int((now - today0).total_seconds()), 1)
            ts = (today0 + timedelta(seconds=rng.randint(0, span))).isoformat()
        elif bucket < 0.52:
            ts = (yesterday0 + timedelta(seconds=rng.randint(0, 86399))).isoformat()
        else:
            hours_ago = rng.randint(48, 24 * 30)
            ts = (now - timedelta(hours=hours_ago)).isoformat()
        key = f"sk-demo-{PARTICIPANTS.index(participant) + 1:02d}"
        # ~60% of rows get an agent (simulates agent-team heavy users)
        agent_id = None
        agent_name = None
        if rng.random() < 0.6:
            agent_id, agent_name = rng.choice(AGENT_POOL)
        rows.append(
            {
                "model": model,
                "prompt_tokens": prompt,
                "completion_tokens": completion,
                "total_tokens": prompt + completion,
                "team": team,
                "api_key": key,
                "tag": rng.choice(TAGS),
                "participant": participant,
                "tool": tool,
                "account_id": account_id,
                "project_id": project_id,
                "agent_id": agent_id,
                "agent_name": agent_name,
                "request_id": f"seed-{i:05d}",
                "source": "seed",
                "ts": ts,
            }
        )

    from app.db import insert_usage_rows

    result = insert_usage_rows(rows)
    return int(result.get("accepted") or 0)


if __name__ == "__main__":
    inserted = seed(force=True)
    print(f"Seeded {inserted} usage events.")
