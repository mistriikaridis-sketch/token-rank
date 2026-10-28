"""Google sign-in for the public site.

Browser sessions use an httpOnly cookie. Google access tokens and ID tokens
are checked once and then discarded. They are not written to the database
or to the browser's localStorage.

Self-hosted installs leave GOOGLE_CLIENT_ID / GOOGLE_CLIENT_SECRET /
GOOGLE_REDIRECT_URI unset and keep using user API keys.
"""
from __future__ import annotations

import hashlib
import os
import secrets
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Optional
from urllib.parse import urlencode, urlsplit

import httpx
from fastapi import APIRouter, Query, Request
from fastapi.responses import JSONResponse, RedirectResponse, Response

from app.auth import Principal, is_placeholder_secret, load_user_directory
from app.db import db

router = APIRouter()

SESSION_COOKIE = "tr_session"
OAUTH_STATE_COOKIE = "tr_oauth_state"
OAUTH_VERIFIER_COOKIE = "tr_oauth_verifier"
OAUTH_NEXT_COOKIE = "tr_oauth_next"
SESSION_DAYS = 14
OAUTH_COOKIE_SECONDS = 600

GOOGLE_AUTH_URL = "https://accounts.google.com/o/oauth2/v2/auth"
GOOGLE_TOKEN_URL = "https://oauth2.googleapis.com/token"
GOOGLE_TOKENINFO_URL = "https://oauth2.googleapis.com/tokeninfo"
ALLOWED_ISSUERS = {"accounts.google.com", "https://accounts.google.com"}
_NEXT_PATHS = {
    "/",
    "/people",
    "/orgs",
    "/projects",
    "/agents",
    "/me",
    "/onboard",
    "/privacy",
    "/settings",
    "/alerts",
    "/forum",
    "/procode",
}


class GoogleAuthError(Exception):
    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


@dataclass(frozen=True)
class GoogleIdentity:
    sub: str
    email: str
    name: str
    email_verified: bool


def _clean(value: Optional[str]) -> str:
    return (value or "").strip()


def google_config() -> dict[str, str]:
    client_id = _clean(os.environ.get("GOOGLE_CLIENT_ID"))
    client_secret = _clean(os.environ.get("GOOGLE_CLIENT_SECRET"))
    redirect_uri = _clean(os.environ.get("GOOGLE_REDIRECT_URI"))
    if is_placeholder_secret(client_id) or is_placeholder_secret(client_secret):
        client_id = ""
        client_secret = ""
    lowered = {client_id.lower(), client_secret.lower()}
    if lowered & {"your-client-id", "your-client-secret", "paste-client-id", "paste-client-secret"}:
        client_id = ""
        client_secret = ""
    return {
        "client_id": client_id,
        "client_secret": client_secret,
        "redirect_uri": redirect_uri,
    }


def google_enabled() -> bool:
    cfg = google_config()
    redirect = cfg["redirect_uri"]
    if not cfg["client_id"] or not cfg["client_secret"] or not redirect:
        return False
    return redirect.startswith("https://") or redirect.startswith("http://")


def admin_emails() -> set[str]:
    raw = os.environ.get("GOOGLE_ADMIN_EMAILS") or ""
    found: set[str] = set()
    for part in raw.replace(";", ",").split(","):
        email = part.strip().lower()
        if email and "@" in email and " " not in email:
            found.add(email)
    return found


def safe_next_path(raw: Optional[str]) -> str:
    """Allow only in-app paths. Anything else becomes empty."""
    if not raw:
        return ""
    text = str(raw).strip()
    if not text.startswith("/") or text.startswith("//") or text.startswith("/\\"):
        return ""
    if "\\" in text or "\n" in text or "\r" in text or "://" in text:
        return ""
    parts = urlsplit(text)
    if parts.scheme or parts.netloc or parts.fragment:
        return ""
    path = parts.path.rstrip("/") or "/"
    if path not in _NEXT_PATHS:
        return ""
    return path + (f"?{parts.query}" if parts.query else "")


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _iso(value: datetime) -> str:
    return value.isoformat()


def _cookie_secure(request: Request) -> bool:
    proto = (request.headers.get("x-forwarded-proto") or request.url.scheme or "")
    proto = proto.split(",")[0].strip().lower()
    if proto == "https":
        return True
    flag = (os.environ.get("COOKIE_SECURE") or "").strip().lower()
    return flag in {"1", "true", "yes", "on"}


def _set_cookie(
    response: Response,
    request: Request,
    key: str,
    value: str,
    max_age: int,
) -> None:
    response.set_cookie(
        key,
        value,
        max_age=max_age,
        httponly=True,
        secure=_cookie_secure(request),
        samesite="lax",
        path="/",
    )


def _clear_cookie(response: Response, request: Request, key: str) -> None:
    response.delete_cookie(
        key,
        path="/",
        httponly=True,
        secure=_cookie_secure(request),
        samesite="lax",
    )


def _clear_oauth_cookies(response: Response, request: Request) -> None:
    for key in (OAUTH_STATE_COOKIE, OAUTH_VERIFIER_COOKIE, OAUTH_NEXT_COOKIE):
        _clear_cookie(response, request, key)


def _pkce_challenge(verifier: str) -> str:
    digest = hashlib.sha256(verifier.encode("ascii")).digest()
    import base64

    return base64.urlsafe_b64encode(digest).decode("ascii").rstrip("=")


def _same(left: str, right: str) -> bool:
    if len(left) != len(right):
        return False
    return secrets.compare_digest(left, right)


def claims_to_identity(claims: dict[str, Any], client_id: str) -> GoogleIdentity:
    aud = str(claims.get("aud") or "")
    iss = str(claims.get("iss") or "")
    sub = str(claims.get("sub") or "").strip()
    email = str(claims.get("email") or "").strip().lower()
    verified = claims.get("email_verified")
    verified_ok = verified is True or str(verified).strip().lower() == "true"
    if aud != client_id or iss not in ALLOWED_ISSUERS:
        raise GoogleAuthError("google_token")
    if not sub or not email or "@" not in email or " " in email or not verified_ok:
        raise GoogleAuthError("google_email")
    name = str(claims.get("name") or "").strip().replace("\n", " ").replace("\r", " ")
    if not name:
        name = email.split("@", 1)[0]
    return GoogleIdentity(sub=sub[:128], email=email[:254], name=name[:128], email_verified=True)


def exchange_and_verify(code: str, verifier: str) -> GoogleIdentity:
    """Exchange the auth code, check the ID token with Google, then drop both tokens."""
    cfg = google_config()
    if not google_enabled():
        raise GoogleAuthError("google_unconfigured")
    if not code or not verifier or len(code) > 2048 or len(verifier) > 256:
        raise GoogleAuthError("google_token")
    try:
        token_resp = httpx.post(
            GOOGLE_TOKEN_URL,
            data={
                "code": code,
                "client_id": cfg["client_id"],
                "client_secret": cfg["client_secret"],
                "redirect_uri": cfg["redirect_uri"],
                "grant_type": "authorization_code",
                "code_verifier": verifier,
            },
            timeout=15.0,
        )
    except httpx.HTTPError as exc:
        raise GoogleAuthError("google_token") from exc
    if token_resp.status_code != 200:
        raise GoogleAuthError("google_token")
    try:
        token_body = token_resp.json()
    except ValueError as exc:
        raise GoogleAuthError("google_token") from exc
    id_token = str(token_body.get("id_token") or "")
    if not id_token:
        raise GoogleAuthError("google_token")
    try:
        info_resp = httpx.get(
            GOOGLE_TOKENINFO_URL,
            params={"id_token": id_token},
            timeout=15.0,
        )
    except httpx.HTTPError as exc:
        raise GoogleAuthError("google_token") from exc
    if info_resp.status_code != 200:
        raise GoogleAuthError("google_token")
    try:
        claims = info_resp.json()
    except ValueError as exc:
        raise GoogleAuthError("google_token") from exc
    if not isinstance(claims, dict):
        raise GoogleAuthError("google_token")
    return claims_to_identity(claims, cfg["client_id"])


def _roster_for_email(email: str) -> Optional[dict[str, Any]]:
    target = email.strip().lower()
    try:
        directory = load_user_directory()
    except Exception:
        return None
    for user in directory:
        listed = str(user.get("email") or "").strip().lower()
        account = str(user.get("account_id") or "").strip().lower()
        if listed and listed == target:
            return user
        if account == target:
            return user
    return None


def _role_and_account(email: str, google_name: str) -> tuple[str, Optional[str], str]:
    roster = _roster_for_email(email)
    role = "member"
    if roster and roster.get("role") == "admin":
        role = "admin"
    if email in admin_emails():
        role = "admin"
    if roster:
        account_id = roster.get("account_id")
        name = str(roster.get("name") or google_name or email)
    else:
        account_id = email
        name = google_name or email.split("@", 1)[0]
    if role == "member" and not account_id:
        account_id = email
    return role, account_id, name[:128]


def upsert_google_user(identity: GoogleIdentity) -> Principal:
    """Create or link a Google identity. Allowlisted emails are admins."""
    role, account_id, name = _role_and_account(identity.email, identity.name)
    now = _iso(_now())
    with db() as conn:
        by_sub = conn.execute(
            "SELECT google_sub FROM google_users WHERE google_sub = ?",
            (identity.sub,),
        ).fetchone()
        by_email = conn.execute(
            "SELECT google_sub FROM google_users WHERE email = ?",
            (identity.email,),
        ).fetchone()
        keep_sub = identity.sub
        if by_email and (not by_sub or by_email["google_sub"] != identity.sub):
            old_sub = str(by_email["google_sub"])
            if by_sub and old_sub != identity.sub:
                conn.execute("DELETE FROM google_users WHERE google_sub = ?", (identity.sub,))
            conn.execute(
                """
                UPDATE google_users
                SET google_sub = ?, email = ?, name = ?, role = ?, account_id = ?, updated_at = ?
                WHERE google_sub = ?
                """,
                (identity.sub, identity.email, name, role, account_id, now, old_sub),
            )
            conn.execute(
                "UPDATE browser_sessions SET google_sub = ? WHERE google_sub = ?",
                (identity.sub, old_sub),
            )
        elif by_sub:
            conn.execute(
                """
                UPDATE google_users
                SET email = ?, name = ?, role = ?, account_id = ?, updated_at = ?
                WHERE google_sub = ?
                """,
                (identity.email, name, role, account_id, now, identity.sub),
            )
        else:
            conn.execute(
                """
                INSERT INTO google_users (
                    google_sub, email, name, role, account_id, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (identity.sub, identity.email, name, role, account_id, now, now),
            )
    return Principal(
        role=role,  # type: ignore[arg-type]
        account_id=account_id,
        name=name,
        key_hint="Google",
        kind="google",
    )


def create_session(google_sub: str) -> str:
    session_id = secrets.token_urlsafe(32)
    now = _now()
    with db() as conn:
        conn.execute(
            """
            INSERT INTO browser_sessions (id, google_sub, created_at, expires_at)
            VALUES (?, ?, ?, ?)
            """,
            (session_id, google_sub, _iso(now), _iso(now + timedelta(days=SESSION_DAYS))),
        )
    return session_id


def _expired(expires_at: str) -> bool:
    try:
        exp = datetime.fromisoformat(expires_at)
    except ValueError:
        return True
    if exp.tzinfo is None:
        exp = exp.replace(tzinfo=timezone.utc)
    return exp <= _now()


def principal_for_session(session_id: str) -> Optional[Principal]:
    if not session_id or len(session_id) > 256:
        return None
    with db() as conn:
        row = conn.execute(
            """
            SELECT s.expires_at, u.google_sub, u.email, u.name, u.role, u.account_id
            FROM browser_sessions s
            JOIN google_users u ON u.google_sub = s.google_sub
            WHERE s.id = ?
            """,
            (session_id,),
        ).fetchone()
        if row is None or _expired(str(row["expires_at"])):
            conn.execute("DELETE FROM browser_sessions WHERE id = ?", (session_id,))
            return None
    role = "admin" if str(row["role"]) == "admin" else "member"
    account_id = row["account_id"]
    if account_id is not None:
        account_id = str(account_id) or None
    return Principal(
        role=role,  # type: ignore[arg-type]
        account_id=account_id,
        name=str(row["name"] or row["email"] or "Google"),
        key_hint="Google",
        kind="google",
    )


def delete_session(session_id: Optional[str]) -> None:
    if not session_id:
        return
    with db() as conn:
        conn.execute("DELETE FROM browser_sessions WHERE id = ?", (session_id,))


def _no_store(response: Response) -> Response:
    response.headers["Cache-Control"] = "no-store"
    return response


def account_has_usage(account_id: Optional[str]) -> bool:
    """True when this account already has non-demo usage. Seed rows do not count."""
    if not account_id:
        return False
    with db() as conn:
        row = conn.execute(
            """
            SELECT COUNT(*) AS n FROM usage_events
            WHERE account_id = ? AND COALESCE(source, '') != 'seed'
            """,
            (account_id,),
        ).fetchone()
    return bool(row and int(row["n"] or 0) > 0)


def post_login_path(account_id: Optional[str], requested: Optional[str]) -> str:
    """New accounts go install the collector. Returning accounts keep their destination."""
    if not account_has_usage(account_id):
        return "/onboard"
    return safe_next_path(requested) or "/me"


def _login_error(code: str, nxt: str) -> str:
    from urllib.parse import quote

    safe = safe_next_path(nxt) or "/onboard"
    return "/login?error=" + code + "&next=" + quote(safe, safe="")


@router.get("/api/v1/auth/config")
def auth_config() -> dict[str, Any]:
    """Public login switches. Never returns the client id or secret."""
    return {
        "google_enabled": google_enabled(),
        "login_path": "/login",
        "google_start_path": "/auth/google/start",
        "key_login": True,
    }


@router.get("/auth/google/start")
def google_start(
    request: Request,
    next_path: str = Query("", alias="next"),
) -> Response:
    if not google_enabled():
        return _no_store(RedirectResponse("/login?error=google_unconfigured", status_code=303))
    cfg = google_config()
    state = secrets.token_urlsafe(24)
    verifier = secrets.token_urlsafe(48)
    dest = safe_next_path(next_path)
    query = urlencode(
        {
            "client_id": cfg["client_id"],
            "redirect_uri": cfg["redirect_uri"],
            "response_type": "code",
            "scope": "openid email profile",
            "state": state,
            "code_challenge": _pkce_challenge(verifier),
            "code_challenge_method": "S256",
            "prompt": "select_account",
        }
    )
    response = RedirectResponse(GOOGLE_AUTH_URL + "?" + query, status_code=303)
    _set_cookie(response, request, OAUTH_STATE_COOKIE, state, OAUTH_COOKIE_SECONDS)
    _set_cookie(response, request, OAUTH_VERIFIER_COOKIE, verifier, OAUTH_COOKIE_SECONDS)
    if dest:
        _set_cookie(response, request, OAUTH_NEXT_COOKIE, dest, OAUTH_COOKIE_SECONDS)
    return _no_store(response)


@router.get("/auth/google/callback")
def google_callback(
    request: Request,
    code: str = "",
    state: str = "",
    error: str = "",
) -> Response:
    nxt = request.cookies.get(OAUTH_NEXT_COOKIE) or ""

    def fail(reason: str) -> Response:
        response = RedirectResponse(_login_error(reason, nxt), status_code=303)
        _clear_oauth_cookies(response, request)
        return _no_store(response)

    if error or not google_enabled():
        return fail("google_denied" if error else "google_unconfigured")
    expected = request.cookies.get(OAUTH_STATE_COOKIE) or ""
    verifier = request.cookies.get(OAUTH_VERIFIER_COOKIE) or ""
    if not expected or not state or not _same(expected, state):
        return fail("google_state")
    try:
        identity = exchange_and_verify(code, verifier)
        principal = upsert_google_user(identity)
        session_id = create_session(identity.sub)
    except GoogleAuthError as exc:
        return fail(exc.code)
    dest = post_login_path(principal.account_id, nxt)
    response = RedirectResponse(dest, status_code=303)
    _set_cookie(response, request, SESSION_COOKIE, session_id, SESSION_DAYS * 24 * 3600)
    _clear_oauth_cookies(response, request)
    return _no_store(response)


def _end_session(request: Request, response: Response) -> Response:
    delete_session(request.cookies.get(SESSION_COOKIE))
    _clear_cookie(response, request, SESSION_COOKIE)
    _clear_oauth_cookies(response, request)
    return _no_store(response)


@router.post("/auth/logout")
def logout_post(request: Request) -> Response:
    response = JSONResponse({"ok": True})
    return _end_session(request, response)


@router.get("/auth/logout")
def logout_get(request: Request) -> Response:
    response = RedirectResponse("/login", status_code=303)
    return _end_session(request, response)
