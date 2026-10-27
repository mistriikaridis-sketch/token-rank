"""Request-scoped collector token, set before the endpoint runs.

Starlette copies contextvars into the threadpool, so this has to be set in
middleware on the request task. Setting it only inside a sync dependency
does not reach ``insert_usage_rows``.
"""
from __future__ import annotations

from contextvars import ContextVar, Token
from dataclasses import dataclass
from typing import Optional


@dataclass(frozen=True)
class CollectorScope:
    account_id: str
    token_hint: str


_current: ContextVar[Optional[CollectorScope]] = ContextVar(
    "token_rankings_collector_scope",
    default=None,
)


def set_collector_scope(scope: Optional[CollectorScope]) -> Token:
    return _current.set(scope)


def reset_collector_scope(token: Token) -> None:
    _current.reset(token)


def current_collector_scope() -> Optional[CollectorScope]:
    return _current.get()
