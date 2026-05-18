"""Docling instrumentation.

Docling + Granite-Docling is the document-layer entry point. Every PDF /
DOCX / scan that comes in produces a Docling extraction. We trace:

* page count
* OCR confidence (min / mean — per Docling's per-page scores)
* table count
* fallback triggers (PaddleOCR fallback, layout-only fallback)
* extraction latency
"""

from __future__ import annotations

import contextlib
import threading
from typing import Any

from opentelemetry import trace as otel_trace

from putsch_obs.instrumentation import get_tracer, is_initialized
from putsch_obs.integrations._base import StopWatch, safe
from putsch_obs.logging import get_logger

log = get_logger(__name__)

_INSTALLED = False
_LOCK = threading.Lock()


def install() -> None:
    """Monkey-patch ``DocumentConverter.convert``. Idempotent."""
    global _INSTALLED
    with _LOCK:
        if _INSTALLED:
            return
        try:
            from docling.document_converter import DocumentConverter  # type: ignore[import-not-found]
        except ImportError as exc:
            raise ImportError(
                "docling is not installed; pip install putsch-obs[integrations]"
            ) from exc

        if not is_initialized():
            from putsch_obs.instrumentation import init

            init()

        original = DocumentConverter.convert

        @safe("docling.convert")
        def traced(self: Any, source: Any, **kwargs: Any) -> Any:
            tracer = get_tracer("putsch_obs.docling")
            with tracer.start_as_current_span(
                "docling.convert",
                kind=otel_trace.SpanKind.INTERNAL,
            ) as sp:
                sp.set_attribute("putsch.kind", "extraction")
                sp.set_attribute("docling.source.type", type(source).__name__)
                src_name = getattr(source, "name", None) or str(source)
                sp.set_attribute("docling.source.name", str(src_name)[:256])
                watch = StopWatch()
                try:
                    result = original(self, source, **kwargs)
                except Exception as exc:
                    sp.record_exception(exc)
                    sp.set_status(otel_trace.Status(otel_trace.StatusCode.ERROR, str(exc)))
                    sp.set_attribute("putsch.latency_ms", watch.elapsed_ms())
                    raise
                sp.set_attribute("putsch.latency_ms", watch.elapsed_ms())
                _annotate_result(sp, result)
                return result

        DocumentConverter.convert = traced  # type: ignore[method-assign]
        _INSTALLED = True
        log.info("docling.instrumentation_installed")


def _annotate_result(sp: Any, result: Any) -> None:
    """Pull the fields Docling exposes; tolerate missing ones (API drift)."""
    with contextlib.suppress(Exception):
        doc = getattr(result, "document", None) or result
        pages = getattr(doc, "pages", None)
        if pages is not None:
            sp.set_attribute("docling.page_count", len(pages))
            conf = _ocr_confidence(pages)
            if conf is not None:
                sp.set_attribute("docling.ocr.confidence_min", conf[0])
                sp.set_attribute("docling.ocr.confidence_mean", conf[1])
        tables = getattr(doc, "tables", None)
        if tables is not None:
            sp.set_attribute("docling.table_count", len(tables))
        # Fallback markers: Granite uses `metadata.fallback_chain`.
        meta = getattr(doc, "metadata", None) or {}
        if isinstance(meta, dict):
            fb = meta.get("fallback_chain")
            if fb:
                sp.set_attribute("docling.fallback_chain", ",".join(map(str, fb)))


def _ocr_confidence(pages: Any) -> tuple[float, float] | None:
    confs: list[float] = []
    for page in pages:
        c = getattr(page, "ocr_confidence", None)
        if c is None and isinstance(page, dict):
            c = page.get("ocr_confidence")
        if c is not None:
            with contextlib.suppress(Exception):
                confs.append(float(c))
    if not confs:
        return None
    return min(confs), sum(confs) / len(confs)


__all__ = ["install"]
