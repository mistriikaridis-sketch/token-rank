"""Fixture scan → ingest, privacy allowlist, and scheduler text."""
from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

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
    "CURSOR_DATA_DIR",
    "PROCODE_DATA_DIR",
):
    os.environ.pop(_name, None)

from fastapi.testclient import TestClient  # noqa: E402

from app.adapters.claude_code import map_claude_code_events  # noqa: E402
from app.adapters.codex import map_codex_events  # noqa: E402
from app.adapters.continue_dev import map_continue_events  # noqa: E402
from app.adapters.cursor_admin import map_cursor_events  # noqa: E402
from app.adapters.procode import map_procode_events  # noqa: E402
from app.adapters.workbuddy import map_workbuddy_events  # noqa: E402
from app.db import db, init_db  # noqa: E402
from app.main import app  # noqa: E402
from collector.config import CollectorConfig  # noqa: E402
from collector.payload import build_payloads, iter_keys, redact_home, summarize, ALLOWED_KEYS  # noqa: E402
from collector.service import render  # noqa: E402
from collector.sources import ToolScan, _platform_roots, scan_all  # noqa: E402
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


LOCAL_TOOLS = ("workbuddy", "claude_code", "codex", "continue", "cursor", "procode")


def _remap(tool: str, events: list[dict]) -> list[dict]:
    if tool == "workbuddy":
        return map_workbuddy_events(events)
    if tool == "claude_code":
        return map_claude_code_events(events)
    if tool == "codex":
        return map_codex_events(events)
    if tool == "continue":
        return map_continue_events(events)
    if tool == "cursor":
        return map_cursor_events(events)
    if tool == "procode":
        return map_procode_events(events)
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
        for tool in LOCAL_TOOLS:
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
        for tool in LOCAL_TOOLS:
            self.assertGreater(doc["tools"][tool]["rows"], 0, tool)
        blob = proc.stdout
        self.assertNotIn(str(Path.home()), blob)
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

    def test_cursor_and_procode_split_without_secrets(self) -> None:
        cursor_rows = self.report["cursor"].rows
        cursor_ids = {row["request_id"] for row in cursor_rows}
        self.assertIn("cursor-export-alice-1", cursor_ids)
        self.assertIn("cursor-export-bob-1", cursor_ids)
        self.assertIn("cursor-export-jsonl-1", cursor_ids)
        self.assertNotIn("cursor-export-secret", cursor_ids)
        self.assertNotIn("cursor-export-nomodel", cursor_ids)
        self.assertNotIn("cursor-export-jsonl-secret", cursor_ids)
        self.assertIn(
            "cursor:bubble:aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa:11111111-1111-4111-8111-111111111111",
            cursor_ids,
        )
        self.assertNotIn(
            "cursor:bubble:aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa:11111111-1111-4111-8111-111111111112",
            cursor_ids,
        )
        accounts = {row["account_id"] for row in cursor_rows}
        self.assertTrue({"alice@acme.com", "bob@acme.com", "carol@acme.com"} <= accounts)
        projects = {row["project_id"] for row in cursor_rows}
        self.assertIn("chatbot-core", projects)
        self.assertGreaterEqual(len(projects), 2)
        models = {row["model"] for row in cursor_rows}
        self.assertIn("default", models)
        self.assertIn("企业内部-cursor-模型", models)
        self.assertIn("企业内部-cursor-气泡", models)
        bubble = next(
            row
            for row in cursor_rows
            if str(row["request_id"]).endswith("11111111-1111-4111-8111-111111111111")
        )
        self.assertEqual(bubble["cache_read_tokens"], 1000)
        self.assertEqual(bubble["account_id"], "alice@acme.com")
        alice = next(row for row in cursor_rows if row["request_id"] == "cursor-export-alice-1")
        self.assertEqual(alice["project_id"], "chatbot-core")
        self.assertEqual(alice["cost_basis"], "cursor_charged")

        procode_rows = self.report["procode"].rows
        procode_ids = {row["request_id"] for row in procode_rows}
        self.assertEqual(
            procode_ids,
            {
                "procode-sample-001",
                "procode-sample-002",
                "procode-sample-004",
                "procode-sample-005",
            },
        )
        by_id = {row["request_id"]: row for row in procode_rows}
        self.assertEqual(by_id["procode-sample-001"]["project_id"], "billing-bot")
        self.assertEqual(by_id["procode-sample-001"]["account_id"], "frank@acme.com")
        self.assertEqual(by_id["procode-sample-002"]["account_id"], "grace@acme.com")
        self.assertEqual(by_id["procode-sample-002"]["model"], "企业内部-procode-模型")
        self.assertEqual(by_id["procode-sample-004"]["project_id"], "batch")
        self.assertEqual(by_id["procode-sample-005"]["project_id"], "export-bot")
        self.assertEqual(by_id["procode-sample-005"]["model"], "企业内部-procode-导出")
        self.assertEqual(by_id["procode-sample-005"]["prompt_tokens"], 12)
        self.assertEqual(by_id["procode-sample-005"]["cache_read_tokens"], 2)
        self.assertGreaterEqual(len({row["project_id"] for row in procode_rows}), 2)
        for blocked_id in (
            "procode-discussion-must-skip",
            "procode-blueprint-must-skip",
            "procode-apikey-file-must-skip",
        ):
            self.assertNotIn(blocked_id, procode_ids)
        diag = self.report["procode"].diagnostics
        self.assertEqual(diag["adapter_status"], "placeholder")
        self.assertIn("encrypted_sqlite", diag["blocked"])
        self.assertGreaterEqual(diag["unopened_db_seen"], 1)
        self.assertIn("canoryn.sqlite", diag["unopened_db_basenames"])
        self.assertGreaterEqual(diag["discussions_skipped"], 1)
        self.assertGreaterEqual(diag["blueprints_skipped"], 1)
        self.assertGreaterEqual(diag["files_skipped_credential_name"], 1)

        blob = json.dumps(self.payloads, ensure_ascii=False)
        for snippet in FORBIDDEN_SNIPPETS:
            self.assertNotIn(snippet, blob, snippet)
        self.assertNotIn("cursorAuth", blob)
        self.assertNotIn("accessToken", blob)

    def test_home_layouts_and_canoryn_alias(self) -> None:
        home = Path(tempfile.mkdtemp(prefix="tr-home-"))
        export_dir = home / ".cursor" / "exports"
        export_dir.mkdir(parents=True)
        (export_dir / "usage.json").write_text(
            json.dumps(
                {
                    "usageEvents": [
                        {
                            "model": "gpt-4o-mini",
                            "userEmail": "alice@acme.com",
                            "eventId": "cursor-home-export",
                            "timestamp": "2026-10-26T08:00:00Z",
                            "conversationId": "eeeeeeee-eeee-4eee-8eee-eeeeeeeeeeee",
                            "tokenUsage": {"inputTokens": 6, "outputTokens": 2},
                        }
                    ]
                }
            ),
            encoding="utf-8",
        )
        db_dir = home / ".config" / "Cursor" / "User" / "globalStorage"
        db_dir.mkdir(parents=True)
        import sqlite3

        db_path = db_dir / "state.vscdb"
        conn = sqlite3.connect(db_path)
        conn.execute("CREATE TABLE cursorDiskKV (key TEXT PRIMARY KEY, value TEXT)")
        conn.execute("CREATE TABLE ItemTable (key TEXT PRIMARY KEY, value TEXT)")
        conn.execute(
            "INSERT INTO ItemTable(key, value) VALUES ('cursorAuth/cachedEmail', 'bob@acme.com')"
        )
        conn.execute(
            "INSERT INTO cursorDiskKV(key, value) VALUES (?, ?)",
            (
                "bubbleId:ffffffff-ffff-4fff-8fff-ffffffffffff:33333333-3333-4333-8333-333333333333",
                json.dumps(
                    {
                        "text": "SECRET THIS MUST NEVER /Users/alice",
                        "tokenCount": {"inputTokens": 4, "outputTokens": 1},
                        "modelInfo": {"modelName": "gpt-4o"},
                        "createdAt": "2026-10-26T09:00:00Z",
                    }
                ),
            ),
        )
        conn.commit()
        conn.close()

        (home / ".procode").mkdir()
        (home / ".procode" / "events.json").write_text(
            json.dumps(
                {
                    "model": "gpt-4o-mini",
                    "prompt_tokens": 3,
                    "completion_tokens": 1,
                    "account_id": "frank@acme.com",
                    "request_id": "procode-home-1",
                    "timestamp": "2026-10-26T10:00:00Z",
                    "project": "/Users/bob/work/home-bot",
                }
            ),
            encoding="utf-8",
        )
        canoryn = home / "Library" / "Application Support" / "Canoryn"
        canoryn.mkdir(parents=True)
        (canoryn / "usage.jsonl").write_text(
            json.dumps(
                {
                    "model": "deepseek-chat",
                    "prompt_tokens": 8,
                    "completion_tokens": 2,
                    "account_id": "grace@acme.com",
                    "project_id": "mac-canoryn",
                    "request_id": "procode-home-2",
                    "timestamp": "2026-10-26T10:05:00Z",
                }
            )
            + "\n",
            encoding="utf-8",
        )
        win = home / "AppData" / "Roaming" / "ProCode"
        win.mkdir(parents=True)
        (win / "usage.json").write_text(
            json.dumps(
                {
                    "model": "glm-4-plus",
                    "prompt_tokens": 9,
                    "completion_tokens": 3,
                    "account_id": "frank@acme.com",
                    "project_id": "win-procode",
                    "request_id": "procode-home-3",
                    "timestamp": "2026-10-26T10:10:00Z",
                }
            ),
            encoding="utf-8",
        )

        found = scan_all(CollectorConfig(), tools=["cursor", "procode"], home=home)
        cursor_ids = {row["request_id"] for row in found["cursor"].rows}
        self.assertIn("cursor-home-export", cursor_ids)
        self.assertIn(
            "cursor:bubble:ffffffff-ffff-4fff-8fff-ffffffffffff:33333333-3333-4333-8333-333333333333",
            cursor_ids,
        )
        bubble = next(row for row in found["cursor"].rows if str(row["request_id"]).startswith("cursor:bubble:"))
        self.assertEqual(bubble["account_id"], "bob@acme.com")
        self.assertNotIn("SECRET", json.dumps(build_payloads(found), ensure_ascii=False))
        procode_ids = {row["request_id"]: row for row in found["procode"].rows}
        self.assertEqual(set(procode_ids), {"procode-home-1", "procode-home-2", "procode-home-3"})
        self.assertEqual(procode_ids["procode-home-1"]["project_id"], "home-bot")

        appdata = Path(tempfile.mkdtemp(prefix="tr-appdata-"))
        leaked = appdata / "Cursor" / "exports"
        leaked.mkdir(parents=True)
        (leaked / "usage.json").write_text(
            json.dumps(
                {
                    "model": "gpt-4o",
                    "userEmail": "leak@acme.com",
                    "eventId": "cursor-should-not-leak",
                    "timestamp": "2026-10-26T11:00:00Z",
                    "tokenUsage": {"inputTokens": 1, "outputTokens": 1},
                }
            ),
            encoding="utf-8",
        )
        os.environ["APPDATA"] = str(appdata)
        try:
            isolated = scan_all(CollectorConfig(), tools=["cursor"], home=home)
        finally:
            os.environ.pop("APPDATA", None)
        isolated_ids = {row["request_id"] for row in isolated["cursor"].rows}
        self.assertNotIn("cursor-should-not-leak", isolated_ids)

        proc = subprocess.run(
            [sys.executable, "-m", "collector", "preview", "--fixtures", "--json", "--tool", "canoryn"],
            cwd=ROOT,
            capture_output=True,
            text=True,
            check=False,
        )
        self.assertEqual(proc.returncode, 0, proc.stderr)
        doc = json.loads(proc.stdout)
        self.assertEqual(set(doc["tools"]), {"procode"})
        self.assertGreater(doc["tools"]["procode"]["rows"], 0)

    def test_preview_redacts_home_prefix(self) -> None:
        home = Path.home()
        report = {
            "cursor": ToolScan(
                tool="cursor",
                roots=[str(home / ".cursor" / "exports")],
                missing_roots=[str(home / ".procode")],
                rows=[],
            )
        }
        doc = summarize(report, {"cursor": []})
        blob = json.dumps(doc, ensure_ascii=False)
        self.assertNotIn(str(home), blob)
        self.assertEqual(doc["tools"]["cursor"]["roots"], ["~/.cursor/exports"])
        self.assertEqual(doc["tools"]["cursor"]["missing_roots"], ["~/.procode"])
        self.assertEqual(redact_home(str(home)), "~")

    def test_procode_skips_sqlite_discussions_and_reads_export_aliases(self) -> None:
        home = Path(tempfile.mkdtemp(prefix="tr-procode-"))
        canoryn = home / "Library" / "Application Support" / "canoryn"
        canoryn.mkdir(parents=True)
        sqlite_path = canoryn / "canoryn.sqlite"
        sqlite_path.write_text(
            json.dumps(
                {
                    "model": "gpt-4o",
                    "prompt_tokens": 9,
                    "completion_tokens": 1,
                    "request_id": "procode-sqlite-must-not-open",
                    "account_id": "frank@acme.com",
                }
            ),
            encoding="utf-8",
        )
        (canoryn / "discussions").mkdir()
        (canoryn / "discussions" / "thread.json").write_text(
            json.dumps(
                {
                    "model": "gpt-4o-mini",
                    "prompt_tokens": 4,
                    "completion_tokens": 1,
                    "request_id": "procode-discussion-temp",
                    "content": "hi",
                }
            ),
            encoding="utf-8",
        )
        (canoryn / "blueprints").mkdir()
        (canoryn / "blueprints" / "flow.json").write_text(
            json.dumps(
                {
                    "model": "gpt-4o-mini",
                    "prompt_tokens": 3,
                    "completion_tokens": 1,
                    "request_id": "procode-blueprint-temp",
                }
            ),
            encoding="utf-8",
        )
        export = home / ".config" / "Canoryn" / "exports"
        export.mkdir(parents=True)
        (export / "token-usage.json").write_text(
            json.dumps(
                {
                    "modelName": "企业内部-procode-导出",
                    "inputTokens": 7,
                    "outputTokens": 1,
                    "userEmail": "grace@acme.com",
                    "requestId": "procode-token-usage-1",
                    "createdAt": "2026-10-26T12:00:00Z",
                    "projectPath": "/Users/alice/work/token-bot",
                }
            ),
            encoding="utf-8",
        )
        (export / "api_key.json").write_text(
            json.dumps(
                {
                    "model": "gpt-4o",
                    "prompt_tokens": 2,
                    "completion_tokens": 1,
                    "request_id": "procode-apikey-temp",
                }
            ),
            encoding="utf-8",
        )
        share = home / ".local" / "share" / "ProCode" / "usage"
        share.mkdir(parents=True)
        (share / "events.jsonl").write_text(
            json.dumps(
                {
                    "model": "glm-4-plus",
                    "prompt_tokens": 5,
                    "completion_tokens": 2,
                    "account_id": "frank@acme.com",
                    "project_id": "linux-procode",
                    "request_id": "procode-linux-share-1",
                    "timestamp": "2026-10-26T12:05:00Z",
                }
            )
            + "\n",
            encoding="utf-8",
        )
        win = home / "AppData" / "Local" / "canoryn"
        win.mkdir(parents=True)
        (win / "usage.json").write_text(
            json.dumps(
                {
                    "model": "deepseek-chat",
                    "prompt_tokens": 8,
                    "completion_tokens": 1,
                    "account_id": "grace@acme.com",
                    "project_id": "win-local",
                    "request_id": "procode-win-local-1",
                    "timestamp": "2026-10-26T12:10:00Z",
                }
            ),
            encoding="utf-8",
        )

        real_read = Path.read_text

        def guarded(self: Path, *args: object, **kwargs: object) -> str:
            if self.suffix.lower() in {".sqlite", ".sqlite3", ".db", ".cryn", ".crynpack"}:
                raise AssertionError(f"opened database file {self.name}")
            if self.name.lower() == "api_key.json":
                raise AssertionError("opened credential-named file")
            if "discussions" in {part.lower() for part in self.parts}:
                raise AssertionError("opened discussions file")
            if "blueprints" in {part.lower() for part in self.parts}:
                raise AssertionError("opened blueprints file")
            return real_read(self, *args, **kwargs)

        with patch.object(Path, "read_text", guarded):
            found = scan_all(CollectorConfig(), tools=["procode"], home=home)
        ids = {row["request_id"]: row for row in found["procode"].rows}
        self.assertEqual(
            set(ids),
            {"procode-token-usage-1", "procode-linux-share-1", "procode-win-local-1"},
        )
        self.assertEqual(ids["procode-token-usage-1"]["project_id"], "token-bot")
        self.assertEqual(ids["procode-token-usage-1"]["account_id"], "grace@acme.com")
        self.assertEqual(ids["procode-token-usage-1"]["model"], "企业内部-procode-导出")
        diag = found["procode"].diagnostics
        self.assertEqual(diag["adapter_status"], "placeholder")
        self.assertGreaterEqual(diag["unopened_db_seen"], 1)
        self.assertGreaterEqual(diag["discussions_skipped"], 1)
        self.assertGreaterEqual(diag["blueprints_skipped"], 1)
        self.assertGreaterEqual(diag["files_skipped_credential_name"], 1)
        self.assertIn("不打开 sqlite", diag["blocked_zh"])
        payloads = build_payloads(found)
        blob = json.dumps({"diagnostics": diag, "payloads": payloads}, ensure_ascii=False)
        self.assertNotIn("procode-sqlite-must-not-open", blob)
        self.assertNotIn("/Users/alice", blob)
        self.assertNotIn(str(home), json.dumps(diag, ensure_ascii=False))

        source = (ROOT / "app" / "adapters" / "procode.py").read_text(encoding="utf-8")
        self.assertNotIn("subprocess", source)
        self.assertNotIn("sqlcipher", source.lower())
        self.assertNotIn("keyring", source.lower())

    def test_procode_platform_roots_and_health_stay_placeholder(self) -> None:
        config_home = Path(tempfile.mkdtemp(prefix="tr-xdg-"))
        data_home = config_home / "share"
        appdata = config_home / "roaming"
        local = config_home / "local"
        previous = {
            name: os.environ.get(name)
            for name in ("XDG_CONFIG_HOME", "XDG_DATA_HOME", "APPDATA", "LOCALAPPDATA")
        }
        os.environ["XDG_CONFIG_HOME"] = str(config_home)
        os.environ["XDG_DATA_HOME"] = str(data_home)
        os.environ["APPDATA"] = str(appdata)
        os.environ["LOCALAPPDATA"] = str(local)
        try:
            roots = {str(path) for path in _platform_roots("procode")}
        finally:
            for name, value in previous.items():
                if value is None:
                    os.environ.pop(name, None)
                else:
                    os.environ[name] = value
        self.assertIn(str(config_home / "Canoryn"), roots)
        self.assertIn(str(data_home / "ProCode"), roots)
        self.assertIn(str(appdata / "canoryn"), roots)
        self.assertIn(str(local / "procode"), roots)

        health = self.client.get("/health")
        self.assertEqual(health.status_code, 200, health.text)
        body = health.json()
        self.assertEqual(body["version"], (ROOT / "VERSION").read_text(encoding="utf-8").strip())
        procode = next(item for item in body["adapters"] if item["id"] == "procode")
        self.assertEqual(procode["status"], "placeholder")
        self.assertEqual(procode["collects"], "usage_json")
        self.assertEqual(procode["blocked"], ["encrypted_sqlite", "keychain", "canoryn_cli"])
        for phrase in ("不解密", "不打开数据库", "不读钥匙串", "不启动 canoryn", "placeholder"):
            self.assertIn(phrase, procode["notes"])

    def test_operator_docs_cover_real_machine_acceptance(self) -> None:
        acceptance = (ROOT / "docs" / "ACCEPTANCE.md").read_text(encoding="utf-8")
        collector = (ROOT / "collector" / "INSTALL.md").read_text(encoding="utf-8")
        install = (ROOT / "docs" / "INSTALL.md").read_text(encoding="utf-8")
        for text in (acceptance, collector):
            for phrase in (
                "真机采集验收",
                "workbuddy",
                "claude_code",
                "codex",
                "continue",
                "cursor",
                "canoryn",
                "account_id × project_id",
                "grain=finops",
                "filtered-usage-events",
                "placeholder",
                "不解密",
                "TOKEN_RANKINGS_API_KEY",
            ):
                self.assertIn(phrase, text, phrase)
        self.assertIn("真机采集验收", install)
        self.assertIn("docs/ACCEPTANCE.md", collector)


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
