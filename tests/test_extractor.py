"""End-to-end extractor pipeline tests with stubs.

Real Docling + real vLLM are tested in integration tests (marked
@pytest.mark.integration), nightly only.
"""

from __future__ import annotations

from typing import Any

import pytest

from putsch_docs.exceptions import (
    ConfidenceError,
    DoclingError,
    FallbackError,
)
from putsch_docs.extractor import DoclingExtractor
from putsch_docs.signatures import InvoiceFields

from .conftest import StubDoclingConverter, StubDSPyProgram, StubFallback


def _build_extractor(
    *,
    converter: Any,
    fallback: StubFallback | None,
    dspy_program: StubDSPyProgram | None = None,
) -> DoclingExtractor:
    ext = DoclingExtractor(converter=converter, fallback=fallback)
    if dspy_program is not None:
        ext._extractor_program = dspy_program  # noqa: SLF001 — test wiring
    # Disable judge to keep tests deterministic
    ext.settings.confidence = ext.settings.confidence.model_copy(
        update={"judge_critical_fields_always": False}
    )
    return ext


@pytest.mark.asyncio
async def test_happy_path_docling_only(canonical_invoice: InvoiceFields) -> None:
    converter = StubDoclingConverter(
        markdown="# Rechnung\n...",
        region_scores={
            "invoice-number": 0.97,
            "vat-id": 0.95,
            "iban": 0.98,
            "net-total": 0.96,
            "vat-amount": 0.96,
            "gross-total": 0.96,
            "table": 0.93,
        },
    )
    ext = _build_extractor(
        converter=converter,
        fallback=None,
        dspy_program=StubDSPyProgram(invoice=canonical_invoice),
    )

    result = await ext.extract(b"%PDF-fake")
    assert result.invoice == canonical_invoice
    assert result.trace.primary_path == "docling"
    assert result.trace.fallback_invoked is False
    assert result.confidence.overall_min >= 0.85


@pytest.mark.asyncio
async def test_fallback_triggered_when_docling_validation_fails(
    canonical_invoice: InvoiceFields,
) -> None:
    converter = StubDoclingConverter(markdown="garbage", region_scores={})
    # DSPy returns None — i.e. the LLM coercion failed to validate
    fb = StubFallback(invoice=canonical_invoice)
    ext = _build_extractor(
        converter=converter,
        fallback=fb,
        dspy_program=StubDSPyProgram(invoice=None),
    )
    result = await ext.extract(b"%PDF-fake")
    assert fb.calls == 1
    assert result.trace.fallback_invoked is True
    assert result.trace.fallback_reason == "validation_error"
    assert result.invoice == canonical_invoice


@pytest.mark.asyncio
async def test_fallback_triggered_when_critical_field_low(
    canonical_invoice: InvoiceFields,
) -> None:
    # Region scores intentionally low for the rechnungsnummer region only
    converter = StubDoclingConverter(
        markdown="# Rechnung",
        region_scores={
            "invoice-number": 0.40,
            "vat-id": 0.95,
            "iban": 0.98,
            "net-total": 0.97,
        },
    )
    fb = StubFallback(invoice=canonical_invoice)
    ext = _build_extractor(
        converter=converter,
        fallback=fb,
        dspy_program=StubDSPyProgram(invoice=canonical_invoice),
    )
    result = await ext.extract(b"%PDF-fake")
    assert fb.calls == 1
    assert result.trace.fallback_invoked is True
    assert result.trace.fallback_reason == "low_confidence_critical_field"


@pytest.mark.asyncio
async def test_docling_raises_and_fallback_recovers(
    canonical_invoice: InvoiceFields,
) -> None:
    converter = StubDoclingConverter(
        markdown="", raise_on_convert=RuntimeError("docling boom")
    )
    fb = StubFallback(invoice=canonical_invoice)
    ext = _build_extractor(converter=converter, fallback=fb)
    result = await ext.extract(b"%PDF-fake")
    assert result.invoice == canonical_invoice
    assert result.trace.primary_path == "fallback"
    assert result.trace.fallback_reason == "docling_failed"


@pytest.mark.asyncio
async def test_both_paths_fail_raises_confidence_error(
    canonical_invoice: InvoiceFields,
) -> None:
    converter = StubDoclingConverter(markdown="empty", region_scores={"invoice-number": 0.20})
    fb = StubFallback(raise_exc=FallbackError("vllm down"))
    ext = _build_extractor(
        converter=converter,
        fallback=fb,
        dspy_program=StubDSPyProgram(invoice=canonical_invoice),
    )
    with pytest.raises(ConfidenceError) as exc_info:
        await ext.extract(b"%PDF-fake")
    payload = exc_info.value.to_dict()
    assert "confidence_report" in payload
    assert exc_info.value.docling_partial is not None


@pytest.mark.asyncio
async def test_fallback_disabled_propagates_docling_error() -> None:
    converter = StubDoclingConverter(
        markdown="", raise_on_convert=RuntimeError("docling boom")
    )
    ext = _build_extractor(converter=converter, fallback=None)
    ext.settings.fallback = ext.settings.fallback.model_copy(update={"enabled": False})

    with pytest.raises(DoclingError):
        await ext.extract(b"%PDF-fake")


@pytest.mark.asyncio
async def test_correlation_id_propagates(canonical_invoice: InvoiceFields) -> None:
    converter = StubDoclingConverter(
        markdown="# Rechnung",
        region_scores={
            "invoice-number": 0.97,
            "vat-id": 0.95,
            "iban": 0.98,
            "net-total": 0.96,
            "vat-amount": 0.96,
            "gross-total": 0.96,
        },
    )
    ext = _build_extractor(
        converter=converter,
        fallback=None,
        dspy_program=StubDSPyProgram(invoice=canonical_invoice),
    )
    result = await ext.extract(b"%PDF-fake", document_id="doc-abc-123", run_id="run-xyz")
    assert result.trace.document_id == "doc-abc-123"
    assert result.trace.run_id == "run-xyz"
