"""Typed exception hierarchy for the document layer.

The AP Crew's exception router does the deciding. Extractors never silently
downgrade — every failure mode is a distinct, payload-carrying exception.
"""

from __future__ import annotations

from typing import Any


class ExtractionError(Exception):
    """Base class for all extraction failures.

    Always carries:
    - document_id: correlation id back to the calling Crew run
    - partial: any partial structured data the path produced (may be None)
    - cause: the underlying exception, if wrapping
    """

    def __init__(
        self,
        message: str,
        *,
        document_id: str | None = None,
        partial: dict[str, Any] | None = None,
        cause: BaseException | None = None,
    ) -> None:
        super().__init__(message)
        self.document_id = document_id
        self.partial = partial
        self.cause = cause

    def to_dict(self) -> dict[str, Any]:
        """Serialize for structured logs / Langfuse error span attributes."""
        return {
            "error_type": type(self).__name__,
            "message": str(self),
            "document_id": self.document_id,
            "has_partial": self.partial is not None,
            "cause_type": type(self.cause).__name__ if self.cause else None,
        }


class DoclingError(ExtractionError):
    """Docling DocumentConverter failed.

    Common causes: unsupported MIME, corrupt PDF, model load failure,
    timeout. Triggers fallback if fallback is enabled.
    """


class FallbackError(ExtractionError):
    """Qwen-VL fallback path failed.

    Causes: vLLM circuit open, timeout, malformed JSON output, schema validation
    failure on the VLM response. If raised together with a DoclingError, the
    caller has no usable extraction.
    """


class ConfidenceError(ExtractionError):
    """Both paths completed but per-field confidence is below the critical threshold.

    Payload includes both paths' outputs and the per-field confidence map so
    the AP Crew's HITL queue can present a side-by-side diff to a human.
    """

    def __init__(
        self,
        message: str,
        *,
        document_id: str | None,
        docling_partial: dict[str, Any] | None,
        fallback_partial: dict[str, Any] | None,
        confidence_report: dict[str, Any],
    ) -> None:
        super().__init__(
            message,
            document_id=document_id,
            partial=docling_partial,
        )
        self.docling_partial = docling_partial
        self.fallback_partial = fallback_partial
        self.confidence_report = confidence_report

    def to_dict(self) -> dict[str, Any]:
        base = super().to_dict()
        base["confidence_report"] = self.confidence_report
        return base


class FieldValidationError(ExtractionError):
    """Structural validation failure independent of model confidence.

    Examples: IBAN MOD-97 fails, USt-IdNr regex fails, line items don't sum
    to brutto within tolerance. These catch model hallucinations that the
    model itself reports as high-confidence — the highest-signal class of
    failure.
    """

    def __init__(
        self,
        message: str,
        *,
        document_id: str | None,
        field: str,
        value: Any,
        rule: str,
    ) -> None:
        super().__init__(message, document_id=document_id)
        self.field = field
        self.value = value
        self.rule = rule

    def to_dict(self) -> dict[str, Any]:
        base = super().to_dict()
        base.update({"field": self.field, "rule": self.rule})
        # value intentionally omitted — may be PII (IBAN, USt-IdNr)
        return base
