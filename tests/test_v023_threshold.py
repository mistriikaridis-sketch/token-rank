"""v0.23 open-source threshold: first install, tool matrix, pricing, privacy."""
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

os.environ.setdefault("DATA_DIR", tempfile.mkdtemp(prefix="tr-v23-"))
os.environ.setdefault("TOKEN_RANKINGS_API_KEY", "test-write-key")
os.environ.setdefault("SEED_ON_START", "0")
os.environ.setdefault("FORUM_SEED_ON_START", "0")
os.environ.pop("DATABASE_URL", None)

from fastapi.testclient import TestClient  # noqa: E402

from app.db import init_db  # noqa: E402
from app.main import app  # noqa: E402
from app.pricing import estimate_cost, estimate_usage_parts  # noqa: E402
from collector.config import CollectorConfig  # noqa: E402
from collector.diagnose import format_diagnose, run_diagnose  # noqa: E402

init_db()

ACCOUNT = "v23-fresh"
USERS = [
    {"name": "新手", "role": "member", "account_id": ACCOUNT, "api_key": "v23-member-key"},
]


class V23ThresholdTests(unittest.TestCase):
    def setUp(self) -> None:
        self._users = os.environ.get("USERS_JSON")
        os.environ["USERS_JSON"] = json.dumps(USERS)
        os.environ["TOKEN_RANKINGS_API_KEY"] = "test-write-key"
        init_db()
        self.client = TestClient(app)
        self.member = {"Authorization": "Bearer v23-member-key"}
        self.write = {"Authorization": "Bearer test-write-key"}

    def tearDown(self) -> None:
        if self._users is None:
            os.environ.pop("USERS_JSON", None)
        else:
            os.environ["USERS_JSON"] = self._users

    def _token(self) -> str:
        signed = self.client.get("/api/v1/onboarding", headers=self.member)
        self.assertEqual(signed.status_code, 200, signed.text)
        script = signed.json()["commands"]["unix"]
        token = script.split("export TOKEN_RANKINGS_API_KEY=", 1)[1].split("\n", 1)[0]
        return token.strip().strip("'").strip('"')

    def test_install_scripts_and_ai_prompt(self) -> None:
        body = self.client.get("/api/v1/onboarding", headers=self.member).json()
        for key in ("macos", "linux", "windows", "macos_curl", "linux_curl", "windows_curl"):
            text = body["commands"][key]
            self.assertIn("失败", text, key)
            self.assertIn("下一步", text, key)
            self.assertNotIn("opentoken", text.lower(), key)
        self.assertIn("ctr_", body["commands"]["macos"])
        self.assertIn("不解密", body["commands"]["windows"])
        self.assertIn("最后 30 行", body["ai_prompt"])
        self.assertIn("没有上传代码", body["ai_prompt"])
        self.assertIn("只装上报", body["ai_prompt"])
        anon = self.client.get("/api/v1/onboarding").json()
        self.assertNotIn("ctr_", anon["commands"]["macos"])
        self.assertNotIn("test-write-key", json.dumps(anon))

    def test_connection_codes(self) -> None:
        token = self._token()
        denied = self.client.post(
            "/api/v1/onboarding/report",
            headers={"Authorization": "Bearer not-a-key"},
            json={"client_time": datetime.now(timezone.utc).isoformat(), "has_local_logs": False},
        )
        self.assertEqual(denied.status_code, 401)
        self.assertEqual(denied.json()["detail"]["error"], "invalid_key")

        quiet = self.client.post(
            "/api/v1/onboarding/report",
            headers={"Authorization": "Bearer " + token},
            json={
                "client_time": datetime.now(timezone.utc).isoformat(),
                "detected": ["cursor"],
                "missing": ["codex", "claude_code", "continue", "workbuddy", "procode"],
                "local_rows": 0,
                "has_local_logs": False,
            },
        )
        self.assertEqual(quiet.status_code, 200, quiet.text)
        self.assertEqual(quiet.json()["error_code"], "no_local_logs")
        self.assertIn("cursor", quiet.json()["detected"])
        self.assertIn("codex", quiet.json()["missing"])

        skewed = self.client.get(
            "/api/v1/onboarding/status",
            headers=self.member,
            params={"client_time": (datetime.now(timezone.utc) - timedelta(hours=2)).isoformat()},
        )
        self.assertEqual(skewed.status_code, 200, skewed.text)
        self.assertEqual(skewed.json()["error_code"], "clock_skew")
        self.assertIn("自动设置", skewed.json()["next_step"])

        clear = self.client.post(
            "/api/v1/admin/clear?scope=all",
            headers={"Authorization": "Bearer " + token},
        )
        self.assertNotEqual(clear.status_code, 200)
        scan = self.client.post(
            "/api/v1/scan/workbuddy",
            headers={"Authorization": "Bearer " + token},
        )
        self.assertNotEqual(scan.status_code, 200)

    def test_tool_matrix_three_states(self) -> None:
        headers = self.write
        now = datetime.now(timezone.utc).isoformat()
        created = self.client.post(
            "/api/v1/usage",
            headers=headers,
            json={
                "model": "gpt-4o-mini",
                "prompt_tokens": 100,
                "completion_tokens": 10,
                "total_tokens": 110,
                "account_id": ACCOUNT,
                "participant": "新手",
                "tool": "cursor",
                "source": "v23",
                "request_id": "v23-cursor-" + uuid.uuid4().hex,
                "timestamp": now,
            },
        )
        self.assertEqual(created.status_code, 200, created.text)
        self.client.post(
            "/api/v1/usage",
            headers=headers,
            json={
                "model": "企业内部-不存在的模型",
                "prompt_tokens": 50,
                "completion_tokens": 5,
                "total_tokens": 55,
                "account_id": ACCOUNT,
                "participant": "新手",
                "tool": "claude_code",
                "source": "v23",
                "request_id": "v23-unknown-" + uuid.uuid4().hex,
                "timestamp": now,
            },
        )
        devices = self.client.get("/api/v1/devices", headers=self.member).json()["items"]
        claude = next(item for item in devices if item["device_key"] == "claude_code")
        patched = self.client.patch(
            f"/api/v1/devices/{claude['id']}",
            headers=self.member,
            json={"status": "disabled"},
        )
        self.assertEqual(patched.status_code, 200, patched.text)
        dash = self.client.get("/api/v1/me/dashboard?window=all", headers=self.member)
        self.assertEqual(dash.status_code, 200, dash.text)
        body = dash.json()
        states = {row["tool"]: row for row in body["tool_matrix"]}
        self.assertEqual(set(states), {"cursor", "claude_code", "codex", "continue", "workbuddy", "procode"})
        self.assertEqual(states["cursor"]["state"], "has_data")
        self.assertEqual(states["cursor"]["state_label"], "有数")
        self.assertEqual(states["claude_code"]["state"], "disabled")
        self.assertEqual(states["claude_code"]["state_label"], "已禁用")
        self.assertEqual(states["procode"]["state"], "not_detected")
        self.assertIn("不解密", states["procode"]["copy"])
        self.assertIn("不用管", states["codex"]["copy"])
        unknown = next(row for row in body["models"] if row["model"] == "企业内部-不存在的模型")
        self.assertTrue(unknown["estimate"])
        self.assertEqual(unknown["estimate_label"], "估算")
        self.assertEqual(body["pricing_table"]["table_version"], "2026.10.28")

    def test_cache_toggle_defaults_off(self) -> None:
        account = "v23-cache"
        os.environ["USERS_JSON"] = json.dumps(
            USERS + [{"name": "缓存", "role": "member", "account_id": account, "api_key": "v23-cache-key"}]
        )
        stamp = datetime.now(timezone.utc).isoformat()
        written = self.client.post(
            "/api/v1/usage",
            headers=self.write,
            json={
                "model": "gpt-4o-mini",
                "prompt_tokens": 0,
                "completion_tokens": 0,
                "cache_read_tokens": 1_000_000,
                "total_tokens": 1_000_000,
                "account_id": account,
                "participant": "缓存",
                "tool": "cursor",
                "source": "v23",
                "request_id": "v23-cache-" + uuid.uuid4().hex,
                "timestamp": stamp,
            },
        )
        self.assertEqual(written.status_code, 200, written.text)
        plain = self.client.get(
            f"/api/v1/rankings/people?window=all&account_id={account}",
            headers=self.write,
        )
        cached = self.client.get(
            f"/api/v1/rankings/people?window=all&account_id={account}&include_cache=true",
            headers=self.write,
        )
        self.assertEqual(plain.status_code, 200, plain.text)
        self.assertFalse(plain.json()["totals"]["include_cache"])
        self.assertTrue(cached.json()["totals"]["include_cache"])
        plain_cost = plain.json()["totals"]["estimated_cost_usd"]
        cached_cost = cached.json()["totals"]["estimated_cost_usd"]
        self.assertAlmostEqual(plain_cost, estimate_cost("gpt-4o-mini", 0, 0), places=4)
        self.assertGreater(cached_cost, plain_cost)
        self.assertAlmostEqual(
            cached_cost,
            estimate_usage_parts("gpt-4o-mini", 0, 0, 1_000_000, 0)["total_usd"],
            places=4,
        )

    def test_privacy_docs_match_and_formula_version(self) -> None:
        privacy = self.client.get("/api/v1/privacy").json()
        readme = (ROOT / "README.md").read_text(encoding="utf-8")
        security = (ROOT / "docs" / "SECURITY.md").read_text(encoding="utf-8")
        install = (ROOT / "docs" / "INSTALL.md").read_text(encoding="utf-8")
        for row in privacy["rows"]:
            self.assertIn(row["item"], readme)
            self.assertIn(row["item"], security)
        self.assertEqual(set(privacy["tables"]), {"会上传", "仅本地", "永不采集"})
        self.assertIn("ctr_", privacy["collector_token"]["note"])
        self.assertIn("不能清库", privacy["collector_token"]["note"])
        self.assertIn("python -m collector preview", "\n".join(privacy["dry_run"]["commands"]))
        self.assertIn("python -m collector once --dry-run", "\n".join(privacy["dry_run"]["commands"]))
        formula = self.client.get("/api/v1/pricing/formula").json()
        self.assertEqual(formula["table_version"], "2026.10.28")
        self.assertEqual(formula["updated_at"], "2026-10-28")
        self.assertIn("不是实扣账单", formula["formula_zh"])
        self.assertFalse(formula["include_cache_default"])
        self.assertIn("tokenrank.online", install)
        self.assertIn("127.0.0.1:8000", (ROOT / "docker-compose.yml").read_text(encoding="utf-8"))
        self.assertEqual((ROOT / "VERSION").read_text(encoding="utf-8").strip(), "0.24.0")
        health = self.client.get("/health").json()
        self.assertEqual(health["version"], "0.24.0")
        self.assertIn("server_time", health)
        self.assertEqual(health["pricing_table"]["table_version"], "2026.10.28")

    def test_diagnose_reports_detected_tools(self) -> None:
        seen = {}

        def fetch(path, body, cfg):
            seen["path"] = path
            if body is None:
                return 200, json.dumps({"status": "ok", "server_time": datetime.now(timezone.utc).isoformat()})
            self.assertNotIn("prompt", json.dumps(body))
            return 200, json.dumps(
                {
                    "error_code": "no_local_logs",
                    "message": "无本地日志。这台电脑上没找到可上报的用量。",
                    "next_step": "没装的工具不用管。",
                    "detected": body["detected"],
                    "missing": body["missing"],
                }
            )

        cfg = CollectorConfig(rankings_url="http://rankings.example", api_key="ctr_test")
        doc = run_diagnose(cfg, fixtures=True, fetch=fetch)
        self.assertEqual(doc["error_code"], "no_local_logs")
        self.assertIn("cursor", doc["detected"])
        self.assertIn("没装", doc["next_step"])
        self.assertIn("下一步", format_diagnose(doc))

        def down(_path, _body, _cfg):
            return 0, ""

        offline = run_diagnose(cfg, fixtures=True, fetch=down)
        self.assertEqual(offline["error_code"], "unreachable")
        self.assertIn("浏览器", offline["next_step"])
        self.assertIn("下一步", format_diagnose(offline))
