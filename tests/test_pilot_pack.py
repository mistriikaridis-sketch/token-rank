"""Enterprise pilot: account × project splits, alerts, and FinOps export."""
from __future__ import annotations

import csv
import io
import json
import os
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

os.environ.setdefault("DATA_DIR", tempfile.mkdtemp(prefix="tr-pilot-"))
os.environ.setdefault("TOKEN_RANKINGS_API_KEY", "test-write-key")
os.environ.setdefault("SEED_ON_START", "0")
os.environ.setdefault("FORUM_SEED_ON_START", "0")
os.environ.pop("DATABASE_URL", None)

from fastapi.testclient import TestClient  # noqa: E402

from app.db import db, insert_usage_rows  # noqa: E402
from app.main import app  # noqa: E402

UNKNOWN_MODEL = "企业内部-不存在的模型"
WRITE = {"Authorization": "Bearer test-write-key"}
MEMBER = {"Authorization": "Bearer pilot-member-key"}


def _iso(days_ago: int) -> str:
    return (datetime.now(timezone.utc) - timedelta(days=days_ago, hours=1)).isoformat()


def _row(**kwargs: object) -> dict:
    base = {
        "model": "gpt-4o",
        "prompt_tokens": 0,
        "completion_tokens": 0,
        "total_tokens": 0,
        "team": "试点部",
        "participant": "试点同事",
        "tool": "cursor",
        "source": "pilot-pack",
        "cost_basis": "invoice",
    }
    base.update(kwargs)
    return base


class PilotPackTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls._env = {
            "USERS_JSON": os.environ.get("USERS_JSON"),
            "ALERT_ACCOUNT_THRESHOLDS": os.environ.get("ALERT_ACCOUNT_THRESHOLDS"),
            "ALERT_SPEND_THRESHOLD_USD": os.environ.get("ALERT_SPEND_THRESHOLD_USD"),
        }
        os.environ["USERS_JSON"] = json.dumps(
            [
                {
                    "api_key": "pilot-admin-key",
                    "role": "admin",
                    "name": "Pilot Admin",
                },
                {
                    "api_key": "pilot-member-key",
                    "role": "member",
                    "account_id": "pilot-alpha",
                    "name": "甲",
                },
                {
                    "api_key": "pilot-idle-key",
                    "role": "member",
                    "account_id": "pilot-idle",
                    "name": "闲置号",
                },
            ]
        )
        os.environ["ALERT_ACCOUNT_THRESHOLDS"] = json.dumps(
            {"pilot-alpha": {"min_tokens": 100, "spend_usd": 1000}}
        )
        os.environ.pop("ALERT_SPEND_THRESHOLD_USD", None)
        cls.client = TestClient(app)
        cls.client.get("/health")
        with db() as conn:
            conn.execute("DELETE FROM usage_events WHERE source = ?", ("pilot-pack",))
        inserted = insert_usage_rows(
            [
                _row(
                    prompt_tokens=1000,
                    completion_tokens=1000,
                    total_tokens=2000,
                    account_id="pilot-alpha",
                    project_id="billing",
                    agent_id="bot-1",
                    agent_name="账单助手",
                    request_id="pilot-pack-alpha-billing",
                    cost_usd=10.0,
                    ts=_iso(1),
                ),
                _row(
                    prompt_tokens=300,
                    completion_tokens=200,
                    total_tokens=500,
                    account_id="pilot-alpha",
                    project_id="mobile",
                    tool="claude_code",
                    agent_id="bot-1",
                    agent_name="账单助手",
                    request_id="pilot-pack-alpha-mobile",
                    cost_usd=4.0,
                    ts=_iso(1),
                ),
                _row(
                    prompt_tokens=80,
                    completion_tokens=20,
                    total_tokens=100,
                    account_id="pilot-alpha",
                    project_id="billing",
                    agent_id="bot-1",
                    request_id="pilot-pack-alpha-old",
                    cost_usd=1.0,
                    ts=_iso(10),
                ),
                _row(
                    model=UNKNOWN_MODEL,
                    prompt_tokens=20000,
                    completion_tokens=0,
                    total_tokens=20000,
                    account_id="pilot-beta",
                    project_id="billing",
                    agent_id="bot-1",
                    agent_name="账单助手",
                    request_id="pilot-pack-beta",
                    cost_usd=None,
                    cost_basis=None,
                    ts=_iso(1),
                ),
                _row(
                    prompt_tokens=40,
                    completion_tokens=10,
                    total_tokens=50,
                    participant="低用量同事",
                    account_id="pilot-gamma",
                    project_id="notes",
                    agent_id="gamma-bot",
                    request_id="pilot-pack-gamma",
                    cost_usd=0.2,
                    ts=_iso(1),
                ),
                _row(
                    prompt_tokens=12000,
                    completion_tokens=8000,
                    total_tokens=20000,
                    participant="费用同事",
                    account_id="pilot-spend",
                    project_id="ops",
                    agent_id="spend-bot",
                    request_id="pilot-pack-spend-now",
                    cost_usd=80.0,
                    ts=_iso(1),
                ),
                _row(
                    prompt_tokens=4000,
                    completion_tokens=3000,
                    total_tokens=7000,
                    participant="费用同事",
                    account_id="pilot-spend",
                    project_id="ops",
                    agent_id="spend-bot",
                    request_id="pilot-pack-spend-old",
                    cost_usd=70.0,
                    ts=_iso(10),
                ),
            ]
        )
        if inserted["accepted"] < 1 and inserted["skipped"] < 1:
            raise AssertionError(inserted)

    @classmethod
    def tearDownClass(cls) -> None:
        for key, value in cls._env.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
        with db() as conn:
            conn.execute("DELETE FROM usage_events WHERE source = ?", ("pilot-pack",))

    def test_projects_stay_split_by_account_and_project(self) -> None:
        res = self.client.get(
            "/api/v1/rankings/projects",
            params={"window": "all", "account_id": "pilot-alpha", "hide_unlabeled": "true"},
        )
        self.assertEqual(res.status_code, 200, res.text)
        body = res.json()
        self.assertEqual(body["totals"]["breakdown"], "account_id × project_id")
        projects = {item["project_id"]: item for item in body["items"]}
        self.assertEqual(set(projects), {"billing", "mobile"})
        self.assertTrue(all(item["account_id"] == "pilot-alpha" for item in body["items"]))
        self.assertNotEqual(projects["billing"]["total_tokens"], projects["mobile"]["total_tokens"])
        self.assertIn("pilot-alpha", body["filters"]["accounts"])
        self.assertIn("billing", body["filters"]["projects"])

    def test_people_splits_are_not_only_a_total(self) -> None:
        res = self.client.get(
            "/api/v1/rankings/people",
            params={"window": "all", "q": "试点同事"},
        )
        self.assertEqual(res.status_code, 200, res.text)
        person = res.json()["items"][0]
        self.assertEqual(person["account_count"], 2)
        self.assertGreaterEqual(person["project_count"], 2)
        self.assertGreaterEqual(len(person["splits"]), 3)
        self.assertEqual(
            sum(row["total_tokens"] for row in person["splits"]),
            person["total_tokens"],
        )
        accounts = {row["account_id"] for row in person["splits"]}
        self.assertEqual(accounts, {"pilot-alpha", "pilot-beta"})

        scoped = self.client.get(
            "/api/v1/rankings/people",
            params={"window": "all", "q": "试点同事", "account_id": "pilot-beta"},
        )
        only = scoped.json()["items"][0]
        self.assertEqual(only["account_count"], 1)
        self.assertTrue(all(row["account_id"] == "pilot-beta" for row in only["splits"]))
        self.assertEqual(only["total_tokens"], 20000)

    def test_member_cannot_read_another_account(self) -> None:
        denied = self.client.get(
            "/api/v1/rankings/projects",
            params={"window": "all", "account_id": "pilot-beta"},
            headers=MEMBER,
        )
        self.assertEqual(denied.status_code, 403)
        own = self.client.get(
            "/api/v1/rankings/people",
            params={"window": "all", "q": "试点同事"},
            headers=MEMBER,
        )
        self.assertEqual(own.status_code, 200, own.text)
        person = own.json()["items"][0]
        self.assertEqual({row["account_id"] for row in person["splits"]}, {"pilot-alpha"})
        self.assertNotIn("pilot-beta", own.json()["filters"]["accounts"])

    def test_account_project_group_and_agents_do_not_merge_accounts(self) -> None:
        ranked = self.client.get(
            "/api/v1/rankings",
            params={"window": "all", "group_by": "account_project", "account_id": "pilot-alpha"},
        )
        self.assertEqual(ranked.status_code, 200, ranked.text)
        body = ranked.json()
        self.assertEqual(body["totals"]["breakdown"], "account_id × project_id")
        self.assertEqual(
            {(item["account_id"], item["project_id"]) for item in body["items"]},
            {("pilot-alpha", "billing"), ("pilot-alpha", "mobile")},
        )

        agents = self.client.get(
            "/api/v1/rankings/agents",
            params={"window": "all", "hide_unlabeled": "false"},
        )
        bots = [item for item in agents.json()["items"] if item["agent_id"] == "bot-1"]
        self.assertEqual(
            sorted(item["account_id"] for item in bots),
            ["pilot-alpha", "pilot-beta"],
        )
        alpha = next(item for item in bots if item["account_id"] == "pilot-alpha")
        self.assertGreaterEqual(alpha["project_count"], 2)
        self.assertIn("billing", alpha["projects"])
        self.assertIn("mobile", alpha["projects"])

    def test_orgs_include_account_project_splits(self) -> None:
        res = self.client.get(
            "/api/v1/rankings/orgs",
            params={"window": "all", "account_id": "pilot-alpha"},
        )
        self.assertEqual(res.status_code, 200, res.text)
        body = res.json()
        team = next(item for item in body["items"] if item["team"] == "试点部")
        self.assertGreaterEqual(len(team["splits"]), 2)
        self.assertTrue(all(row["account_id"] == "pilot-alpha" for row in team["splits"]))
        self.assertIn("accounts", body["filters"])
        self.assertIn("projects", body["filters"])

    def test_pilot_alerts_idle_low_spike_and_spend(self) -> None:
        res = self.client.get(
            "/api/v1/alerts",
            params={
                "window": "7d",
                "threshold_tokens": 1000,
                "spend_threshold_usd": 50,
                "spike_ratio": 2,
                "spike_min_usd": 1,
            },
            headers=WRITE,
        )
        self.assertEqual(res.status_code, 200, res.text)
        body = res.json()
        self.assertIn("费用突增", body["narrative"])
        self.assertIn("ALERT_ACCOUNT_THRESHOLDS", body["configure"])
        by_id = {row["account_id"]: row for row in body["by_account"]}
        self.assertIn("pilot-idle", by_id)
        self.assertEqual(by_id["pilot-idle"]["alert_rules"], ["idle"])
        self.assertGreaterEqual(len(by_id["pilot-alpha"]["projects"]), 2)
        self.assertIn("spend_spike", by_id["pilot-alpha"]["alert_rules"])
        self.assertNotIn("account_spend", by_id["pilot-alpha"]["alert_rules"])
        self.assertNotIn("low_usage", by_id["pilot-alpha"]["alert_rules"])
        self.assertIn("low_usage", by_id["pilot-gamma"]["alert_rules"])
        self.assertEqual(by_id["pilot-spend"]["alert_rules"], ["account_spend"])
        rules = {(item["account_id"], item["rule"]) for item in body["alerts"]}
        self.assertIn(("pilot-alpha", "spend_spike"), rules)
        self.assertIn(("pilot-idle", "idle"), rules)
        spike = next(
            item
            for item in body["alerts"]
            if item["account_id"] == "pilot-alpha" and item["rule"] == "spend_spike"
        )
        self.assertIn("账号", spike["message"])
        self.assertGreaterEqual(len(spike["projects"]), 2)

        member = self.client.get("/api/v1/alerts", params={"window": "7d"}, headers=MEMBER)
        self.assertEqual(member.status_code, 200, member.text)
        scoped = member.json()
        self.assertEqual(scoped["scope"], "self")
        self.assertEqual(
            [row["account_id"] for row in scoped["by_account"]],
            ["pilot-alpha"],
        )

        legacy = self.client.get(
            "/api/v1/alerts/low-usage",
            params={"window": "7d", "threshold_tokens": 10000},
            headers=MEMBER,
        )
        self.assertEqual(legacy.status_code, 200, legacy.text)
        self.assertEqual(legacy.json()["scope"], "self")
        self.assertEqual(legacy.json()["accounts_evaluated"], 1)

    def test_finops_export_keeps_unknown_model_and_member_scope(self) -> None:
        res = self.client.get(
            "/api/v1/export/usage.json",
            params={"window": "all", "grain": "finops", "account_id": "pilot-beta"},
            headers=WRITE,
        )
        self.assertEqual(res.status_code, 200, res.text)
        body = res.json()
        self.assertEqual(body["dimensions"], ["account_id", "project_id", "model", "tool"])
        self.assertEqual(body["count"], 1)
        item = body["items"][0]
        self.assertEqual(item["account_id"], "pilot-beta")
        self.assertEqual(item["project_id"], "billing")
        self.assertEqual(item["model"], UNKNOWN_MODEL)
        self.assertEqual(item["tool"], "cursor")
        self.assertEqual(item["price_tier"], "default")
        self.assertTrue(item["estimate"])
        self.assertGreater(item["estimated_cost_usd"], 0)

        csv_res = self.client.get(
            "/api/v1/export/usage.csv",
            params={"window": "all", "grain": "finops", "account_id": "pilot-beta"},
            headers=WRITE,
        )
        self.assertEqual(csv_res.status_code, 200, csv_res.text)
        rows = list(csv.DictReader(io.StringIO(csv_res.text)))
        self.assertEqual(list(rows[0]), [
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
        ])
        self.assertEqual(rows[0]["model"], UNKNOWN_MODEL)
        self.assertEqual(rows[0]["estimate"], "true")
        self.assertEqual(rows[0]["price_tier"], "default")

        events = self.client.get(
            "/api/v1/export/usage.csv",
            params={"window": "all", "account_id": "pilot-beta"},
            headers=WRITE,
        )
        self.assertTrue(events.text.startswith("id,ts,model,"))
        self.assertIn(UNKNOWN_MODEL, events.text)

        scoped = self.client.get(
            "/api/v1/export/usage.json",
            params={"window": "all", "grain": "finops"},
            headers=MEMBER,
        )
        self.assertEqual(scoped.status_code, 200, scoped.text)
        self.assertTrue(scoped.json()["items"])
        self.assertTrue(all(row["account_id"] == "pilot-alpha" for row in scoped.json()["items"]))
        denied = self.client.get(
            "/api/v1/export/usage.json",
            params={"window": "all", "grain": "finops", "account_id": "pilot-beta"},
            headers=MEMBER,
        )
        self.assertEqual(denied.status_code, 403)

    def test_docs_and_ui_describe_the_pilot_pack(self) -> None:
        install = (ROOT / "docs" / "INSTALL.md").read_text(encoding="utf-8")
        self.assertIn("企业试点上线清单", install)
        for phrase in (
            "POSTGRES_PASSWORD",
            "RANKINGS_REQUIRE_AUTH",
            "reverse_proxy",
            "collector/INSTALL.md",
            "demo-alice-key",
            "ALERT_ACCOUNT_THRESHOLDS",
            "grain=finops",
        ):
            self.assertIn(phrase, install)
        ui = (ROOT / "static" / "index.html").read_text(encoding="utf-8")
        self.assertIn('id="filterAccount"', ui)
        self.assertIn('id="filterProject"', ui)
        self.assertIn('id="projectFilterProject"', ui)
        self.assertIn('grain: "finops"', ui)
        self.assertIn("费用突增", ui)
        self.assertIn("账号 × 项目", ui)


if __name__ == "__main__":
    unittest.main()
