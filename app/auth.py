"""Auth: write API key + light RBAC (admin / member).

Write path (ingest/sync/scan/admin/seed):
  TOKEN_RANKINGS_API_KEY via Authorization: Bearer or X-API-Key

Read / export path (optional or required):
  Same headers; resolve against:
    1) TOKEN_RANKINGS_API_KEY → service admin
    2) USERS_JSON / USERS_FILE → user principals (admin|member + account_id)

Env:
  TOKEN_RANKINGS_API_KEY   ingest / admin write key
  USERS_JSON               inline JSON array of user records
  USERS_FILE               path to JSON file (e.g. fixtures/users.json)
  RANKINGS_REQUIRE_AUTH    if 1/true, ranking GETs require a valid principal

User records may carry plaintext ``api_key`` (demo roster) or ``api_key_sha256``
(hex SHA-256 of the user key). Production bootstrap writes only the hash.
"""
from __future__ import annotations

import hashlib
import json
import os
import secrets
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal, Optional

from fastapi import Cookie, Header, HTTPException

ENV_KEY = "TOKEN_RANKINGS_API_KEY"
ENV_USERS_JSON = "USERS_JSON"
ENV_USERS_FILE = "USERS_FILE"
ENV_REQUIRE_AUTH = "RANKINGS_REQUIRE_AUTH"

Role = Literal["admin", "member"]


@dataclass(frozen=True)
class Principal:
    role: Role
    account_id: Optional[str]
    name: str
    key_hint: str
    kind: str  # "service" | "user"


def get_configured_api_key() -> Optional[str]:
    key = (os.environ.get(ENV_KEY) or "").strip()
    return key or None


def api_key_configured() -> bool:
    return get_configured_api_key() is not None


def rankings_require_auth() -> bool:
    return (os.environ.get(ENV_REQUIRE_AUTH) or "").strip().lower() in (
        "1",
        "true",
        "yes",
        "on",
    )


# Shipped placeholders. Health and the UI warn while any of these are still active.
# Do not add real customer secrets here.
_DEMO_SERVICE_KEYS = frozenset({"dev-token-rankings-change-me"})
_DEMO_USER_KEYS = frozenset({"demo-admin-key", "demo-alice-key", "demo-dave-key"})
_PLACEHOLDER_FRAGMENTS = (
    "change-me",
    "change_me",
    "changeme",
    "replace-me",
    "your-key",
    "your_key",
)

MSG_INVALID_KEY = (
    "密钥不正确。请核对后再试。"
    "在页面上请粘贴管理员发给你的用户密钥，不要粘贴采集用的写入密钥。"
)
MSG_MISSING_KEY = (
    "还没有登录。请打开网页 /login：公开站点用 Google 登录，"
    "私有部署粘贴用户密钥。脚本请在请求头加上 Authorization: Bearer <用户密钥>。"
)
MSG_AUTH_REQUIRED = "这个站已经要求登录后才能看排行。请打开 /login。"
MSG_ADMIN_REQUIRED = "这个操作只有管理员可以做。请换用管理员密钥。"
MSG_MEMBER_NO_ACCOUNT = "你的身份没有绑定账号，暂时不能查看数据。请联系管理员检查用户名单。"
MSG_ACCOUNT_FORBIDDEN = "你只能查看自己的账号，不能查看别人的数据。"
MSG_MEMBER_WRITE_FORBIDDEN = (
    "成员密钥不能写入用量，也不能代替别人的账号上报。请改用采集用的写入密钥。"
)
MSG_WRITE_KEY_MISSING = (
    "写入功能还没配置密钥。请设置环境变量 TOKEN_RANKINGS_API_KEY（见安装说明）。"
    "看排行不需要这个密钥，除非已经要求登录。"
)


def is_placeholder_secret(value: Optional[str]) -> bool:
    """True for CHANGE_ME-style placeholders. Never treats a real secret as documentation."""
    if not value:
        return False
    key = value.strip().lower()
    if key in {"change_me", "changeme", "change-me", "todo", "secret", "xxx"}:
        return True
    return any(fragment in key for fragment in _PLACEHOLDER_FRAGMENTS)


def _sha256_hex(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _looks_like_sha256(value: str) -> bool:
    text = (value or "").strip().lower()
    return len(text) == 64 and all(c in "0123456789abcdef" for c in text)


# Hashes of shipped placeholders. A production file that only stores sha256
# still trips the yellow banner if someone hashed a demo key.
_DEMO_USER_KEY_SHA256 = frozenset(_sha256_hex(k) for k in _DEMO_USER_KEYS)
_PLACEHOLDER_KEY_SHA256 = frozenset(
    _sha256_hex(p)
    for p in (
        "change_me",
        "changeme",
        "change-me",
        "CHANGE_ME",
        "todo",
        "secret",
        "xxx",
        "dev-token-rankings-change-me",
        *tuple(_DEMO_SERVICE_KEYS),
        *tuple(_DEMO_USER_KEYS),
    )
)


def user_record_uses_demo_key(api_key: str, digest: str) -> bool:
    """True when a roster row still carries a demo or placeholder credential."""
    key = (api_key or "").strip()
    if key and (key in _DEMO_USER_KEYS or key in _DEMO_SERVICE_KEYS or is_placeholder_secret(key)):
        return True
    digest_norm = (digest or "").strip().lower()
    if not digest_norm:
        return False
    if not _looks_like_sha256(digest_norm):
        return True
    return digest_norm in _DEMO_USER_KEY_SHA256 or digest_norm in _PLACEHOLDER_KEY_SHA256


def setup_warnings() -> dict[str, Any]:
    """Non-secret flags for the first-run banner. Does not echo key material."""
    service = get_configured_api_key()
    demo_service = bool(service) and (
        service in _DEMO_SERVICE_KEYS or is_placeholder_secret(service)
    )
    demo_user = False
    try:
        directory = load_user_directory()
    except HTTPException:
        directory = []
    for user in directory:
        if user_record_uses_demo_key(
            str(user.get("api_key") or ""),
            str(user.get("api_key_sha256") or ""),
        ):
            demo_user = True
            break
    warnings: list[str] = []
    if demo_service:
        warnings.append(
            "写入密钥仍是演示值。请改成只有你们自己知道的一长串随机字符，"
            "不要继续用 dev-token-rankings-change-me 或 CHANGE_ME。"
        )
    if demo_user:
        warnings.append(
            "用户密钥仍是演示值（例如 demo-alice-key）。正式使用前请在用户名单里全部换掉。"
        )
    if not rankings_require_auth():
        warnings.append("排行榜目前不强制登录。生产环境请设置 RANKINGS_REQUIRE_AUTH=1。")
    return {
        "demo_keys_in_use": demo_service or demo_user,
        "demo_service_key": demo_service,
        "demo_user_keys": demo_user,
        "rankings_require_auth": rankings_require_auth(),
        "warnings": warnings,
    }


def extract_presented_key(
    authorization: Optional[str],
    x_api_key: Optional[str],
) -> Optional[str]:
    if x_api_key and x_api_key.strip():
        return x_api_key.strip()
    if authorization:
        auth = authorization.strip()
        if auth.lower().startswith("bearer "):
            token = auth[7:].strip()
            if token:
                return token
    return None


def _keys_match(presented: str, expected: str) -> bool:
    a = presented.encode("utf-8")
    b = expected.encode("utf-8")
    if len(a) != len(b):
        secrets.compare_digest(b, b)
        return False
    return secrets.compare_digest(a, b)


def _key_hint(key: str) -> str:
    if len(key) <= 4:
        return "****"
    return "…" + key[-4:]


def _load_users_raw() -> list[dict[str, Any]]:
    raw = (os.environ.get(ENV_USERS_JSON) or "").strip()
    if raw:
        try:
            data = json.loads(raw)
        except json.JSONDecodeError as e:
            raise HTTPException(
                status_code=500,
                detail={
                    "error": "users_json_invalid",
                    "message": f"USERS_JSON is not valid JSON: {e}",
                },
            ) from e
        if not isinstance(data, list):
            raise HTTPException(
                status_code=500,
                detail={
                    "error": "users_json_invalid",
                    "message": "USERS_JSON must be a JSON array",
                },
            )
        return data

    path = (os.environ.get(ENV_USERS_FILE) or "").strip()
    if not path:
        return []
    p = Path(path)
    if not p.is_file():
        raise HTTPException(
            status_code=500,
            detail={
                "error": "users_file_missing",
                "message": f"USERS_FILE not found: {path}",
            },
        )
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
    except json.JSONDecodeError as e:
        raise HTTPException(
            status_code=500,
            detail={
                "error": "users_file_invalid",
                "message": f"USERS_FILE is not valid JSON: {e}",
            },
        ) from e
    if not isinstance(data, list):
        raise HTTPException(
            status_code=500,
            detail={
                "error": "users_file_invalid",
                "message": "USERS_FILE must be a JSON array",
            },
        )
    return data


def load_user_directory() -> list[dict[str, Any]]:
    """Normalized user records (api_key, role, account_id, name)."""
    out: list[dict[str, Any]] = []
    for item in _load_users_raw():
        if not isinstance(item, dict):
            continue
        api_key = str(item.get("api_key") or item.get("key") or "").strip()
        digest = str(item.get("api_key_sha256") or "").strip().lower()
        if not api_key and not digest:
            continue
        role_raw = str(item.get("role") or "member").strip().lower()
        role: Role = "admin" if role_raw == "admin" else "member"
        account_id = item.get("account_id")
        if account_id is not None:
            account_id = str(account_id).strip() or None
        name = str(item.get("name") or account_id or role).strip()
        email = str(item.get("email") or "").strip().lower()
        if role == "member" and not account_id:
            # skip invalid member without account scope
            continue
        out.append(
            {
                "api_key": api_key,
                "api_key_sha256": digest,
                "role": role,
                "account_id": account_id,
                "name": name,
                "email": email,
            }
        )
    return out


def users_configured() -> bool:
    try:
        return len(load_user_directory()) > 0
    except HTTPException:
        return False


def resolve_principal(presented: Optional[str]) -> Optional[Principal]:
    """Map a presented key to Principal, or None if empty. Unknown key → 401."""
    if not presented:
        return None

    service = get_configured_api_key()
    if service and _keys_match(presented, service):
        return Principal(
            role="admin",
            account_id=None,
            name="service",
            key_hint=_key_hint(presented),
            kind="service",
        )

    presented_hash = _sha256_hex(presented)
    for u in load_user_directory():
        raw = str(u.get("api_key") or "")
        digest = str(u.get("api_key_sha256") or "")
        matched = False
        if raw and _keys_match(presented, raw):
            matched = True
        elif digest and _looks_like_sha256(digest) and _keys_match(presented_hash, digest):
            matched = True
        if matched:
            return Principal(
                role=u["role"],
                account_id=u.get("account_id"),
                name=u["name"],
                key_hint=_key_hint(presented),
                kind="user",
            )

    raise HTTPException(
        status_code=401,
        detail={
            "error": "invalid_api_key",
            "message": MSG_INVALID_KEY,
        },
        headers={"WWW-Authenticate": "Bearer"},
    )


async def require_write_api_key(
    authorization: Optional[str] = Header(None),
    x_api_key: Optional[str] = Header(None, alias="X-API-Key"),
) -> str:
    """Dependency for ingest / sync / scan / admin / seed writes."""
    expected = get_configured_api_key()
    if not expected:
        raise HTTPException(
            status_code=503,
            detail={
                "error": "api_key_not_configured",
                "message": MSG_WRITE_KEY_MISSING,
            },
        )
    presented = extract_presented_key(authorization, x_api_key)
    if not presented:
        raise HTTPException(
            status_code=401,
            detail={
                "error": "missing_api_key",
                "message": MSG_MISSING_KEY,
            },
            headers={"WWW-Authenticate": "Bearer"},
        )
    if not _keys_match(presented, expected):
        # Allow admin *user* keys to also write (optional convenience)
        try:
            principal = resolve_principal(presented)
        except HTTPException:
            principal = None
        if principal and principal.role == "admin" and principal.kind == "user":
            return presented
        if principal and principal.role == "member":
            raise HTTPException(
                status_code=403,
                detail={
                    "error": "member_write_forbidden",
                    "message": MSG_MEMBER_WRITE_FORBIDDEN,
                },
            )
        raise HTTPException(
            status_code=401,
            detail={
                "error": "invalid_api_key",
                "message": MSG_INVALID_KEY,
            },
            headers={"WWW-Authenticate": "Bearer"},
        )
    return presented


async def require_ingest_api_key(
    authorization: Optional[str] = Header(None),
    x_api_key: Optional[str] = Header(None, alias="X-API-Key"),
) -> str:
    """Ingest only: service key, admin user key, or a personal collector token.

    Collector tokens are account-scoped. They are not accepted by scan, sync,
    seed, or admin clear.
    """
    presented = extract_presented_key(authorization, x_api_key)
    if presented and presented.startswith("ctr_"):
        from app.self_service import match_collector_token

        if match_collector_token(presented):
            return presented
    return await require_write_api_key(authorization, x_api_key)


def resolve_browser_principal(
    presented: Optional[str],
    session_id: Optional[str],
) -> Optional[Principal]:
    """API key wins when a caller sends one. Otherwise use the Google session cookie."""
    if presented:
        return resolve_principal(presented)
    if session_id and session_id.strip():
        from app.google_auth import principal_for_session

        return principal_for_session(session_id.strip())
    return None


async def optional_principal(
    authorization: Optional[str] = Header(None),
    x_api_key: Optional[str] = Header(None, alias="X-API-Key"),
    tr_session: Optional[str] = Cookie(None, alias="tr_session"),
) -> Optional[Principal]:
    """For ranking GETs: anonymous OK unless RANKINGS_REQUIRE_AUTH."""
    presented = extract_presented_key(authorization, x_api_key)
    principal = resolve_browser_principal(presented, tr_session)
    if principal is None and rankings_require_auth():
        raise HTTPException(
            status_code=401,
            detail={
                "error": "auth_required",
                "message": MSG_AUTH_REQUIRED,
            },
            headers={"WWW-Authenticate": "Bearer"},
        )
    return principal


async def require_principal(
    authorization: Optional[str] = Header(None),
    x_api_key: Optional[str] = Header(None, alias="X-API-Key"),
    tr_session: Optional[str] = Cookie(None, alias="tr_session"),
) -> Principal:
    """For /me, export, forum posts — Google session cookie or a user API key."""
    presented = extract_presented_key(authorization, x_api_key)
    if not presented and not (tr_session and tr_session.strip()):
        raise HTTPException(
            status_code=401,
            detail={
                "error": "missing_api_key",
                "message": MSG_MISSING_KEY,
            },
            headers={"WWW-Authenticate": "Bearer"},
        )
    principal = resolve_browser_principal(presented, tr_session)
    if principal is None:
        raise HTTPException(
            status_code=401,
            detail={
                "error": "invalid_api_key" if presented else "missing_api_key",
                "message": MSG_INVALID_KEY if presented else MSG_MISSING_KEY,
            },
            headers={"WWW-Authenticate": "Bearer"},
        )
    return principal


async def require_admin(
    authorization: Optional[str] = Header(None),
    x_api_key: Optional[str] = Header(None, alias="X-API-Key"),
    tr_session: Optional[str] = Cookie(None, alias="tr_session"),
) -> Principal:
    """Admin-only dependency (audit list, etc.)."""
    presented = extract_presented_key(authorization, x_api_key)
    if not presented and not (tr_session and tr_session.strip()):
        raise HTTPException(
            status_code=401,
            detail={
                "error": "missing_api_key",
                "message": MSG_MISSING_KEY,
            },
            headers={"WWW-Authenticate": "Bearer"},
        )
    p = resolve_browser_principal(presented, tr_session)
    if p is None:
        raise HTTPException(
            status_code=401,
            detail={
                "error": "invalid_api_key" if presented else "missing_api_key",
                "message": MSG_INVALID_KEY if presented else MSG_MISSING_KEY,
            },
            headers={"WWW-Authenticate": "Bearer"},
        )
    if p.role != "admin":
        raise HTTPException(
            status_code=403,
            detail={
                "error": "admin_required",
                "message": MSG_ADMIN_REQUIRED,
            },
        )
    return p


def apply_account_scope(
    principal: Optional[Principal],
    requested_account_id: Optional[str],
) -> Optional[str]:
    """Return the account_id filter to apply.

    - member: always their account_id; requesting another → 403
    - admin / anonymous: use requested_account_id (may be None = all)
    """
    if principal is None:
        return requested_account_id
    if principal.role == "admin":
        return requested_account_id
    # member
    own = (principal.account_id or "").strip()
    if not own:
        raise HTTPException(
            status_code=403,
            detail={
                "error": "member_missing_account",
                "message": MSG_MEMBER_NO_ACCOUNT,
            },
        )
    if requested_account_id and requested_account_id.strip() != own:
        raise HTTPException(
            status_code=403,
            detail={
                "error": "account_forbidden",
                "message": MSG_ACCOUNT_FORBIDDEN,
            },
        )
    return own


def auth_status() -> dict[str, Any]:
    users = []
    try:
        users = load_user_directory()
    except HTTPException as e:
        return {
            "write_auth_configured": api_key_configured(),
            "users_configured": False,
            "users_error": e.detail,
            "rankings_require_auth": rankings_require_auth(),
            "user_count": 0,
        }
    return {
        "write_auth_configured": api_key_configured(),
        "users_configured": len(users) > 0,
        "rankings_require_auth": rankings_require_auth(),
        "user_count": len(users),
        "users": [
            {
                "name": u["name"],
                "role": u["role"],
                "account_id": u.get("account_id"),
                "key_hint": _key_hint(u.get("api_key") or u.get("api_key_sha256") or ""),
            }
            for u in users
        ],
    }
