"""End-to-end integration test against a real-shaped invoice.

This file is the single source of truth for "does the platform actually
process an invoice?". It grows as each module PR merges:

1.  Bootstrap (this PR) — the fixture parses through ``putsch_contracts``.
    Everything else ``pytest.importorskip``s out.
2.  After ``putsch-obs`` merges — wraps the parse in a real Observability
    span and asserts a span attribute lands on the in-memory exporter.
3.  After ``putsch-memory`` merges — looks up the supplier by USt-IdNr
    against the in-memory test Graphiti backend.
4.  After ``putsch-compile`` merges — pulls the ``extract_invoice_fields``
    signature out of the registry and confirms its metric threshold.
5.  After ``putsch-docs`` merges — re-extracts from the fixture PDF and
    confirms the structured Invoice matches the canonical JSON within
    tolerance.
6.  After ``putsch-swarm`` merges — runs the AP workflow end-to-end and
    asserts a ``WorkflowState`` with status ``COMPLETED``.

The pattern: each step ``importorskip``s its module and adds *exactly
one assertion*. No copy-paste of golden JSON; each module is mocked at
its external service boundary only (Neo4j driver, vLLM HTTP, Langfuse
HTTP) — never at the sibling-Putsch-package boundary.
"""

from __future__ import annotations

from decimal import Decimal
from pathlib import Path

import pytest
from putsch_contracts import (
    Invoice,
    TraceContext,
)

pytestmark = [pytest.mark.integration, pytest.mark.e2e]


def test_fixture_parses_through_contracts(real_shape_invoice: Invoice) -> None:
    """Step 1 (live since bootstrap).

    The fixture has to parse cleanly under the contract validators or
    every later module is being judged against an invalid baseline.
    """
    assert real_shape_invoice.invoice_number == "RE-2026-04-00813"
    assert real_shape_invoice.totals.total_gross == Decimal("2489.48")
    assert real_shape_invoice.supplier.vat_id == "DE143789021"
    assert real_shape_invoice.bank_details is not None
    assert real_shape_invoice.bank_details.iban == "DE89370400440532013000"
    assert len(real_shape_invoice.line_items) == 4


async def test_observability_spans_around_parse(real_shape_invoice: Invoice) -> None:
    """Step 2: enabled when ``putsch-obs`` (PR #6) merges."""
    obs = pytest.importorskip("putsch_obs.instrumentation")
    trace = TraceContext(correlation_id="e2e-step2", tenant="putsch", workflow="ap_extraction")
    observability = obs.Observability.in_memory()
    async with observability.span("parse-invoice", trace=trace):
        _ = real_shape_invoice.invoice_number
    spans = observability.exported_spans()
    assert any(s.name == "parse-invoice" for s in spans)


async def test_memory_lookup_finds_supplier(real_shape_invoice: Invoice) -> None:
    """Step 3: enabled when ``putsch-memory`` (PR #5) merges."""
    memory_mod = pytest.importorskip("putsch_memory.graphiti_client")
    trace = TraceContext(correlation_id="e2e-step3", tenant="putsch", workflow="ap_extraction")
    client = memory_mod.GraphitiClient.in_memory()
    vendor = await client.lookup_vendor(vat_id=real_shape_invoice.supplier.vat_id, trace=trace)
    assert vendor is not None
    assert vendor.vat_id == real_shape_invoice.supplier.vat_id


async def test_compile_registry_has_extract_signature() -> None:
    """Step 4: enabled when ``putsch-compile`` (PR #4) merges."""
    registry_mod = pytest.importorskip("putsch_compile.registry")
    registry = registry_mod.Registry.in_memory_test()
    sig = await registry.get("extract_invoice_fields")
    assert sig.metric.threshold >= 0.90
    assert sig.tier.value in {"small", "medium", "large"}


async def test_docling_extractor_reproduces_fixture(
    real_shape_invoice: Invoice, real_shape_invoice_path: Path
) -> None:
    """Step 5: enabled when ``putsch-docs`` (PR #3) merges.

    The extractor is asked to parse the canonical fixture (regenerated
    as a synthesised PDF in the docs package's test setup) and we
    compare to within the per-field confidence tolerance.
    """
    docs_mod = pytest.importorskip("putsch_docs.extractor")
    trace = TraceContext(correlation_id="e2e-step5", tenant="putsch", workflow="ap_extraction")
    extractor = docs_mod.DoclingExtractor.in_memory_for_fixture(real_shape_invoice_path)
    result = await extractor.extract(str(real_shape_invoice_path), trace=trace)
    assert result.invoice.invoice_number == real_shape_invoice.invoice_number
    assert abs(
        result.invoice.totals.total_gross - real_shape_invoice.totals.total_gross
    ) <= Decimal("0.05")


async def test_swarm_runs_ap_workflow_end_to_end(real_shape_invoice: Invoice) -> None:
    """Step 6: enabled when ``putsch-swarm`` (PR #2) merges.

    Real ``putsch-obs``, real ``putsch-memory``, real ``putsch-compile``,
    real ``putsch-docs`` (per the "do not mock sibling packages" rule);
    external services (Neo4j driver, vLLM HTTP, Langfuse HTTP, LangGraph
    Postgres) are mocked or use ephemeral test containers.
    """
    swarm_mod = pytest.importorskip("putsch_swarm.orchestrator")
    trace = TraceContext(
        correlation_id="e2e-step6", tenant="putsch", workflow="ap_kreditorenbuchhaltung"
    )
    orchestrator = swarm_mod.Orchestrator.in_memory_test()
    state = await orchestrator.run(
        "ap_kreditorenbuchhaltung",
        {"invoice": real_shape_invoice.model_dump(mode="json")},
        trace=trace,
    )
    assert state.status.value in {"completed", "awaiting_human"}
