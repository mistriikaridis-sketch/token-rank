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
"""
from __future__ import annotations

import json
import os
import secrets
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal, Optional

from fastapi import Header, HTTPException

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
        if not api_key:
            continue
        role_raw = str(item.get("role") or "member").strip().lower()
        role: Role = "admin" if role_raw == "admin" else "member"
        account_id = item.get("account_id")
        if account_id is not None:
            account_id = str(account_id).strip() or None
        name = str(item.get("name") or account_id or role).strip()
        if role == "member" and not account_id:
            # skip invalid member without account scope
            continue
        out.append(
            {
                "api_key": api_key,
                "role": role,
                "account_id": account_id,
                "name": name,
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

    for u in load_user_directory():
        if _keys_match(presented, u["api_key"]):
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
            "message": "Invalid API key",
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
                "message": (
                    "Write APIs require TOKEN_RANKINGS_API_KEY. "
                    "Set it in the environment (see docker-compose.yml). "
                    "Ranking GET endpoints remain public unless RANKINGS_REQUIRE_AUTH=1."
                ),
            },
        )
    presented = extract_presented_key(authorization, x_api_key)
    if not presented:
        raise HTTPException(
            status_code=401,
            detail={
                "error": "missing_api_key",
                "message": "Provide Authorization: Bearer <key> or X-API-Key header",
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
        raise HTTPException(
            status_code=401,
            detail={
                "error": "invalid_api_key",
                "message": "Invalid API key",
            },
            headers={"WWW-Authenticate": "Bearer"},
        )
    return presented


async def optional_principal(
    authorization: Optional[str] = Header(None),
    x_api_key: Optional[str] = Header(None, alias="X-API-Key"),
) -> Optional[Principal]:
    """For ranking GETs: anonymous OK unless RANKINGS_REQUIRE_AUTH."""
    presented = extract_presented_key(authorization, x_api_key)
    principal = resolve_principal(presented)
    if principal is None and rankings_require_auth():
        raise HTTPException(
            status_code=401,
            detail={
                "error": "auth_required",
                "message": (
                    "RANKINGS_REQUIRE_AUTH is set; provide Authorization: Bearer "
                    "<user or service key> or X-API-Key"
                ),
            },
            headers={"WWW-Authenticate": "Bearer"},
        )
    return principal


async def require_principal(
    authorization: Optional[str] = Header(None),
    x_api_key: Optional[str] = Header(None, alias="X-API-Key"),
) -> Principal:
    """For /me, export, audit — must present a valid key."""
    presented = extract_presented_key(authorization, x_api_key)
    if not presented:
        raise HTTPException(
            status_code=401,
            detail={
                "error": "missing_api_key",
                "message": "Provide Authorization: Bearer <key> or X-API-Key header",
            },
            headers={"WWW-Authenticate": "Bearer"},
        )
    principal = resolve_principal(presented)
    if principal is None:
        raise HTTPException(
            status_code=401,
            detail={
                "error": "invalid_api_key",
                "message": "Invalid API key",
            },
            headers={"WWW-Authenticate": "Bearer"},
        )
    return principal


async def require_admin(
    authorization: Optional[str] = Header(None),
    x_api_key: Optional[str] = Header(None, alias="X-API-Key"),
) -> Principal:
    """Admin-only dependency (audit list, etc.)."""
    presented = extract_presented_key(authorization, x_api_key)
    if not presented:
        raise HTTPException(
            status_code=401,
            detail={
                "error": "missing_api_key",
                "message": "Provide Authorization: Bearer <key> or X-API-Key header",
            },
            headers={"WWW-Authenticate": "Bearer"},
        )
    p = resolve_principal(presented)
    if p is None or p.role != "admin":
        raise HTTPException(
            status_code=403,
            detail={
                "error": "admin_required",
                "message": "This endpoint requires an admin API key",
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
                "message": "Member principal has no account_id configured",
            },
        )
    if requested_account_id and requested_account_id.strip() != own:
        raise HTTPException(
            status_code=403,
            detail={
                "error": "account_forbidden",
                "message": (
                    f"Member can only access account_id={own}; "
                    f"requested={requested_account_id.strip()}"
                ),
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
                "key_hint": _key_hint(u["api_key"]),
            }
            for u in users
        ],
    }
