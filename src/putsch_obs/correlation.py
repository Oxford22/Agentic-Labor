"""Correlation IDs.

Every request, every agent run, every eval item gets a correlation id. It
flows through:

* structlog: bound to every log record
* OTel: stamped on every span as ``putsch.correlation_id``
* Langfuse: passed as the ``trace_id`` when creating top-level traces, so a
  log line and a Langfuse trace are always one click apart.

We use a contextvar rather than threadlocal because the stack is async.
"""

from __future__ import annotations

import secrets
from contextlib import contextmanager
from contextvars import ContextVar
from typing import Iterator

_CORRELATION_ID: ContextVar[str | None] = ContextVar("putsch_correlation_id", default=None)


def new_correlation_id() -> str:
    """Generate a fresh, URL-safe correlation id."""
    return secrets.token_urlsafe(16)


def get_correlation_id() -> str | None:
    """Return the correlation id bound to the current context, or ``None``."""
    return _CORRELATION_ID.get()


def set_correlation_id(value: str | None) -> None:
    """Bind a correlation id to the current context."""
    _CORRELATION_ID.set(value)


@contextmanager
def correlation_scope(value: str | None = None) -> Iterator[str]:
    """Context manager that binds a correlation id for the duration of the block.

    >>> with correlation_scope() as cid:
    ...     do_work()  # every span and log inside carries `cid`
    """
    cid = value or new_correlation_id()
    token = _CORRELATION_ID.set(cid)
    try:
        yield cid
    finally:
        _CORRELATION_ID.reset(token)


__all__ = [
    "correlation_scope",
    "get_correlation_id",
    "new_correlation_id",
    "set_correlation_id",
]
