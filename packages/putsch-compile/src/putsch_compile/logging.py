"""Structured logging. JSON to stdout. Correlation IDs propagate through ``contextvars``.

Why this exists separately from any framework's logging: Langfuse, structlog, DSPy, and LiteLLM
each have a notion of "trace context" and none of them agree. We pick one — structlog with a
``contextvars`` binder — and bind everything else to it. The correlation ID is the join key in
incident response, so it must be in every log line emitted while a request is being served.
"""

from __future__ import annotations

import logging
import sys
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from typing import Any

import structlog
from structlog.contextvars import bind_contextvars, clear_contextvars
from structlog.types import EventDict, Processor

_configured = False

_correlation_id: ContextVar[str | None] = ContextVar("putsch_correlation_id", default=None)


def _add_correlation_id(_logger: Any, _method: str, event_dict: EventDict) -> EventDict:
    cid = _correlation_id.get()
    if cid is not None:
        event_dict.setdefault("correlation_id", cid)
    return event_dict


def _add_logger_name(logger: Any, _method: str, event_dict: EventDict) -> EventDict:
    """Like ``structlog.stdlib.add_logger_name`` but tolerant of structlog's PrintLogger.

    The stdlib version assumes a ``.name`` attribute that PrintLogger doesn't expose. Falling back
    silently keeps logging from blowing up in unit tests that don't configure stdlib.
    """

    name = getattr(logger, "name", None)
    if name:
        event_dict.setdefault("logger", name)
    return event_dict


def configure_logging(level: str = "INFO", *, json: bool = True) -> None:
    """Idempotent. Safe to call from CLI entrypoints, tests, or nothing at all."""

    global _configured
    if _configured:
        return

    timestamper = structlog.processors.TimeStamper(fmt="iso", utc=True)

    shared_processors: list[Processor] = [
        structlog.contextvars.merge_contextvars,
        structlog.stdlib.add_log_level,
        _add_logger_name,
        _add_correlation_id,
        timestamper,
        structlog.processors.StackInfoRenderer(),
        structlog.processors.format_exc_info,
    ]

    if json:
        shared_processors.append(structlog.processors.JSONRenderer(serializer=_orjson_dumps))
    else:
        shared_processors.append(structlog.dev.ConsoleRenderer(colors=sys.stderr.isatty()))

    structlog.configure(
        processors=shared_processors,
        wrapper_class=structlog.make_filtering_bound_logger(getattr(logging, level.upper())),
        context_class=dict,
        logger_factory=structlog.PrintLoggerFactory(),
        cache_logger_on_first_use=True,
    )

    # Route stdlib logging through structlog too — DSPy / LiteLLM use stdlib loggers.
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(
        structlog.stdlib.ProcessorFormatter(
            processor=shared_processors[-1],
            foreign_pre_chain=shared_processors[:-1],
        )
    )
    root = logging.getLogger()
    root.handlers = [handler]
    root.setLevel(level)

    _configured = True


def _orjson_dumps(obj: Any, default: Any = None) -> str:
    """Tiny orjson wrapper so structlog can ship bytes-clean JSON without pulling stdlib json."""
    import orjson

    return orjson.dumps(obj, default=default).decode("utf-8")


def new_correlation_id() -> str:
    return uuid.uuid4().hex


@contextmanager
def correlation_scope(correlation_id: str | None = None, **bindings: Any) -> Iterator[str]:
    """Set a correlation ID + optional bindings for the duration of a block.

    Use at every entrypoint: HTTP handler, CLI command, scheduled job. The ID propagates into every
    structlog call below and into the Langfuse trace via ``tracing.start_span``.
    """

    cid = correlation_id or new_correlation_id()
    token = _correlation_id.set(cid)
    bind_contextvars(correlation_id=cid, **bindings)
    try:
        yield cid
    finally:
        _correlation_id.reset(token)
        clear_contextvars()


def get_correlation_id() -> str | None:
    return _correlation_id.get()


def get_logger(name: str | None = None) -> structlog.stdlib.BoundLogger:
    if not _configured:
        configure_logging()
    logger: structlog.stdlib.BoundLogger = (
        structlog.get_logger(name) if name else structlog.get_logger()
    )
    return logger
