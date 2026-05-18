"""CrewAI tool wrapper.

Exposes the DoclingExtractor as a `BaseTool` so the AP Crew's
Match-Agent, Buchungs-Agent, and exception-router can invoke it via
their normal tool-calling loop.

The tool docstring is the agent's reasoning contract — write it for the
LLM, not for humans.
"""

from __future__ import annotations

import asyncio
import base64
from pathlib import Path
from typing import Any

from crewai.tools import BaseTool
from pydantic import BaseModel, ConfigDict, Field, model_validator

from putsch_docs.exceptions import ExtractionError
from putsch_docs.extractor import DoclingExtractor, ExtractionResult


class ExtractInvoiceInput(BaseModel):
    """Path-or-bytes input. Exactly one of `path` or `bytes_b64` is required.

    `bytes_b64` is base64 because CrewAI tool-call arguments must be JSON-safe;
    agents pass file contents pre-encoded.
    """

    model_config = ConfigDict(extra="forbid")

    path: Path | None = Field(default=None, description="Absolute path to the invoice file.")
    bytes_b64: str | None = Field(
        default=None,
        description="Base64-encoded file bytes. Used when the agent already has the document "
        "in memory (e.g. from a mail-fetch tool).",
    )
    document_id: str | None = Field(
        default=None,
        description="Correlation id to thread through observability. Omit to mint one.",
    )

    @model_validator(mode="after")
    def _exactly_one_source(self) -> ExtractInvoiceInput:
        if (self.path is None) == (self.bytes_b64 is None):
            msg = "exactly one of `path` or `bytes_b64` must be provided"
            raise ValueError(msg)
        return self

    def materialize(self) -> Path | bytes:
        if self.path is not None:
            return self.path
        assert self.bytes_b64 is not None
        try:
            return base64.b64decode(self.bytes_b64, validate=True)
        except (ValueError, TypeError) as exc:
            msg = "bytes_b64 is not valid base64"
            raise ValueError(msg) from exc


class ExtractInvoiceTool(BaseTool):
    """CrewAI tool: extract a German Eingangsrechnung into typed fields."""

    name: str = "extract_invoice"
    description: str = (
        "Extract structured fields from a German incoming invoice (Eingangsrechnung).\n"
        "\n"
        "Use this tool when you have a PDF, scanned image, or XRechnung XML of a single\n"
        "vendor invoice and need its fields as typed data (Rechnungsnummer,\n"
        "Rechnungsdatum, Lieferant USt-IdNr, IBAN, Netto/MwSt/Brutto amounts, line\n"
        "items, payment terms). The tool runs Docling (structural parser) first; for\n"
        "scanned or low-quality documents it automatically falls back to a vision\n"
        "model. Output is the InvoiceFields schema with a per-field confidence\n"
        "report. Validators (IBAN MOD-97, USt-IdNr format, arithmetic consistency)\n"
        "have already passed before the tool returns.\n"
        "\n"
        "Inputs (exactly one of path or bytes_b64):\n"
        "  path: absolute path to an invoice file.\n"
        "  bytes_b64: base64-encoded file contents.\n"
        "  document_id (optional): correlation id.\n"
        "\n"
        "Output: a JSON object {invoice, confidence, trace}.\n"
        "  invoice: the structured InvoiceFields.\n"
        "  confidence: per-field confidence report.\n"
        "  trace: provenance — which extraction path produced which field.\n"
        "\n"
        "Errors are returned as objects with `error_type` and `message`. Inspect\n"
        "`error_type`:\n"
        "  DoclingError / FallbackError → model-side failure.\n"
        "  ConfidenceError → both paths produced output but neither met the critical\n"
        "    threshold on at least one critical field. Route to the HITL queue with\n"
        "    the partial output attached.\n"
        "  FieldValidationError → IBAN, USt-IdNr, or arithmetic check failed."
    )

    args_schema: type[BaseModel] = ExtractInvoiceInput

    # CrewAI's BaseTool requires this attribute set; we inject the extractor.
    extractor: DoclingExtractor = Field(default_factory=DoclingExtractor, exclude=True)

    model_config = ConfigDict(arbitrary_types_allowed=True)

    def _run(self, **kwargs: Any) -> dict[str, Any]:
        """Sync entry point CrewAI uses. Drives the async extractor via asyncio.run."""
        return asyncio.run(self._arun(**kwargs))

    async def _arun(self, **kwargs: Any) -> dict[str, Any]:
        try:
            payload = ExtractInvoiceInput.model_validate(kwargs)
        except Exception as exc:  # noqa: BLE001 — CrewAI passes raw kwargs
            return {"error_type": "InputValidationError", "message": str(exc)}

        source = payload.materialize()
        try:
            result: ExtractionResult = await self.extractor.extract(
                source, document_id=payload.document_id
            )
        except ExtractionError as exc:
            return exc.to_dict()
        except Exception as exc:  # noqa: BLE001
            return {
                "error_type": "InternalError",
                "message": "unexpected failure in extractor",
                "cause_type": type(exc).__name__,
            }
        return result.model_dump(mode="json")


def build_default_tool() -> ExtractInvoiceTool:
    """Factory used by the Crew bootstrap. Lets the crew swap settings later."""
    return ExtractInvoiceTool()
