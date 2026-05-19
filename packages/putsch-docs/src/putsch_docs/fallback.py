"""Qwen2.5-VL-72B fallback pipeline.

Why this exists:
    Docling is a structural parser; Qwen-VL is a vision-language reasoner.
    Their failure modes are uncorrelated — Docling stumbles on
    handwritten annotations, low-DPI scans, and unusual stamp placement,
    while Qwen-VL stumbles on dense numerical tables. Stacking them
    gives a near-complementary error surface.

    The fallback is not "try harder" with the same model — it's a
    fundamentally different architecture applied to the same input.
    That's the bet.

Implementation choices:
- vLLM endpoint behind LiteLLM (model swap = config change).
- Async client. Page rasters sent as base64 in OpenAI-compatible
  multimodal messages. We respect `max_pages_per_request` to keep
  72B vision context tractable.
- Tenacity retries on transient errors, purgatory circuit breaker on
  repeated failures (so a sustained vLLM outage degrades gracefully
  instead of melting the AP Crew's latency budget).
- Output is the SAME InvoiceFields schema. Callers cannot tell which
  path produced the result — but the ExtractionTrace records it.
"""

from __future__ import annotations

import base64
import io
import json
from pathlib import Path
from typing import Any

import httpx
import pypdfium2 as pdfium
from PIL import Image
from purgatory import AsyncCircuitBreakerFactory
from purgatory.domain.model import OpenedState
from pydantic import ValidationError
from tenacity import (
    AsyncRetrying,
    RetryError,
    retry_if_exception_type,
    stop_after_attempt,
    wait_exponential,
)

from putsch_docs.config import FallbackSettings, get_settings
from putsch_docs.exceptions import FallbackError
from putsch_docs.observability import current_document_id, get_logger
from putsch_docs.signatures import InvoiceFields

log = get_logger(__name__)

# Single, process-wide circuit breaker factory. Per-endpoint context separates
# breakers across distinct vLLM upstreams if we add a pool later.
_BREAKER_FACTORY = AsyncCircuitBreakerFactory()


# ----- VLM prompt --------------------------------------------------------------------

_VLM_SYSTEM_PROMPT = (
    "You are an expert at extracting structured data from German B2B incoming "
    "invoices (Eingangsrechnungen). You receive one full invoice as page images "
    "and must produce a single JSON object matching the InvoiceFields schema. "
    "Rules:\n"
    "- Parse German number format: '.' = thousands separator, ',' = decimal. "
    "'1.234,56' becomes the number 1234.56. Output monetary values as JSON "
    "strings with two decimal places (e.g., '1234.56').\n"
    "- Parse dates from DD.MM.YYYY to ISO YYYY-MM-DD strings.\n"
    "- USt-IdNr keeps its country prefix and has no internal spaces.\n"
    "- IBAN keeps its country prefix and has no spaces.\n"
    "- Capture every positional line item. Each line's einzelpreis * menge "
    "must equal its gesamtpreis.\n"
    "- Use null for fields genuinely absent from the document. Do not "
    "hallucinate USt-IdNr, IBAN, Lieferantennummer, or Bestellnummer.\n"
    "- mwst_satz at the invoice level is the headline rate. Per-line rate "
    "may differ on mixed-rate invoices.\n"
    "- Return ONLY the JSON object. No prose, no markdown fences."
)


# ----- Image preparation -------------------------------------------------------------


def _render_pdf_pages(
    source: Path | bytes,
    *,
    max_pages: int,
    dpi: int,
    jpeg_quality: int,
) -> list[bytes]:
    """Rasterize PDF pages to JPEG bytes for the VLM.

    For non-PDF inputs (image bytes), wraps the input as a single page.
    """
    if isinstance(source, bytes) and not source[:5] in (b"%PDF-", b"%PDF"):
        # Already an image
        img = Image.open(io.BytesIO(source)).convert("RGB")
        buf = io.BytesIO()
        img.save(buf, format="JPEG", quality=jpeg_quality, optimize=True)
        return [buf.getvalue()]

    pdf = pdfium.PdfDocument(source if isinstance(source, bytes) else str(source))
    out: list[bytes] = []
    scale = dpi / 72.0
    page_count = min(len(pdf), max_pages)
    for i in range(page_count):
        page = pdf[i]
        bitmap = page.render(scale=scale, rotation=0)
        pil = bitmap.to_pil().convert("RGB")
        buf = io.BytesIO()
        pil.save(buf, format="JPEG", quality=jpeg_quality, optimize=True)
        out.append(buf.getvalue())
    return out


def _to_data_url(jpeg_bytes: bytes) -> str:
    b64 = base64.b64encode(jpeg_bytes).decode("ascii")
    return f"data:image/jpeg;base64,{b64}"


# ----- Client ------------------------------------------------------------------------


class QwenVLFallback:
    """Async vLLM client speaking OpenAI-compatible multimodal chat completions."""

    def __init__(
        self,
        settings: FallbackSettings | None = None,
        *,
        http_client: httpx.AsyncClient | None = None,
    ) -> None:
        self.settings = settings or get_settings().fallback
        self._owns_client = http_client is None
        self._client = http_client or httpx.AsyncClient(
            timeout=self.settings.timeout_seconds,
            base_url=str(self.settings.endpoint),
            headers={
                "Authorization": f"Bearer {self.settings.api_key.get_secret_value()}",
                "Content-Type": "application/json",
            },
        )

    async def aclose(self) -> None:
        if self._owns_client:
            await self._client.aclose()

    async def __aenter__(self) -> QwenVLFallback:
        return self

    async def __aexit__(self, *_: object) -> None:
        await self.aclose()

    # ---------- public ----------

    async def extract(self, source: Path | bytes) -> InvoiceFields:
        """Run the VLM extraction. Raises FallbackError on any failure."""
        if not self.settings.enabled:
            raise FallbackError(
                "fallback disabled in config; cannot invoke VLM",
                document_id=current_document_id(),
            )

        log.info(
            "fallback.invoke",
            model=self.settings.model_id,
            endpoint=str(self.settings.endpoint),
        )

        try:
            page_images = _render_pdf_pages(
                source,
                max_pages=self.settings.max_pages_per_request,
                dpi=144,
                jpeg_quality=self.settings.image_jpeg_quality,
            )
        except Exception as exc:
            raise FallbackError(
                "failed to rasterize document for VLM",
                document_id=current_document_id(),
                cause=exc,
            ) from exc

        if not page_images:
            raise FallbackError(
                "no pages to send to VLM",
                document_id=current_document_id(),
            )

        try:
            raw = await self._call_vllm_with_breaker(page_images)
        except FallbackError:
            raise
        except Exception as exc:
            raise FallbackError(
                "vLLM call failed after retries",
                document_id=current_document_id(),
                cause=exc,
            ) from exc

        return self._parse_response(raw)

    # ---------- transport ----------

    async def _call_vllm_with_breaker(self, page_images: list[bytes]) -> str:
        breaker = await _BREAKER_FACTORY.get_breaker(
            circuit=f"vllm:{self.settings.model_id}",
            threshold=self.settings.breaker_fail_max,
            ttl=float(self.settings.breaker_reset_seconds),
        )
        try:
            async with breaker:
                return await self._call_vllm_retry(page_images)
        except OpenedState as exc:
            raise FallbackError(
                "vLLM circuit breaker OPEN — sustained upstream failure",
                document_id=current_document_id(),
                cause=exc,
            ) from exc

    async def _call_vllm_retry(self, page_images: list[bytes]) -> str:
        retryer = AsyncRetrying(
            stop=stop_after_attempt(self.settings.max_retries + 1),
            wait=wait_exponential(multiplier=1, min=1, max=10),
            retry=retry_if_exception_type(
                (httpx.HTTPError, httpx.TimeoutException, _TransientVLLMError)
            ),
            reraise=True,
        )
        try:
            async for attempt in retryer:
                with attempt:
                    return await self._call_vllm(page_images)
        except RetryError as exc:  # pragma: no cover — defensive
            raise FallbackError(
                "vLLM exhausted retries",
                document_id=current_document_id(),
                cause=exc,
            ) from exc
        raise FallbackError(  # unreachable
            "retry loop returned without value",
            document_id=current_document_id(),
        )

    async def _call_vllm(self, page_images: list[bytes]) -> str:
        content: list[dict[str, Any]] = [
            {
                "type": "text",
                "text": (
                    "Extract this German invoice into the InvoiceFields JSON object."
                ),
            }
        ]
        content.extend(
            {"type": "image_url", "image_url": {"url": _to_data_url(img)}}
            for img in page_images
        )

        body = {
            "model": self.settings.model_id,
            "messages": [
                {"role": "system", "content": _VLM_SYSTEM_PROMPT},
                {"role": "user", "content": content},
            ],
            "temperature": 0.0,
            "max_tokens": 4096,
            "response_format": {"type": "json_object"},
        }

        resp = await self._client.post("/chat/completions", json=body)
        if resp.status_code >= 500:
            raise _TransientVLLMError(f"upstream {resp.status_code}: {resp.text[:200]}")
        if resp.status_code >= 400:
            raise FallbackError(
                f"vLLM rejected request: {resp.status_code}",
                document_id=current_document_id(),
                cause=httpx.HTTPStatusError(
                    "client error", request=resp.request, response=resp
                ),
            )
        payload = resp.json()
        try:
            return str(payload["choices"][0]["message"]["content"])
        except (KeyError, IndexError, TypeError) as exc:
            raise FallbackError(
                "malformed vLLM response payload",
                document_id=current_document_id(),
                cause=exc,
            ) from exc

    # ---------- decoding ----------

    def _parse_response(self, raw: str) -> InvoiceFields:
        try:
            data = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise FallbackError(
                "vLLM returned non-JSON content",
                document_id=current_document_id(),
                cause=exc,
            ) from exc

        try:
            return InvoiceFields.model_validate(data)
        except ValidationError as exc:
            raise FallbackError(
                "vLLM output failed InvoiceFields validation",
                document_id=current_document_id(),
                partial=data if isinstance(data, dict) else None,
                cause=exc,
            ) from exc


class _TransientVLLMError(Exception):
    """Marker for retryable upstream errors (5xx, timeouts)."""
