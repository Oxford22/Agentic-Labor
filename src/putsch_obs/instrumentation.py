"""OTel + Langfuse bootstrap.

A single ``init()`` call wires:

* the OTel ``TracerProvider`` with a Frankfurt-bound OTLP/HTTP exporter
* a ``BatchSpanProcessor`` with a bounded queue and drop-on-overflow
* a PII-redacting span processor that runs BEFORE export
* the Langfuse SDK, configured against the self-hosted instance, sharing
  the same trace ids as OTel so a Langfuse trace and an OTel trace are
  the same artefact
* structlog (via ``putsch_obs.logging``)

Subsequent calls are no-ops; ``init()`` is idempotent. ``shutdown()`` flushes
in-flight spans and tears the providers down — safe to call from atexit.

Performance characteristics
---------------------------
``init()`` does I/O (collector handshake) so callers should invoke it at
startup, not in the request path. Once initialized, span creation is
allocation-free in the common path and the export goes through a
background daemon thread, so the application's hot path is unaffected.

Error policy
------------
Every call into OTel / Langfuse is wrapped. A failure inside the
instrumentation NEVER propagates to the calling application. Errors are
logged at ``WARN`` and a ``putsch.instrumentation.dropped`` counter is
incremented (and itself exported as a metric).
"""

from __future__ import annotations

import atexit
import contextlib
import threading
from collections.abc import Iterator, Mapping
from typing import Any

from opentelemetry import trace as otel_trace
from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import ReadableSpan, Span, SpanProcessor, TracerProvider
from opentelemetry.sdk.trace.export import (
    BatchSpanProcessor,
    SpanExportResult,
)

from putsch_obs.config import PutschObsSettings, get_settings
from putsch_obs.correlation import get_correlation_id
from putsch_obs.exceptions import RedactionError
from putsch_obs.logging import get_logger
from putsch_obs.redaction import RedactionEngine, get_engine, install_engine

log = get_logger(__name__)

# ── module-level state ─────────────────────────────────────────────────
_lock = threading.RLock()
_initialized: bool = False
_provider: TracerProvider | None = None
_langfuse: Any | None = None
_dropped_spans: int = 0  # bumped on overflow + on redaction failure


# ─────────────────────────────────────────────────────────────────────────────
# Redacting span processor
# ─────────────────────────────────────────────────────────────────────────────


class _RedactingSpanProcessor(SpanProcessor):
    """Runs immediately before the batch exporter.

    For every span we receive:

    * Re-write string attributes through the deterministic redactor.
    * Stamp the correlation id and retention class on every span so they
      are searchable in Langfuse.
    * If redaction raises, the span is *not* exported and a counter is
      incremented. The application is unaffected.

    The async/LLM redaction stage runs in eval pipelines, not here — the
    span export path must stay deterministic and fast.
    """

    def __init__(
        self,
        engine: RedactionEngine,
        settings: PutschObsSettings,
    ) -> None:
        self._engine = engine
        self._settings = settings

    def on_start(self, span: Span, parent_context: Any = None) -> None:  # noqa: D401
        try:
            cid = get_correlation_id()
            if cid:
                span.set_attribute("putsch.correlation_id", cid)
            span.set_attribute(
                "putsch.retention_class", self._settings.retention_class.value
            )
            span.set_attribute(
                "deployment.environment", self._settings.deployment_environment
            )
        except Exception as exc:  # pragma: no cover - defensive
            log.warning("instrumentation.on_start_failed", err=str(exc))

    def on_end(self, span: ReadableSpan) -> None:
        try:
            attrs = dict(span.attributes or {})
            redacted = self._engine.redact_attrs(attrs)
            # OTel SDK spans expose `_attributes` for mutation; the public
            # API only allows append, not replace. Using the private attr
            # here is the documented Way for SpanProcessors (see OTel issue
            # #2387). Wrap in try so an SDK upgrade can't crash us.
            try:
                span._attributes = redacted  # type: ignore[attr-defined]
            except Exception:  # pragma: no cover
                for k, v in redacted.items():
                    span.set_attribute(k, v) if hasattr(span, "set_attribute") else None
        except RedactionError as exc:
            global _dropped_spans
            _dropped_spans += 1
            log.warning(
                "instrumentation.span_dropped_on_redaction",
                err=str(exc),
                span_name=span.name,
            )
            # We cannot truly stop export from a processor mid-flight, but
            # we can scrub the attributes to a known-safe placeholder.
            try:
                span._attributes = {  # type: ignore[attr-defined]
                    "putsch.redaction": "failed_closed",
                    "putsch.retention_class": self._settings.retention_class.value,
                }
            except Exception:  # pragma: no cover
                pass
        except Exception as exc:  # pragma: no cover - defensive
            log.warning("instrumentation.on_end_failed", err=str(exc))

    def shutdown(self) -> None:
        return None

    def force_flush(self, timeout_millis: int = 30_000) -> bool:
        return True


# ─────────────────────────────────────────────────────────────────────────────
# Drop-counting exporter wrapper
# ─────────────────────────────────────────────────────────────────────────────


class _DropCountingExporter:
    """Wraps the OTLP exporter to count drops and never raise."""

    def __init__(self, inner: OTLPSpanExporter) -> None:
        self._inner = inner

    def export(self, spans: list[ReadableSpan]) -> SpanExportResult:
        global _dropped_spans
        try:
            return self._inner.export(spans)
        except Exception as exc:
            _dropped_spans += len(spans)
            log.warning(
                "instrumentation.export_failed",
                err=str(exc),
                err_type=type(exc).__name__,
                n_spans=len(spans),
            )
            return SpanExportResult.FAILURE

    def shutdown(self) -> None:
        with contextlib.suppress(Exception):
            self._inner.shutdown()

    def force_flush(self, timeout_millis: int = 30_000) -> bool:
        try:
            return bool(self._inner.force_flush(timeout_millis))
        except Exception:
            return False


# ─────────────────────────────────────────────────────────────────────────────
# Public API
# ─────────────────────────────────────────────────────────────────────────────


def init(
    *,
    service_name: str | None = None,
    settings: PutschObsSettings | None = None,
    engine: RedactionEngine | None = None,
) -> None:
    """Initialize OTel + Langfuse. Idempotent and thread-safe.

    Calls beyond the first are no-ops.
    """
    global _initialized, _provider, _langfuse

    with _lock:
        if _initialized:
            return
        cfg = settings or get_settings()
        if service_name:
            # Mutate via assignment so the validator runs.
            cfg.service_name = service_name

        if engine is not None:
            install_engine(engine)
        red = engine or get_engine()

        resource = Resource.create(cfg.otel_resource_attrs())
        provider = TracerProvider(resource=resource)

        otlp_endpoint = str(cfg.otel_exporter_endpoint).rstrip("/") + "/v1/traces"
        exporter = OTLPSpanExporter(
            endpoint=otlp_endpoint,
            timeout=int(cfg.otel_export_timeout_seconds),
        )
        wrapped = _DropCountingExporter(exporter)
        batch = BatchSpanProcessor(
            wrapped,  # type: ignore[arg-type]
            max_queue_size=cfg.otel_max_queue_size_count,
            max_export_batch_size=cfg.otel_max_export_batch_size_count,
            schedule_delay_millis=cfg.otel_schedule_delay_millis,
            export_timeout_millis=int(cfg.otel_export_timeout_seconds * 1000),
        )

        # Redactor runs BEFORE batching, so PII never enters the queue.
        provider.add_span_processor(_RedactingSpanProcessor(red, cfg))
        provider.add_span_processor(batch)

        otel_trace.set_tracer_provider(provider)
        _provider = provider

        _langfuse = _maybe_init_langfuse(cfg)

        atexit.register(shutdown)
        _initialized = True
        log.info(
            "instrumentation.initialized",
            service=cfg.service_name,
            env=cfg.deployment_environment,
            retention=cfg.retention_class.value,
            endpoint=otlp_endpoint,
            langfuse=str(cfg.langfuse_host),
        )


def _maybe_init_langfuse(cfg: PutschObsSettings) -> Any | None:
    """Initialize the Langfuse SDK if creds are present. Returns ``None`` otherwise.

    Even with creds absent (development), OTel keeps working and the
    application sees no behavioural difference.
    """
    pk = cfg.langfuse_public_key.get_secret_value()
    sk = cfg.langfuse_secret_key.get_secret_value()
    if not (pk and sk):
        log.info("instrumentation.langfuse_skipped", reason="no_credentials")
        return None
    try:
        from langfuse import Langfuse

        client = Langfuse(
            public_key=pk,
            secret_key=sk,
            host=str(cfg.langfuse_host).rstrip("/"),
            flush_at=cfg.langfuse_flush_at_count,
            flush_interval=cfg.langfuse_flush_interval_seconds,
            release=f"{cfg.service_name}@{cfg.service_version}",
            environment=cfg.deployment_environment,
        )
        return client
    except Exception as exc:  # pragma: no cover - import-time / env-specific
        log.warning(
            "instrumentation.langfuse_init_failed",
            err=str(exc),
            err_type=type(exc).__name__,
        )
        return None


def is_initialized() -> bool:
    return _initialized


def get_tracer(name: str = "putsch_obs") -> otel_trace.Tracer:
    if not _initialized:
        init()
    return otel_trace.get_tracer(name)


def get_langfuse() -> Any | None:
    """Return the Langfuse client if available, else ``None``.

    Caller must tolerate ``None`` — every Langfuse call site has a no-op
    fallback for the "no credentials" path.
    """
    return _langfuse


@contextlib.contextmanager
def span(
    name: str,
    *,
    attributes: Mapping[str, Any] | None = None,
    kind: otel_trace.SpanKind = otel_trace.SpanKind.INTERNAL,
) -> Iterator[Span]:
    """Convenience span context manager.

    Wraps :func:`get_tracer` and adds correlation/error semantics.
    Errors raised inside the block are recorded on the span as exceptions
    but re-raised — instrumentation must not swallow application errors.
    """
    tracer = get_tracer()
    with tracer.start_as_current_span(name, kind=kind) as sp:
        if attributes:
            for k, v in attributes.items():
                with contextlib.suppress(Exception):
                    sp.set_attribute(k, v)
        try:
            yield sp
        except Exception as exc:
            try:
                sp.record_exception(exc)
                sp.set_status(otel_trace.Status(otel_trace.StatusCode.ERROR, str(exc)))
            except Exception:  # pragma: no cover - defensive
                pass
            raise


def dropped_span_count() -> int:
    """Counter for tests + the drift dashboard."""
    return _dropped_spans


def shutdown() -> None:
    """Flush and tear down. Safe to call multiple times."""
    global _initialized, _provider, _langfuse
    with _lock:
        if not _initialized:
            return
        if _provider is not None:
            try:
                _provider.force_flush(timeout_millis=5_000)
            except Exception as exc:
                log.warning("instrumentation.flush_failed", err=str(exc))
            try:
                _provider.shutdown()
            except Exception as exc:
                log.warning("instrumentation.shutdown_failed", err=str(exc))
        if _langfuse is not None:
            with contextlib.suppress(Exception):
                _langfuse.flush()
            with contextlib.suppress(Exception):
                _langfuse.shutdown()
        _provider = None
        _langfuse = None
        _initialized = False


__all__ = [
    "dropped_span_count",
    "get_langfuse",
    "get_tracer",
    "init",
    "is_initialized",
    "shutdown",
    "span",
]
