"""Login page, Google session cookie, and post-login collector schedule."""
from __future__ import annotations

import json
import os
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

os.environ.setdefault("DATA_DIR", tempfile.mkdtemp(prefix="tr-login-"))
os.environ.setdefault("TOKEN_RANKINGS_API_KEY", "test-write-key")
os.environ.setdefault("SEED_ON_START", "0")
os.environ.setdefault("FORUM_SEED_ON_START", "0")
os.environ.pop("DATABASE_URL", None)

from fastapi.testclient import TestClient  # noqa: E402

from app.db import init_db  # noqa: E402
from app.google_auth import (  # noqa: E402
    GoogleIdentity,
    claims_to_identity,
    exchange_and_verify,
    post_login_path,
)
from app.main import app  # noqa: E402

init_db()

GOOGLE_VARS = (
    "GOOGLE_CLIENT_ID",
    "GOOGLE_CLIENT_SECRET",
    "GOOGLE_REDIRECT_URI",
    "GOOGLE_ADMIN_EMAILS",
)


class LoginFlowTests(unittest.TestCase):
    def setUp(self) -> None:
        self._saved = {name: os.environ.get(name) for name in ("USERS_JSON", *GOOGLE_VARS)}
        os.environ["USERS_JSON"] = json.dumps(
            [
                {
                    "name": "已有成员",
                    "role": "member",
                    "account_id": "acme-prod",
                    "email": "linked@example.com",
                    "api_key": "login-member-key",
                }
            ]
        )
        for name in GOOGLE_VARS:
            os.environ.pop(name, None)
        init_db()
        self.client = TestClient(app)

    def tearDown(self) -> None:
        for name, value in self._saved.items():
            if value is None:
                os.environ.pop(name, None)
            else:
                os.environ[name] = value

    def test_login_page_offers_google_and_key(self) -> None:
        page = self.client.get("/login")
        self.assertEqual(page.status_code, 200)
        self.assertIn("使用 Google 登录", page.text)
        self.assertIn("私有部署 / 密钥登录", page.text)
        self.assertIn("每日自动同步用量", page.text)
        self.assertIn("token_rankings_user_api_key", page.text)
        self.assertIn('href="/onboard"', page.text)
        self.assertNotIn("id_token", page.text)

    def test_google_stays_off_without_client(self) -> None:
        cfg = self.client.get("/api/v1/auth/config")
        self.assertEqual(cfg.status_code, 200)
        body = cfg.json()
        self.assertFalse(body["google_enabled"])
        self.assertNotIn("client_secret", cfg.text)
        started = self.client.get("/auth/google/start", follow_redirects=False)
        self.assertEqual(started.status_code, 303)
        self.assertIn("/login", started.headers["location"])
        self.assertIn("google_unconfigured", started.headers["location"])
        self.assertNotIn("accounts.google.com", started.headers["location"])

    def test_install_commands_schedule_existing_timer(self) -> None:
        signed = self.client.get(
            "/api/v1/onboarding",
            headers={"Authorization": "Bearer login-member-key"},
        )
        self.assertEqual(signed.status_code, 200, signed.text)
        body = signed.json()
        self.assertIn("每日自动同步用量", body["schedule_note"])
        self.assertIn("不是另一套同步协议", body["schedule_note"])
        self.assertIn("每日自动同步用量", body["ai_prompt"])
        self.assertIn("只装上报", body["ai_prompt"])
        for key in ("macos", "linux", "windows"):
            text = body["commands"][key]
            self.assertIn("collector service", text, key)
            self.assertIn("每日自动同步用量", text, key)
            self.assertNotIn("定时上报（可选）", text, key)
            self.assertIn("不解密", text, key)
        self.assertIn("--os macos", body["commands"]["macos"])
        self.assertIn("launchctl load", body["commands"]["macos"])
        self.assertIn("--os linux --enable", body["commands"]["linux"])
        self.assertIn("--os windows", body["commands"]["windows"])

    def test_new_account_lands_on_onboard(self) -> None:
        self.assertEqual(post_login_path(None, "/people"), "/onboard")
        self.assertEqual(post_login_path("nobody-yet", "/settings"), "/onboard")
        self.assertEqual(post_login_path("nobody-yet", "https://evil.example"), "/onboard")

    def test_returning_account_keeps_destination(self) -> None:
        written = self.client.post(
            "/api/v1/usage",
            headers={"Authorization": "Bearer " + os.environ["TOKEN_RANKINGS_API_KEY"]},
            json={
                "model": "gpt-4o-mini",
                "prompt_tokens": 10,
                "completion_tokens": 1,
                "total_tokens": 11,
                "account_id": "acme-prod",
                "participant": "已有成员",
                "tool": "cursor",
                "source": "login-test",
                "request_id": "login-usage-1",
            },
        )
        self.assertEqual(written.status_code, 200, written.text)
        self.assertEqual(post_login_path("acme-prod", "/settings"), "/settings")
        self.assertEqual(post_login_path("acme-prod", "https://evil.example"), "/me")

    def test_google_session_is_httponly_and_lands_on_onboard(self) -> None:
        os.environ["GOOGLE_CLIENT_ID"] = "test-google-client"
        os.environ["GOOGLE_CLIENT_SECRET"] = "test-google-secret"
        os.environ["GOOGLE_REDIRECT_URI"] = "http://127.0.0.1:8000/auth/google/callback"
        os.environ["GOOGLE_ADMIN_EMAILS"] = "boss@example.com"
        started = self.client.get("/auth/google/start", follow_redirects=False)
        self.assertEqual(started.status_code, 303)
        location = started.headers["location"]
        self.assertTrue(location.startswith("https://accounts.google.com/o/oauth2/v2/auth?"))
        self.assertNotIn("test-google-secret", location)
        self.assertIn("test-google-client", location)
        blob = "\n".join(started.headers.get_list("set-cookie"))
        self.assertIn("HttpOnly", blob)
        self.assertIn("tr_oauth_state", blob)
        state = self.client.cookies.get("tr_oauth_state")

        def fake_exchange(code: str, verifier: str) -> GoogleIdentity:
            self.assertEqual(code, "good-code")
            self.assertTrue(verifier)
            return GoogleIdentity(
                sub="google-sub-1",
                email="new-person@example.com",
                name="新人",
                email_verified=True,
            )

        import app.google_auth as google_auth

        original = google_auth.exchange_and_verify
        google_auth.exchange_and_verify = fake_exchange
        try:
            back = self.client.get(
                "/auth/google/callback",
                params={"code": "good-code", "state": state},
                follow_redirects=False,
            )
        finally:
            google_auth.exchange_and_verify = original
        self.assertEqual(back.status_code, 303, back.text)
        self.assertEqual(back.headers["location"], "/onboard")
        session_cookie = "\n".join(back.headers.get_list("set-cookie"))
        self.assertIn("tr_session=", session_cookie)
        self.assertIn("HttpOnly", session_cookie)
        self.assertNotIn("good-code", session_cookie)
        self.assertNotIn("id_token", session_cookie)
        who = self.client.get("/api/v1/whoami")
        self.assertTrue(who.json()["authenticated"])
        self.assertEqual(who.json()["role"], "member")
        self.assertEqual(who.json()["kind"], "google")
        self.assertEqual(who.json()["account_id"], "new-person@example.com")
        denied = self.client.post("/api/v1/admin/clear?scope=seed")
        self.assertEqual(denied.status_code, 401)
        self.client.post("/auth/logout")
        after = self.client.get("/api/v1/whoami")
        self.assertFalse(after.json()["authenticated"])

    def test_allowlist_and_roster_link(self) -> None:
        os.environ["GOOGLE_CLIENT_ID"] = "test-google-client"
        os.environ["GOOGLE_CLIENT_SECRET"] = "test-google-secret"
        os.environ["GOOGLE_REDIRECT_URI"] = "https://tokenrank.online/auth/google/callback"
        os.environ["GOOGLE_ADMIN_EMAILS"] = "Boss@Example.com"
        started = self.client.get(
            "/auth/google/start",
            params={"next": "/people"},
            follow_redirects=False,
        )
        state = self.client.cookies.get("tr_oauth_state")

        def fake_exchange(code: str, verifier: str) -> GoogleIdentity:
            return GoogleIdentity(
                sub="google-sub-boss",
                email="boss@example.com",
                name="负责人",
                email_verified=True,
            )

        import app.google_auth as google_auth

        original = google_auth.exchange_and_verify
        google_auth.exchange_and_verify = fake_exchange
        try:
            back = self.client.get(
                "/auth/google/callback",
                params={"code": "c", "state": state},
                follow_redirects=False,
            )
        finally:
            google_auth.exchange_and_verify = original
        self.assertEqual(back.headers["location"], "/onboard")
        who = self.client.get("/api/v1/whoami")
        self.assertEqual(who.json()["role"], "admin")
        keyed = self.client.get(
            "/api/v1/whoami",
            headers={"Authorization": "Bearer login-member-key"},
        )
        self.assertEqual(keyed.json()["role"], "member")
        self.assertEqual(keyed.json()["account_id"], "acme-prod")

    def test_claims_reject_unverified_email(self) -> None:
        with self.assertRaises(Exception) as caught:
            claims_to_identity(
                {
                    "aud": "test-google-client",
                    "iss": "https://accounts.google.com",
                    "sub": "abc",
                    "email": "a@example.com",
                    "email_verified": "false",
                },
                "test-google-client",
            )
        self.assertEqual(getattr(caught.exception, "code", ""), "google_email")

    def test_exchange_does_not_keep_the_id_token(self) -> None:
        os.environ["GOOGLE_CLIENT_ID"] = "test-google-client"
        os.environ["GOOGLE_CLIENT_SECRET"] = "test-google-secret"
        os.environ["GOOGLE_REDIRECT_URI"] = "https://tokenrank.online/auth/google/callback"
        seen = {}

        class Resp:
            def __init__(self, status: int, payload: dict) -> None:
                self.status_code = status
                self._payload = payload

            def json(self) -> dict:
                return self._payload

        def fake_post(url, data=None, timeout=None):
            seen["url"] = url
            seen["secret"] = (data or {}).get("client_secret")
            return Resp(200, {"id_token": "header.payload.sig"})

        def fake_get(url, params=None, timeout=None):
            seen["info"] = url
            return Resp(
                200,
                {
                    "aud": "test-google-client",
                    "iss": "https://accounts.google.com",
                    "sub": "sub-9",
                    "email": "a@example.com",
                    "email_verified": "true",
                    "name": "A",
                },
            )

        import httpx

        original_post, original_get = httpx.post, httpx.get
        httpx.post, httpx.get = fake_post, fake_get
        try:
            identity = exchange_and_verify("code", "verifier")
        finally:
            httpx.post, httpx.get = original_post, original_get
        self.assertEqual(identity.email, "a@example.com")
        self.assertFalse(hasattr(identity, "id_token"))
        self.assertIn("oauth2.googleapis.com/token", seen["url"])
        self.assertEqual(seen["secret"], "test-google-secret")
        self.assertIn("tokeninfo", seen["info"])

    def test_html_401_redirects_to_login(self) -> None:
        page = self.client.get(
            "/api/v1/export/usage.csv",
            headers={"Accept": "text/html"},
            follow_redirects=False,
        )
        self.assertEqual(page.status_code, 303)
        self.assertIn("/login", page.headers["location"])
        api = self.client.get("/api/v1/export/usage.csv", follow_redirects=False)
        self.assertEqual(api.status_code, 401)
        self.assertIn("登录", api.json()["detail"]["message"])

    def test_wrong_key_is_chinese(self) -> None:
        denied = self.client.get(
            "/api/v1/whoami",
            headers={"Authorization": "Bearer not-a-user-key"},
        )
        self.assertEqual(denied.status_code, 401)
        self.assertIn("密钥不正确", denied.json()["detail"]["message"])


if __name__ == "__main__":
    unittest.main()
