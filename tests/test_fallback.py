"""Fallback VLM client tests.

We stub the HTTP transport with respx — no actual vLLM needed. These tests
exercise the retry, circuit breaker, JSON parsing, and schema-validation
paths.
"""

from __future__ import annotations

import json
from decimal import Decimal
from typing import Any

import httpx
import pytest
import respx

from putsch_docs.config import FallbackSettings
from putsch_docs.exceptions import FallbackError
from putsch_docs.fallback import QwenVLFallback


@pytest.fixture
def vllm_settings() -> FallbackSettings:
    return FallbackSettings(
        enabled=True,
        endpoint="http://stub-vllm:8000/v1",
        max_retries=2,
        breaker_fail_max=3,
        timeout_seconds=5.0,
    )


VALID_INVOICE_JSON: dict[str, Any] = {
    "rechnungsnummer": "VLM-2026-1",
    "rechnungsdatum": "2026-04-18",
    "leistungsdatum": "2026-04-15",
    "lieferant_name": "Test GmbH",
    "lieferant_ustid": "DE129273398",
    "lieferant_address": "Musterstraße 1, 58095 Hagen",
    "kunde_ustid": "DE811184878",
    "iban": "DE89370400440532013000",
    "bic": "COBADEFFXXX",
    "netto_betrag": "100.00",
    "mwst_satz": "19.00",
    "mwst_betrag": "19.00",
    "brutto_betrag": "119.00",
    "waehrung": "EUR",
    "zahlungsziel": 30,
    "skonto_prozent": None,
    "skonto_frist": None,
    "bestellnummer_ref": None,
    "lieferantennummer_ref": None,
    "line_items": [
        {
            "position": 1,
            "material_nummer": "M-1",
            "beschreibung": "Test",
            "menge": "1.00",
            "einheit": "STK",
            "einzelpreis": "100.00",
            "gesamtpreis": "100.00",
            "mwst_satz": "19.00",
        }
    ],
}


@pytest.fixture
def fake_pdf_bytes() -> bytes:
    """Minimal valid 1-page PDF. pypdfium2 will rasterize it."""
    return _minimal_pdf()


def _minimal_pdf() -> bytes:
    # Hand-rolled minimal PDF — single blank page, valid xref. Just enough
    # for pypdfium2 to render. Copy of the canonical "smallest PDF" example.
    return (
        b"%PDF-1.4\n"
        b"1 0 obj<</Type/Catalog/Pages 2 0 R>>endobj\n"
        b"2 0 obj<</Type/Pages/Count 1/Kids[3 0 R]>>endobj\n"
        b"3 0 obj<</Type/Page/Parent 2 0 R/MediaBox[0 0 200 200]"
        b"/Resources<<>>/Contents 4 0 R>>endobj\n"
        b"4 0 obj<</Length 8>>stream\nBT ET\n\nendstream endobj\n"
        b"xref\n0 5\n0000000000 65535 f \n"
        b"0000000009 00000 n \n"
        b"0000000052 00000 n \n"
        b"0000000098 00000 n \n"
        b"0000000176 00000 n \n"
        b"trailer<</Size 5/Root 1 0 R>>\nstartxref\n232\n%%EOF\n"
    )


@pytest.mark.asyncio
async def test_happy_path_returns_invoice(
    vllm_settings: FallbackSettings, fake_pdf_bytes: bytes
) -> None:
    with respx.mock(base_url=str(vllm_settings.endpoint)) as mock:
        mock.post("/chat/completions").mock(
            return_value=httpx.Response(
                200,
                json={
                    "choices": [
                        {"message": {"content": json.dumps(VALID_INVOICE_JSON)}}
                    ]
                },
            )
        )
        client = QwenVLFallback(vllm_settings)
        try:
            inv = await client.extract(fake_pdf_bytes)
        finally:
            await client.aclose()
        assert inv.rechnungsnummer == "VLM-2026-1"
        assert inv.netto_betrag == Decimal("100.00")


@pytest.mark.asyncio
async def test_retries_on_5xx_then_succeeds(
    vllm_settings: FallbackSettings, fake_pdf_bytes: bytes
) -> None:
    with respx.mock(base_url=str(vllm_settings.endpoint)) as mock:
        route = mock.post("/chat/completions")
        route.side_effect = [
            httpx.Response(503, text="upstream down"),
            httpx.Response(
                200,
                json={
                    "choices": [
                        {"message": {"content": json.dumps(VALID_INVOICE_JSON)}}
                    ]
                },
            ),
        ]
        client = QwenVLFallback(vllm_settings)
        try:
            inv = await client.extract(fake_pdf_bytes)
        finally:
            await client.aclose()
        assert inv.rechnungsnummer == "VLM-2026-1"
        assert route.call_count == 2


@pytest.mark.asyncio
async def test_4xx_does_not_retry(
    vllm_settings: FallbackSettings, fake_pdf_bytes: bytes
) -> None:
    with respx.mock(base_url=str(vllm_settings.endpoint)) as mock:
        route = mock.post("/chat/completions").mock(
            return_value=httpx.Response(400, text="bad request")
        )
        client = QwenVLFallback(vllm_settings)
        try:
            with pytest.raises(FallbackError):
                await client.extract(fake_pdf_bytes)
        finally:
            await client.aclose()
        assert route.call_count == 1  # no retry on client error


@pytest.mark.asyncio
async def test_invalid_json_raises_fallback_error(
    vllm_settings: FallbackSettings, fake_pdf_bytes: bytes
) -> None:
    with respx.mock(base_url=str(vllm_settings.endpoint)) as mock:
        mock.post("/chat/completions").mock(
            return_value=httpx.Response(
                200,
                json={"choices": [{"message": {"content": "not json"}}]},
            )
        )
        client = QwenVLFallback(vllm_settings)
        try:
            with pytest.raises(FallbackError, match="non-JSON"):
                await client.extract(fake_pdf_bytes)
        finally:
            await client.aclose()


@pytest.mark.asyncio
async def test_schema_violation_raises_fallback_error(
    vllm_settings: FallbackSettings, fake_pdf_bytes: bytes
) -> None:
    bad_payload = {**VALID_INVOICE_JSON, "iban": "not-an-iban"}
    with respx.mock(base_url=str(vllm_settings.endpoint)) as mock:
        mock.post("/chat/completions").mock(
            return_value=httpx.Response(
                200,
                json={"choices": [{"message": {"content": json.dumps(bad_payload)}}]},
            )
        )
        client = QwenVLFallback(vllm_settings)
        try:
            with pytest.raises(FallbackError, match="failed InvoiceFields validation"):
                await client.extract(fake_pdf_bytes)
        finally:
            await client.aclose()


@pytest.mark.asyncio
async def test_disabled_raises(vllm_settings: FallbackSettings, fake_pdf_bytes: bytes) -> None:
    disabled = vllm_settings.model_copy(update={"enabled": False})
    client = QwenVLFallback(disabled)
    try:
        with pytest.raises(FallbackError, match="fallback disabled"):
            await client.extract(fake_pdf_bytes)
    finally:
        await client.aclose()
