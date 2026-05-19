"""Observability: structlog (JSON), Langfuse tracing, PII redaction.

Contract with the calling Crew:
- Correlation id (document_id, run_id) flows in via contextvars.
- Every extraction emits a Langfuse trace; every model decision a child span.
- PII never appears in logs unredacted. Redaction happens at the structlog
  boundary, so application code does not need to think about it.
"""

from __future__ import annotations

import logging
import re
import sys
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from typing import Any, Protocol, cast

import structlog
from structlog.contextvars import bind_contextvars, clear_contextvars
from structlog.types import EventDict, Processor

from putsch_docs.config import ObservabilitySettings, get_settings

_DOCUMENT_ID: ContextVar[str | None] = ContextVar("document_id", default=None)
_RUN_ID: ContextVar[str | None] = ContextVar("run_id", default=None)

# ----- PII redaction -----------------------------------------------------------------

# Conservative: match common PII shapes in free-form German invoice text.
# These run on log records, not on extracted Pydantic fields (which are
# already structured and PII-classified at the schema level).
_PII_PATTERNS: dict[str, re.Pattern[str]] = {
    # IBAN: country code + 13–32 alnum (Germany is DE + 20 digits)
    "iban": re.compile(r"\b[A-Z]{2}\d{2}[A-Z0-9]{10,30}\b"),
    # USt-IdNr: country code + up to 12 alphanumeric
    "ustid": re.compile(r"\b[A-Z]{2}[A-Z0-9]{8,12}\b"),
    # Email
    "email": re.compile(r"\b[\w.+-]+@[\w-]+\.[\w.-]+\b"),
    # German tax number (Steuernummer) — patterns vary by Bundesland
    "steuer_nr": re.compile(r"\b\d{2,3}/\d{2,4}/\d{4,5}\b"),
    # German phone
    "phone": re.compile(r"\b(?:\+49|0)[\s\-()/]?\d[\d\s\-()/]{7,18}\b"),
}


class PIIRedactor(Protocol):
    """Public contract — the observability module exposes this so the rest of the
    stack can plug in its own redaction policy without importing structlog."""

    def redact(self, text: str) -> str: ...


class DefaultPIIRedactor:
    """Default implementation. Replaces matches with `<{kind}:redacted>`."""

    def redact(self, text: str) -> str:
        out = text
        for kind, pat in _PII_PATTERNS.items():
            out = pat.sub(f"<{kind}:redacted>", out)
        return out


_REDACTOR: PIIRedactor = DefaultPIIRedactor()


def set_pii_redactor(redactor: PIIRedactor) -> None:
    """Swap in an alternative redaction policy (e.g., the obs module's stricter one)."""
    global _REDACTOR
    _REDACTOR = redactor


# ----- structlog processors ---------------------------------------------------------


def _add_correlation_ids(_: Any, __: str, event_dict: EventDict) -> EventDict:
    doc_id = _DOCUMENT_ID.get()
    if doc_id:
        event_dict.setdefault("document_id", doc_id)
    run_id = _RUN_ID.get()
    if run_id:
        event_dict.setdefault("run_id", run_id)
    return event_dict


def _redact_event_dict(_: Any, __: str, event_dict: EventDict) -> EventDict:
    """Walk the event dict and redact any string value.

    Fields named in `_NEVER_LOG` are dropped entirely. Strings elsewhere are
    pattern-redacted. Bytes are summarized (length only).
    """
    if not get_settings().obs.redact_pii_in_logs:
        return event_dict

    cleaned: EventDict = {}
    for k, v in event_dict.items():
        if k in _NEVER_LOG:
            cleaned[k] = "<dropped:never_log>"
            continue
        cleaned[k] = _redact_value(v)
    return cleaned


_NEVER_LOG: frozenset[str] = frozenset(
    {
        "document_bytes",
        "raw_pdf",
        "page_image",
        "page_images",
        "api_key",
        "secret",
    }
)


def _redact_value(v: Any) -> Any:
    if isinstance(v, str):
        return _REDACTOR.redact(v)
    if isinstance(v, bytes):
        return f"<bytes:len={len(v)}>"
    if isinstance(v, dict):
        return {k: _redact_value(vv) for k, vv in v.items()}
    if isinstance(v, list | tuple):
        return [_redact_value(x) for x in v]
    return v


def configure_logging(settings: ObservabilitySettings | None = None) -> None:
    """Idempotent. Safe to call from app entry points and tests."""
    settings = settings or get_settings().obs

    processors: list[Processor] = [
        structlog.contextvars.merge_contextvars,
        _add_correlation_ids,
        structlog.processors.add_log_level,
        structlog.processors.TimeStamper(fmt="iso", utc=True),
        structlog.processors.StackInfoRenderer(),
        structlog.processors.format_exc_info,
        _redact_event_dict,
        structlog.processors.JSONRenderer(),
    ]

    structlog.configure(
        processors=processors,
        wrapper_class=structlog.make_filtering_bound_logger(
            getattr(logging, settings.log_level)
        ),
        context_class=dict,
        logger_factory=structlog.PrintLoggerFactory(file=sys.stderr),
        cache_logger_on_first_use=True,
    )


def get_logger(name: str | None = None) -> structlog.stdlib.BoundLogger:
    return cast("structlog.stdlib.BoundLogger", structlog.get_logger(name))


# ----- correlation context helpers --------------------------------------------------


@contextmanager
def correlation(
    *, document_id: str | None = None, run_id: str | None = None
) -> Iterator[tuple[str, str]]:
    """Bind document_id + run_id for the duration of the with-block.

    The AP Crew passes its own run_id when it calls the extractor; we mint a
    fresh document_id per file unless the caller supplied one.
    """
    doc_id = document_id or f"doc_{uuid.uuid4().hex[:16]}"
    r_id = run_id or f"run_{uuid.uuid4().hex[:16]}"
    doc_tok = _DOCUMENT_ID.set(doc_id)
    run_tok = _RUN_ID.set(r_id)
    bind_contextvars(document_id=doc_id, run_id=r_id)
    try:
        yield doc_id, r_id
    finally:
        _DOCUMENT_ID.reset(doc_tok)
        _RUN_ID.reset(run_tok)
        clear_contextvars()


def current_document_id() -> str | None:
    return _DOCUMENT_ID.get()


def current_run_id() -> str | None:
    return _RUN_ID.get()


# ----- Langfuse client (lazy) -------------------------------------------------------


class _LangfuseHandle:
    """Lazy Langfuse client. None when disabled or keys absent."""

    _client: Any | None = None
    _initialized: bool = False

    @classmethod
    def get(cls) -> Any | None:
        if cls._initialized:
            return cls._client
        cls._initialized = True
        settings = get_settings().obs
        if not settings.langfuse_enabled:
            return None
        pub = settings.langfuse_public_key.get_secret_value()
        sec = settings.langfuse_secret_key.get_secret_value()
        if not pub or not sec:
            get_logger(__name__).info(
                "langfuse.disabled", reason="missing_keys", host=str(settings.langfuse_host)
            )
            return None
        try:
            from langfuse import Langfuse  # local import — optional dep at runtime

            cls._client = Langfuse(
                public_key=pub,
                secret_key=sec,
                host=str(settings.langfuse_host),
            )
        except Exception as exc:  # pragma: no cover — defensive
            get_logger(__name__).warning("langfuse.init_failed", error=str(exc))
            cls._client = None
        return cls._client


def langfuse_client() -> Any | None:
    """Return the singleton Langfuse client, or None if disabled."""
    return _LangfuseHandle.get()
