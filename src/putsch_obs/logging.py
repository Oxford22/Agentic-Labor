"""Structlog wiring.

* JSON output to stdout (parsed by Loki, Datadog, whatever).
* Correlation id and active span id added on every record.
* Level is configurable via ``LOG_LEVEL`` env. Default ``INFO``.

Loggers obtained via :func:`get_logger` are safe to import at module top
level — wiring is idempotent.
"""

from __future__ import annotations

import logging
import os
import sys
from typing import Any

import structlog
from opentelemetry import trace as otel_trace

from putsch_obs.correlation import get_correlation_id

_CONFIGURED = False


def _add_correlation_id(_: Any, __: str, event_dict: dict[str, Any]) -> dict[str, Any]:
    cid = get_correlation_id()
    if cid is not None:
        event_dict.setdefault("correlation_id", cid)
    return event_dict


def _add_span_context(_: Any, __: str, event_dict: dict[str, Any]) -> dict[str, Any]:
    ctx = otel_trace.get_current_span().get_span_context()
    if ctx.is_valid:
        event_dict.setdefault("trace_id", format(ctx.trace_id, "032x"))
        event_dict.setdefault("span_id", format(ctx.span_id, "016x"))
    return event_dict


def configure_logging(level: str | None = None) -> None:
    """Wire structlog. Idempotent."""
    global _CONFIGURED
    if _CONFIGURED:
        return

    level_name = (level or os.environ.get("LOG_LEVEL") or "INFO").upper()
    level_num = getattr(logging, level_name, logging.INFO)

    # Stdlib handler — keep it minimal; structlog does the formatting.
    logging.basicConfig(
        format="%(message)s",
        stream=sys.stdout,
        level=level_num,
        force=True,
    )

    structlog.configure(
        processors=[
            structlog.contextvars.merge_contextvars,
            structlog.stdlib.add_log_level,
            structlog.processors.TimeStamper(fmt="iso", utc=True),
            _add_correlation_id,
            _add_span_context,
            structlog.processors.StackInfoRenderer(),
            structlog.processors.format_exc_info,
            structlog.processors.JSONRenderer(),
        ],
        wrapper_class=structlog.make_filtering_bound_logger(level_num),
        logger_factory=structlog.stdlib.LoggerFactory(),
        cache_logger_on_first_use=True,
    )

    _CONFIGURED = True


def get_logger(name: str | None = None) -> structlog.stdlib.BoundLogger:
    """Return a structlog logger. Configures on first call."""
    configure_logging()
    return structlog.get_logger(name)


__all__ = ["configure_logging", "get_logger"]
