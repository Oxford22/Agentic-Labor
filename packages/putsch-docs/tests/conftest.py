"""pytest fixtures and helpers.

Test design:
- Unit tests stub out Docling and the VLM client. They run in milliseconds
  and are the CI default.
- Integration tests (marked @pytest.mark.integration) hit the real Docling
  model and the local vLLM container; they run nightly.
- Performance tests (marked @pytest.mark.performance) enforce p99 budgets.
"""

from __future__ import annotations

import base64
import os
from collections.abc import Iterator
from datetime import date
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest

from putsch_docs.config import Settings, get_settings
from putsch_docs.signatures import InvoiceFields, InvoiceLineItem

# ----- environment isolation --------------------------------------------------------

# Block accidental network / Langfuse during unit tests
os.environ.setdefault("PUTSCH_DOCS_OBS__LANGFUSE_ENABLED", "false")
os.environ.setdefault("PUTSCH_DOCS_FALLBACK__ENABLED", "true")
os.environ.setdefault("PUTSCH_DOCS_LLM__API_KEY", "test-key")


@pytest.fixture(autouse=True)
def _clear_settings_cache() -> Iterator[None]:
    """Ensure each test sees a freshly-built Settings (env-driven)."""
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


@pytest.fixture
def fixtures_dir() -> Path:
    return Path(__file__).parent / "fixtures"


@pytest.fixture
def clean_xrechnung_path(fixtures_dir: Path) -> Path:
    return fixtures_dir / "clean_xrechnung.xml"


@pytest.fixture
def scanned_pdf_bytes(fixtures_dir: Path) -> bytes:
    return (fixtures_dir / "scanned_invoice.pdf").read_bytes()


@pytest.fixture
def multipage_table_bytes(fixtures_dir: Path) -> bytes:
    return (fixtures_dir / "multipage_tables.pdf").read_bytes()


@pytest.fixture
def handwritten_bytes(fixtures_dir: Path) -> bytes:
    return (fixtures_dir / "handwritten_annotation.pdf").read_bytes()


@pytest.fixture
def watermark_bytes(fixtures_dir: Path) -> bytes:
    return (fixtures_dir / "watermark_stamp.pdf").read_bytes()


# ----- canned valid InvoiceFields ---------------------------------------------------


@pytest.fixture
def canonical_invoice() -> InvoiceFields:
    """A spec-conformant German B2B invoice. Used as the golden in arithmetic /
    validator tests."""
    return InvoiceFields(
        rechnungsnummer="2026-04001",
        rechnungsdatum=date(2026, 4, 18),
        leistungsdatum=date(2026, 4, 15),
        lieferant_name="Schmidt Industrieteile GmbH",
        lieferant_ustid="DE129273398",
        lieferant_address="Industriestraße 14, 58095 Hagen",
        kunde_ustid="DE811184878",
        iban="DE89370400440532013000",
        bic="COBADEFFXXX",
        netto_betrag=Decimal("1000.00"),
        mwst_satz=Decimal("19.00"),
        mwst_betrag=Decimal("190.00"),
        brutto_betrag=Decimal("1190.00"),
        waehrung="EUR",
        zahlungsziel=30,
        skonto_prozent=Decimal("2.00"),
        skonto_frist=14,
        bestellnummer_ref="PO-2026-7781",
        lieferantennummer_ref="50012345",
        line_items=[
            InvoiceLineItem(
                position=1,
                material_nummer="M-4711",
                beschreibung="Zahnradgetriebe Typ A",
                menge=Decimal("10.0000"),
                einheit="STK",
                einzelpreis=Decimal("60.00"),
                gesamtpreis=Decimal("600.00"),
                mwst_satz=Decimal("19.00"),
            ),
            InvoiceLineItem(
                position=2,
                material_nummer="M-4712",
                beschreibung="Wellendichtring 30x42",
                menge=Decimal("4.0000"),
                einheit="STK",
                einzelpreis=Decimal("100.00"),
                gesamtpreis=Decimal("400.00"),
                mwst_satz=Decimal("19.00"),
            ),
        ],
    )


# ----- Docling stub -----------------------------------------------------------------


class _StubElement:
    def __init__(self, label: str, confidence: float) -> None:
        self.label = label
        self.confidence = confidence


class _StubPage:
    def __init__(self, elements: list[_StubElement]) -> None:
        self.elements = elements


class _StubDoclingDocument:
    """Quacks like docling_core.types.doc.DoclingDocument for our purposes."""

    def __init__(self, markdown: str, scores: dict[str, float]) -> None:
        self._markdown = markdown
        elements = [_StubElement(k, v) for k, v in scores.items()]
        self.pages = [_StubPage(elements)]

    def export_to_markdown(self) -> str:
        return self._markdown


class _StubConvertResult:
    def __init__(self, doc: _StubDoclingDocument) -> None:
        self.document = doc


class StubDoclingConverter:
    """Drop-in for docling.DocumentConverter in unit tests."""

    def __init__(
        self,
        *,
        markdown: str,
        region_scores: dict[str, float] | None = None,
        raise_on_convert: Exception | None = None,
    ) -> None:
        self._md = markdown
        self._scores = region_scores or {}
        self._raise = raise_on_convert

    def convert(self, _source: Any) -> _StubConvertResult:
        if self._raise:
            raise self._raise
        return _StubConvertResult(_StubDoclingDocument(self._md, self._scores))


# ----- VLM fallback stub ------------------------------------------------------------


class StubFallback:
    """Returns a pre-canned InvoiceFields or raises a pre-canned exception."""

    def __init__(
        self,
        *,
        invoice: InvoiceFields | None = None,
        raise_exc: Exception | None = None,
    ) -> None:
        self._invoice = invoice
        self._raise = raise_exc
        self.calls = 0

    async def extract(self, _source: Any) -> InvoiceFields:
        self.calls += 1
        if self._raise:
            raise self._raise
        assert self._invoice is not None
        return self._invoice


# ----- DSPy stub -------------------------------------------------------------------


class StubDSPyProgram:
    def __init__(self, *, invoice: InvoiceFields | None) -> None:
        self._invoice = invoice
        self.calls = 0

    def __call__(self, *, markdown: str) -> Any:  # noqa: ARG002
        self.calls += 1

        class _Pred:
            pass

        p = _Pred()
        p.invoice = self._invoice
        return p


@pytest.fixture
def b64() -> Any:
    return lambda b: base64.b64encode(b).decode("ascii")
