"""Enterprise UX: onboarding token, privacy, dashboard, visibility, devices."""
from __future__ import annotations

import json
import os
import sys
import tempfile
import unittest
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

os.environ.setdefault("DATA_DIR", tempfile.mkdtemp(prefix="tr-product-"))
os.environ.setdefault("TOKEN_RANKINGS_API_KEY", "test-write-key")
os.environ.setdefault("SEED_ON_START", "0")
os.environ.setdefault("FORUM_SEED_ON_START", "0")

from fastapi.testclient import TestClient  # noqa: E402

from app.db import init_db  # noqa: E402
from app.main import app  # noqa: E402

init_db()

ALICE = "ux22-alice"
DAVE = "ux22-dave"
USERS = [
    {
        "name": "爱丽丝",
        "role": "member",
        "account_id": ALICE,
        "api_key": "ux-alice-key",
    },
    {
        "name": "戴夫",
        "role": "member",
        "account_id": DAVE,
        "api_key": "ux-dave-key",
    },
]


class ProductUxTests(unittest.TestCase):
    def setUp(self) -> None:
        self._users = os.environ.get("USERS_JSON")
        self._key = os.environ.get("TOKEN_RANKINGS_API_KEY")
        os.environ["USERS_JSON"] = json.dumps(USERS)
        os.environ["TOKEN_RANKINGS_API_KEY"] = self._key or "test-write-key"
        init_db()
        self.client = TestClient(app)
        self.write = {"Authorization": "Bearer " + os.environ["TOKEN_RANKINGS_API_KEY"]}

    def tearDown(self) -> None:
        if self._users is None:
            os.environ.pop("USERS_JSON", None)
        else:
            os.environ["USERS_JSON"] = self._users
        if self._key is None:
            os.environ.pop("TOKEN_RANKINGS_API_KEY", None)
        else:
            os.environ["TOKEN_RANKINGS_API_KEY"] = self._key

    def _ingest(
        self,
        account: str,
        tokens: int,
        *,
        tool: str = "cursor",
        when: datetime | None = None,
        cache_read: int = 0,
        participant: str | None = None,
        headers: dict | None = None,
    ):
        stamp = (when or datetime.now(timezone.utc)).isoformat()
        body = {
            "model": "gpt-4o-mini",
            "prompt_tokens": tokens,
            "completion_tokens": 10,
            "cache_read_tokens": cache_read,
            "total_tokens": tokens + 10 + cache_read,
            "account_id": account,
            "participant": participant or account,
            "team": "平台",
            "tool": tool,
            "project_id": "demo-proj",
            "source": "ux-test",
            "request_id": "ux-" + uuid.uuid4().hex,
            "timestamp": stamp,
        }
        response = self.client.post(
            "/api/v1/usage",
            json=body,
            headers=headers or self.write,
        )
        return response

    def test_version_and_pages(self) -> None:
        health = self.client.get("/health")
        self.assertEqual(health.status_code, 200)
        self.assertEqual(health.json()["version"], "0.22.0")
        for path, text in (
            ("/onboard", "如何接入"),
            ("/onboard", "勿转发"),
            ("/privacy", "永不采集"),
            ("/settings", "全公司"),
            ("/me", "我的用量"),
        ):
            page = self.client.get(path)
            self.assertEqual(page.status_code, 200, path)
            self.assertIn(text, page.text)

    def test_onboarding_hides_service_key_until_signed_in(self) -> None:
        service = os.environ["TOKEN_RANKINGS_API_KEY"]
        anon = self.client.get("/api/v1/onboarding")
        self.assertEqual(anon.status_code, 200)
        body = anon.text
        self.assertNotIn(service, body)
        self.assertNotIn("ctr_", body)
        self.assertFalse(anon.json()["token_ready"])
        self.assertIn("勿转发", anon.json()["warning"])

        signed = self.client.get(
            "/api/v1/onboarding",
            headers={"Authorization": "Bearer ux-alice-key"},
        )
        self.assertEqual(signed.status_code, 200)
        payload = signed.json()
        self.assertTrue(payload["token_ready"])
        self.assertIn("ctr_", payload["commands"]["unix"])
        self.assertNotIn(service, payload["commands"]["unix"])
        self.assertNotIn("opentoken", payload["commands"]["unix"].lower())
        self.assertIn("没有上传代码", payload["ai_prompt"])

        token = payload["commands"]["unix"].split("export TOKEN_RANKINGS_API_KEY=")[1].split("\n", 1)[0]
        token = token.strip().strip("'")
        script = self.client.get(
            "/api/v1/onboarding/install.sh?os=unix",
            headers={"Authorization": "Bearer " + token},
        )
        self.assertEqual(script.status_code, 200)
        self.assertIn(token, script.text)
        self.assertNotIn("opentoken", script.text.lower())
        self.assertIn("不解密", script.text)

    def test_collector_token_is_account_scoped(self) -> None:
        signed = self.client.get(
            "/api/v1/onboarding",
            headers={"Authorization": "Bearer ux-alice-key"},
        )
        token = signed.json()["commands"]["unix"].split("export TOKEN_RANKINGS_API_KEY=")[1]
        token = token.split("\n", 1)[0].strip().strip("'")
        denied = self.client.post(
            "/api/v1/admin/clear?scope=seed",
            headers={"Authorization": "Bearer " + token},
        )
        self.assertIn(denied.status_code, (401, 403))
        written = self._ingest(
            DAVE,
            1234,
            headers={"Authorization": "Bearer " + token},
        )
        self.assertEqual(written.status_code, 200, written.text)
        alice = self.client.get(
            "/api/v1/me/dashboard?window=all",
            headers={"Authorization": "Bearer ux-alice-key"},
        )
        self.assertEqual(alice.status_code, 200)
        self.assertGreaterEqual(alice.json()["overview"]["total_tokens"], 1234)
        dave = self.client.get(
            f"/api/v1/me/dashboard?window=all&account_id={DAVE}",
            headers={"Authorization": "Bearer ux-dave-key"},
        )
        self.assertEqual(dave.json()["overview"]["total_tokens"], 0)

    def test_privacy_dictionary_and_formula(self) -> None:
        privacy = self.client.get("/api/v1/privacy").json()
        buckets = {row["bucket"] for row in privacy["rows"]}
        self.assertEqual(buckets, {"会上传", "仅本地", "永不采集"})
        never = " ".join(row["item"] for row in privacy["rows"] if row["bucket"] == "永不采集")
        for word in ("代码", "对话", "截图", "密钥"):
            self.assertIn(word, never)
        self.assertTrue(any("preview" in cmd for cmd in privacy["dry_run"]["commands"]))
        self.assertNotIn("prompt", privacy["sample_event"])
        self.assertIn("不要读取对话", privacy["ai_audit_prompt"])
        self.assertIn("canoryn", privacy["ai_audit_prompt"])
        formula = self.client.get("/api/v1/pricing/formula").json()
        self.assertIn("缓存读", formula["formula_zh"])
        self.assertIn("列表价", formula["formula_zh"])
        self.assertEqual(formula["cost_basis"], "estimate")

    def test_dashboard_cache_heatmap_and_anomaly(self) -> None:
        now = datetime.now(timezone.utc)
        self._ingest(ALICE, 10_000, when=now - timedelta(days=4), participant="爱丽丝")
        self._ingest(ALICE, 10_000, when=now - timedelta(days=3), participant="爱丽丝")
        self._ingest(ALICE, 10_000, when=now - timedelta(days=2), participant="爱丽丝")
        self._ingest(ALICE, 10_000, when=now - timedelta(days=1), participant="爱丽丝")
        hot = self._ingest(
            ALICE,
            200_000,
            when=now,
            cache_read=1_000_000,
            participant="爱丽丝",
        )
        self.assertEqual(hot.status_code, 200, hot.text)
        dash = self.client.get(
            "/api/v1/me/dashboard?window=today",
            headers={"Authorization": "Bearer ux-alice-key"},
        )
        self.assertEqual(dash.status_code, 200, dash.text)
        body = dash.json()
        self.assertGreater(len(body["heatmap"]), 20)
        self.assertGreater(body["cache"]["cache_savings_usd"], 0)
        self.assertIn("cache_read", body["cache"]["parts_pct"])
        self.assertEqual(body["anomaly"]["status"], "review")
        self.assertEqual(body["anomaly"]["label"], "异常用量待审")
        self.assertGreaterEqual(body["overview"]["streak_days"], 1)
        self.assertTrue(body["detail"]["hourly"])
        self.assertEqual(body["timezones"][0]["timezone"], "Asia/Shanghai")
        tools = {row["tool"] for row in body["tools"]}
        self.assertIn("cursor", tools)

    def test_visibility_hides_self_from_company_board(self) -> None:
        self._ingest(ALICE, 50_000, participant="ux22-pub-person")
        self._ingest(DAVE, 80_000, participant="ux22-private-person")
        saved = self.client.put(
            "/api/v1/me/prefs",
            headers={"Authorization": "Bearer ux-dave-key"},
            json={"visibility": "self", "timezone": "Asia/Shanghai", "show_rank": False},
        )
        self.assertEqual(saved.status_code, 200, saved.text)
        self.assertEqual(saved.json()["visibility"], "self")
        anon = self.client.get("/api/v1/rankings/people?window=all&q=ux22-private-person")
        self.assertEqual(anon.status_code, 200, anon.text)
        names = [item["participant"] for item in anon.json()["items"]]
        self.assertNotIn("ux22-private-person", names)
        public = self.client.get("/api/v1/rankings/people?window=all&q=ux22-pub-person")
        self.assertIn("ux22-pub-person", [item["participant"] for item in public.json()["items"]])
        admin = self.client.get(
            "/api/v1/rankings/people?window=all&q=ux22-private-person",
            headers=self.write,
        )
        self.assertIn(
            "ux22-private-person",
            [item["participant"] for item in admin.json()["items"]],
        )

    def test_device_disable_skips_new_usage(self) -> None:
        first = self._ingest(ALICE, 100, tool="codex", participant="爱丽丝")
        self.assertEqual(first.status_code, 200, first.text)
        listing = self.client.get(
            "/api/v1/devices",
            headers={"Authorization": "Bearer ux-alice-key"},
        )
        self.assertEqual(listing.status_code, 200, listing.text)
        codex = [item for item in listing.json()["items"] if item["device_key"] == "codex"]
        self.assertTrue(codex, listing.text)
        renamed = self.client.patch(
            f"/api/v1/devices/{codex[0]['id']}",
            headers={"Authorization": "Bearer ux-alice-key"},
            json={"name": "爱丽丝的 Codex", "status": "disabled"},
        )
        self.assertEqual(renamed.status_code, 200, renamed.text)
        self.assertEqual(renamed.json()["name"], "爱丽丝的 Codex")
        blocked = self._ingest(ALICE, 999, tool="codex", participant="爱丽丝")
        self.assertEqual(blocked.status_code, 403, blocked.text)
        self.assertEqual(blocked.json()["detail"]["error"], "device_disabled")

    def test_feedback_rejects_secrets(self) -> None:
        ok = self.client.post(
            "/api/v1/feedback",
            headers={"Authorization": "Bearer ux-alice-key"},
            json={
                "tool_name": "Cursor",
                "note": "榜上是 0",
                "structure_text": "目录 ~/.cursor 存在\npreview 行数：0\n计划任务：没有",
            },
        )
        self.assertEqual(ok.status_code, 200, ok.text)
        bad = self.client.post(
            "/api/v1/feedback",
            headers={"Authorization": "Bearer ux-alice-key"},
            json={"tool_name": "Cursor", "structure_text": "key=sk-proj-abcdefghijklmnop"},
        )
        self.assertEqual(bad.status_code, 400)
        member = self._ingest(DAVE, 1, headers={"Authorization": "Bearer ux-alice-key"})
        self.assertEqual(member.status_code, 403)

    def test_three_day_window(self) -> None:
        response = self.client.get("/api/v1/rankings?window=3d")
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json()["window"], "3d")


if __name__ == "__main__":
    unittest.main()
