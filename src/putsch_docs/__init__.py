"""Putsch AP Crew document/OCR layer.

Primary: Docling + Granite-Docling 258M (structural parser, MIT, IBM Research).
Fallback: Qwen2.5-VL-72B via vLLM (vision-language reasoner) when Docling
confidence drops below threshold on critical fields.

Public surface — everything else is internal:
"""

from putsch_docs.confidence import ConfidenceReport, FieldConfidence
from putsch_docs.exceptions import (
    ConfidenceError,
    DoclingError,
    ExtractionError,
    FallbackError,
    FieldValidationError,
)
from putsch_docs.extractor import DoclingExtractor, ExtractionResult, ExtractionTrace
from putsch_docs.signatures import InvoiceFields, InvoiceLineItem

__all__ = [
    "ConfidenceError",
    "ConfidenceReport",
    "DoclingError",
    "DoclingExtractor",
    "ExtractionError",
    "ExtractionResult",
    "ExtractionTrace",
    "FallbackError",
    "FieldConfidence",
    "FieldValidationError",
    "InvoiceFields",
    "InvoiceLineItem",
]

__version__ = "0.1.0"
