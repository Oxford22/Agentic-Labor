"""Structured logging configuration.

JSON output by default — the Frankfurt log sink (Vector → Loki) consumes
JSON; the `console` renderer is only for local dev.

Every log line carries `correlation_id` (the Langfuse trace ID) when it
is set in the context. Agent code that writes to memory MUST set it via
`bind_correlation_id` so the audit trail closes.
"""

from __future__ import annotations

import logging
import sys
from contextvars import ContextVar
from typing import Any

import structlog
from structlog.contextvars import bind_contextvars, clear_contextvars
from structlog.types import EventDict, Processor, WrappedLogger

from putsch_memory.config import settings

_correlation_id: ContextVar[str | None] = ContextVar("correlation_id", default=None)
_configured: bool = False


def _inject_correlation(
    _logger: WrappedLogger, _method: str, event_dict: EventDict
) -> EventDict:
    cid = _correlation_id.get()
    if cid is not None:
        event_dict.setdefault("correlation_id", cid)
    event_dict.setdefault("component", "putsch_memory")
    event_dict.setdefault("region", settings.region)
    return event_dict


def _drop_sensitive(
    _logger: WrappedLogger, _method: str, event_dict: EventDict
) -> EventDict:
    """Best-effort scrub for known-sensitive keys.

    This is the *last line of defense*; the writers already redact PII
    via `putsch_obs.redaction`. We never log full payload bodies for
    personnel facts; this filter exists for defense-in-depth and to
    catch developer mistakes during code review.
    """
    sensitive_keys = {"password", "api_key", "secret", "token", "iban", "ust_id_nr"}
    for key in list(event_dict.keys()):
        if key.lower() in sensitive_keys:
            event_dict[key] = "***redacted***"
    return event_dict


def configure_logging(*, force: bool = False) -> None:
    """Initialise structlog. Idempotent unless `force=True`."""
    global _configured
    if _configured and not force:
        return

    processors: list[Processor] = [
        structlog.contextvars.merge_contextvars,
        structlog.processors.add_log_level,
        structlog.processors.TimeStamper(fmt="iso", utc=True),
        structlog.processors.StackInfoRenderer(),
        structlog.processors.format_exc_info,
        _inject_correlation,
        _drop_sensitive,
    ]

    if settings.log_format == "json":
        processors.append(structlog.processors.JSONRenderer(serializer=_json_serializer))
    else:
        processors.append(structlog.dev.ConsoleRenderer(colors=sys.stderr.isatty()))

    structlog.configure(
        processors=processors,
        wrapper_class=structlog.make_filtering_bound_logger(
            logging.getLevelName(settings.log_level)
        ),
        context_class=dict,
        logger_factory=structlog.PrintLoggerFactory(file=sys.stderr),
        cache_logger_on_first_use=True,
    )

    # Tame the noisy upstreams.
    for noisy in ("neo4j", "httpx", "httpcore", "graphiti_core"):
        logging.getLogger(noisy).setLevel(logging.WARNING)

    _configured = True


def _json_serializer(obj: Any, **kwargs: Any) -> str:
    # orjson is faster and handles datetime natively, but it returns bytes;
    # structlog wants str. Falling back to stdlib `json` when orjson unset.
    try:
        import orjson

        return orjson.dumps(obj, option=orjson.OPT_NAIVE_UTC | orjson.OPT_SORT_KEYS).decode()
    except ImportError:  # pragma: no cover — orjson is a hard dep
        import json

        return json.dumps(obj, default=str, sort_keys=True)


def bind_correlation_id(correlation_id: str) -> None:
    """Bind the Langfuse trace id to every subsequent log line in this task."""
    _correlation_id.set(correlation_id)
    bind_contextvars(correlation_id=correlation_id)


def clear_correlation_id() -> None:
    _correlation_id.set(None)
    clear_contextvars()


def get_logger(name: str | None = None) -> structlog.stdlib.BoundLogger:
    configure_logging()
    return structlog.get_logger(name) if name else structlog.get_logger()
