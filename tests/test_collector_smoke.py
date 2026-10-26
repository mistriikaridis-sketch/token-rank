"""Fixture scan → ingest, privacy allowlist, and scheduler text."""
from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

os.environ["DATA_DIR"] = tempfile.mkdtemp(prefix="tr-collector-")
os.environ["TOKEN_RANKINGS_API_KEY"] = "test-write-key"
os.environ["SEED_ON_START"] = "0"
os.environ["FORUM_SEED_ON_START"] = "0"
os.environ.pop("DATABASE_URL", None)
for _name in (
    "WORKBUDDY_PROJECTS_DIR",
    "CLAUDE_PROJECTS_DIR",
    "CODEX_SESSIONS_DIR",
    "CONTINUE_DATA_DIR",
):
    os.environ.pop(_name, None)

from fastapi.testclient import TestClient  # noqa: E402

from app.adapters.claude_code import map_claude_code_events  # noqa: E402
from app.adapters.codex import map_codex_events  # noqa: E402
from app.adapters.continue_dev import map_continue_events  # noqa: E402
from app.adapters.workbuddy import map_workbuddy_events  # noqa: E402
from app.db import db, init_db  # noqa: E402
from app.main import app  # noqa: E402
from collector.config import CollectorConfig  # noqa: E402
from collector.payload import build_payloads, iter_keys, ALLOWED_KEYS  # noqa: E402
from collector.service import render  # noqa: E402
from collector.sources import scan_all  # noqa: E402
from collector.upload import INGEST_PATH, upload_payloads  # noqa: E402

init_db()

FORBIDDEN_SNIPPETS = (
    "SECRET",
    "THIS MUST NEVER",
    "NEVER STORE",
    "/Users/alice",
    "/Users/bob",
    "echo SECRET",
    "userAgent",
    "filepath",
)


def _remap(tool: str, events: list[dict]) -> list[dict]:
    if tool == "workbuddy":
        return map_workbuddy_events(events)
    if tool == "claude_code":
        return map_claude_code_events(events)
    if tool == "codex":
        return map_codex_events(events)
    if tool == "continue":
        return map_continue_events(events)
    raise AssertionError(tool)


class CollectorFixtureTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.client = TestClient(app)
        cls.cfg = CollectorConfig(
            rankings_url="http://testserver",
            api_key="test-write-key",
        )
        cls.report = scan_all(cls.cfg, fixtures=True)
        cls.payloads = build_payloads(cls.report)

    def test_each_tool_has_rows(self) -> None:
        for tool in ("workbuddy", "claude_code", "codex", "continue"):
            self.assertGreater(len(self.report[tool].rows), 0, tool)
            self.assertGreater(len(self.payloads[tool]), 0, tool)

    def test_payload_has_no_chat_or_paths(self) -> None:
        blob = json.dumps(self.payloads, ensure_ascii=False)
        for snippet in FORBIDDEN_SNIPPETS:
            self.assertNotIn(snippet, blob, snippet)
        leaked = sorted(
            {
                key
                for events in self.payloads.values()
                for key in iter_keys(events)
                if key not in ALLOWED_KEYS
            }
        )
        self.assertEqual(leaked, [])

    def test_round_trip_preserves_tokens_and_request_ids(self) -> None:
        for tool, events in self.payloads.items():
            mapped = _remap(tool, events)
            self.assertEqual(len(mapped), len(events), tool)
            self.assertEqual(
                {row["request_id"] for row in mapped},
                {row["request_id"] for row in self.report[tool].rows},
                tool,
            )
            self.assertEqual(
                sum(int(row["prompt_tokens"]) for row in mapped),
                sum(int(row["prompt_tokens"]) for row in self.report[tool].rows),
                tool,
            )
            self.assertEqual(
                sum(int(row["completion_tokens"]) for row in mapped),
                sum(int(row["completion_tokens"]) for row in self.report[tool].rows),
                tool,
            )

    def test_ingest_is_idempotent(self) -> None:
        def transport(path: str, body: dict, cfg: CollectorConfig) -> dict:
            response = self.client.post(
                path,
                json=body,
                headers={"Authorization": f"Bearer {cfg.api_key}"},
            )
            if response.status_code >= 400:
                raise AssertionError(f"{path} {response.status_code} {response.text}")
            return response.json()

        first = upload_payloads(self.payloads, self.cfg, transport=transport)
        second = upload_payloads(self.payloads, self.cfg, transport=transport)
        for tool in self.payloads:
            self.assertGreater(first[tool]["accepted"], 0, tool)
            self.assertEqual(second[tool]["accepted"], 0, tool)
            self.assertGreater(second[tool]["skipped"], 0, tool)
            self.assertEqual(path_for(tool), INGEST_PATH[tool])

        with db() as conn:
            row = conn.execute(
                """
                SELECT model FROM usage_events
                WHERE source = 'workbuddy' AND request_id LIKE 'workbuddy:%'
                LIMIT 1
                """
            ).fetchone()
        # Fixture models are already canonical; unknown names are covered below.
        self.assertIsNotNone(row)

    def test_identity_override_and_workbody_alias(self) -> None:
        cfg = CollectorConfig(account_id="acme-prod", participant="爱丽丝")
        report = scan_all(cfg, fixtures=True, tools=["workbuddy"])
        self.assertTrue(all(row["account_id"] == "acme-prod" for row in report["workbuddy"].rows))
        self.assertTrue(all(row["participant"] == "爱丽丝" for row in report["workbuddy"].rows))

        home = Path(tempfile.mkdtemp(prefix="tr-home-"))
        projects = home / ".workbody" / "projects" / "demo"
        projects.mkdir(parents=True)
        (projects / "session.jsonl").write_text(
            "\n".join(
                [
                    json.dumps(
                        {
                            "type": "message",
                            "content": "THIS MUST NEVER BE STORED in the upload",
                            "model": "moonshot-v1-8k",
                        },
                        ensure_ascii=False,
                    ),
                    json.dumps(
                        {
                            "type": "llm.usage",
                            "model": "moonshot-v1-8k",
                            "prompt_tokens": 10,
                            "completion_tokens": 4,
                            "timestamp": "2026-10-25T03:00:00Z",
                            "request_id": "wb-alias-1",
                        },
                        ensure_ascii=False,
                    ),
                ]
            )
            + "\n",
            encoding="utf-8",
        )
        found = scan_all(CollectorConfig(), tools=["workbuddy"], home=home)
        self.assertEqual(len(found["workbuddy"].rows), 1)
        self.assertEqual(found["workbuddy"].rows[0]["model"], "moonshot-v1-8k")
        self.assertNotIn("THIS MUST NEVER", json.dumps(build_payloads(found), ensure_ascii=False))

    def test_cli_preview_fixtures(self) -> None:
        proc = subprocess.run(
            [sys.executable, "-m", "collector", "preview", "--fixtures", "--json"],
            cwd=ROOT,
            capture_output=True,
            text=True,
            check=False,
        )
        self.assertEqual(proc.returncode, 0, proc.stderr)
        doc = json.loads(proc.stdout)
        self.assertGreater(doc["totals"]["rows"], 0)
        for tool in ("workbuddy", "claude_code", "codex", "continue"):
            self.assertGreater(doc["tools"][tool]["rows"], 0, tool)
        blob = proc.stdout
        for snippet in FORBIDDEN_SNIPPETS:
            self.assertNotIn(snippet, blob)
        self.assertIn("列表价", doc["totals"]["cost_note_zh"])
        self.assertFalse(doc["privacy"]["uploads_chat"])

    def test_service_snippets(self) -> None:
        cron = render("cron", interval_minutes=30)
        self.assertIn("*/30 * * * *", cron)
        self.assertIn("collector once", cron)
        linux = render("linux", interval_minutes=30)
        self.assertIn("OnUnitActiveSec=30min", linux)
        self.assertIn("collector once", linux)
        macos = render("macos", interval_minutes=30)
        self.assertIn("<key>StartInterval</key>", macos)
        self.assertIn("<integer>1800</integer>", macos)
        windows = render("windows", interval_minutes=30)
        self.assertIn("schtasks", windows)
        self.assertIn("/SC MINUTE /MO 30", windows)

        proc = subprocess.run(
            [sys.executable, "-m", "collector", "service", "--os", "cron", "--print", "--interval", "30"],
            cwd=ROOT,
            capture_output=True,
            text=True,
            check=False,
        )
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("*/30 * * * *", proc.stdout)
        self.assertNotIn(str(Path.home() / ".config" / "systemd"), proc.stdout)


def path_for(tool: str) -> str:
    return INGEST_PATH[tool]


class IngestUnknownModelTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.client = TestClient(app)

    def test_usage_endpoint_keeps_unknown_and_canonicalizes_known(self) -> None:
        headers = {"Authorization": "Bearer test-write-key"}
        unknown = self.client.post(
            "/api/v1/usage",
            headers=headers,
            json={
                "model": "企业内部-不存在的模型",
                "prompt_tokens": 100,
                "completion_tokens": 20,
                "request_id": "unknown-model-1",
                "source": "api",
                "account_id": "acme-prod",
            },
        )
        self.assertEqual(unknown.status_code, 200, unknown.text)
        known = self.client.post(
            "/api/v1/usage",
            headers=headers,
            json={
                "model": "ZhipuAI/GLM-4-Plus",
                "prompt_tokens": 100,
                "completion_tokens": 20,
                "request_id": "glm-alias-1",
                "source": "api",
                "account_id": "acme-prod",
            },
        )
        self.assertEqual(known.status_code, 200, known.text)
        with db() as conn:
            rows = conn.execute(
                """
                SELECT request_id, model FROM usage_events
                WHERE request_id IN ('unknown-model-1', 'glm-alias-1')
                """
            ).fetchall()
        found = {row["request_id"]: row["model"] for row in rows}
        self.assertEqual(found["unknown-model-1"], "企业内部-不存在的模型")
        self.assertEqual(found["glm-alias-1"], "glm-4-plus")
        self.assertGreater(known.json()["estimated_cost_usd"], unknown.json()["estimated_cost_usd"])


if __name__ == "__main__":
    unittest.main()
