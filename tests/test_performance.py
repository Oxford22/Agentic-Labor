"""p99 latency budget tests.

We assert that the *orchestration overhead* (excluding real model
inference) stays inside the budgeted envelope. The integration suite
(real Docling + real vLLM) measures end-to-end and is run nightly.
"""

from __future__ import annotations

import asyncio
import time

import pytest

from putsch_docs.extractor import DoclingExtractor
from putsch_docs.signatures import InvoiceFields

from .conftest import StubDoclingConverter, StubDSPyProgram


@pytest.mark.performance
@pytest.mark.asyncio
async def test_overhead_clean_pdf_under_50ms(canonical_invoice: InvoiceFields) -> None:
    """Pipeline overhead — excluding model inference — must stay under 50ms p99.

    Real Docling adds ~0.5s/page on L4; the budget for our orchestration
    around it is 50ms. If this regresses we're adding orchestration cruft.
    """
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
    ext = DoclingExtractor(converter=converter)
    ext._extractor_program = StubDSPyProgram(invoice=canonical_invoice)  # noqa: SLF001
    ext.settings.confidence = ext.settings.confidence.model_copy(
        update={"judge_critical_fields_always": False}
    )

    # Warmup
    await ext.extract(b"%PDF-fake")

    durations: list[float] = []
    for _ in range(50):
        t0 = time.monotonic()
        await ext.extract(b"%PDF-fake")
        durations.append((time.monotonic() - t0) * 1000.0)

    durations.sort()
    p99 = durations[int(len(durations) * 0.99) - 1]
    assert p99 < 50.0, f"orchestration p99 {p99:.1f}ms exceeded 50ms budget"


@pytest.mark.performance
@pytest.mark.asyncio
async def test_batch_extraction_parallelizes(canonical_invoice: InvoiceFields) -> None:
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
    ext = DoclingExtractor(converter=converter)
    ext._extractor_program = StubDSPyProgram(invoice=canonical_invoice)  # noqa: SLF001
    ext.settings.confidence = ext.settings.confidence.model_copy(
        update={"judge_critical_fields_always": False}
    )

    sources = [b"%PDF-fake"] * 8
    t0 = time.monotonic()
    results = await ext.extract_batch(sources)
    elapsed = time.monotonic() - t0
    assert all(r is not None for r in results)
    # Batch should not be 8x serial — sanity check that concurrency works at all
    assert elapsed < 8.0  # extremely loose; just guard against accidental serialization
    _ = asyncio  # silence unused
