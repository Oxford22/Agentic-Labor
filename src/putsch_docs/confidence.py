"""Per-field confidence calibration.

This is the most strategic code in the module. It decides:
1. Which fields are confident enough to ship.
2. Which trigger the VLM fallback path.
3. Which fail-hard into the AP Crew's HITL queue.

Design choices:
- Confidence is per-field, not per-document. An invoice with high-confidence
  header fields and one low-confidence line item triggers fallback only for
  the line item region; the header is not re-extracted.
- Signals are combined, never trusted in isolation:
    A. Docling region confidence (structural extractor's self-report)
    B. Format validator pass/fail (cheap, deterministic, highest signal)
    C. Arithmetic consistency at structure level (netto+mwst=brutto)
    D. LLM-as-judge agreement (most expensive; only on critical fields)
- Defaults are conservative. A field is "high" only if every signal agrees.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from putsch_docs.config import ConfidenceSettings, get_settings
from putsch_docs.observability import get_logger
from putsch_docs.signatures import InvoiceFields
from putsch_docs.validators import (
    amounts_consistent,
    is_plausible_invoice_date,
    is_plausible_leistungsdatum,
    is_valid_iban,
    is_valid_ustid,
    line_items_sum_to_netto,
    mwst_rate_plausible,
)

log = get_logger(__name__)

ConfidenceLevel = Literal["high", "medium", "low"]


# ----- Public reports --------------------------------------------------------------


class FieldConfidence(BaseModel):
    """Confidence record for one extracted field."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    name: str
    docling_score: float = Field(ge=0.0, le=1.0)
    validator_passed: bool | None = Field(
        default=None,
        description="None when no deterministic validator exists for this field.",
    )
    judge_agreed: bool | None = Field(
        default=None,
        description="None when judge was not invoked for this field.",
    )
    judge_confidence: float | None = Field(default=None, ge=0.0, le=1.0)
    final_score: float = Field(ge=0.0, le=1.0)
    level: ConfidenceLevel
    triggered_fallback: bool = False


class ConfidenceReport(BaseModel):
    """Per-document confidence report attached to every ExtractionResult."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    fields: dict[str, FieldConfidence]
    overall_min: float
    overall_mean: float
    arithmetic_consistent: bool
    line_items_sum_consistent: bool | None
    fallback_required: bool
    critical_failures: list[str] = Field(default_factory=list)

    def to_loggable(self) -> dict[str, Any]:
        """Compact form suitable for structured logs / Langfuse attributes."""
        return {
            "overall_min": round(self.overall_min, 4),
            "overall_mean": round(self.overall_mean, 4),
            "arithmetic_ok": self.arithmetic_consistent,
            "line_items_ok": self.line_items_sum_consistent,
            "fallback_required": self.fallback_required,
            "critical_failures": list(self.critical_failures),
            "low_fields": sorted(
                k for k, v in self.fields.items() if v.level == "low"
            ),
        }


# ----- Signal inputs ---------------------------------------------------------------


@dataclass(slots=True)
class FieldEvidence:
    """One field's raw evidence — fed into ConfidenceCalibrator."""

    name: str
    value: Any
    docling_score: float
    document_excerpt: str | None = None  # used by judge
    judge_agreed: bool | None = None
    judge_confidence: float | None = None


@dataclass(slots=True)
class StructureEvidence:
    """Structure-level signals."""

    arithmetic_consistent: bool
    line_items_sum_consistent: bool | None
    line_items_count: int = 0
    headline_mwst_rate_plausible: bool = True
    invoice_date_plausible: bool = True
    leistungsdatum_plausible: bool = True


# ----- Validator dispatch ----------------------------------------------------------

# Per-field deterministic validator. None ⇒ no validator applicable.
# Functions take the field value and return bool. Wrap them to be safe on
# missing values.


def _validate_field(name: str, value: Any) -> bool | None:
    if value is None:
        return None
    try:
        match name:
            case "lieferant_ustid" | "kunde_ustid":
                return is_valid_ustid(str(value))
            case "iban":
                return is_valid_iban(str(value))
            case "rechnungsdatum":
                return is_plausible_invoice_date(value)
            case "mwst_satz":
                return mwst_rate_plausible(Decimal(str(value)))
            case _:
                return None
    except (ValueError, TypeError, ArithmeticError):
        return False


# ----- Calibrator ------------------------------------------------------------------


@dataclass
class ConfidenceCalibrator:
    """Combines signals into per-field FieldConfidence and an overall report.

    Weighting model:
        final_score = min(
            base_score,
            validator_factor,
            judge_factor,
            structure_factor,
        )

    `min` is intentional: any single negative signal collapses confidence.
    We do not average — averaging hides hard failures behind weak positives.
    Arithmetic violation collapses the score for every amount field, not
    just one.
    """

    settings: ConfidenceSettings = field(default_factory=lambda: get_settings().confidence)

    # ---------- public ----------

    def build_report(
        self,
        *,
        invoice: InvoiceFields | None,
        field_evidence: list[FieldEvidence],
        structure: StructureEvidence,
    ) -> ConfidenceReport:
        amount_fields = {"netto_betrag", "mwst_betrag", "brutto_betrag"}
        confidence_map: dict[str, FieldConfidence] = {}
        for ev in field_evidence:
            confidence_map[ev.name] = self._score_field(
                ev,
                structure=structure,
                is_amount_field=ev.name in amount_fields,
            )

        if confidence_map:
            scores = [c.final_score for c in confidence_map.values()]
            overall_min = min(scores)
            overall_mean = sum(scores) / len(scores)
        else:
            overall_min = 0.0
            overall_mean = 0.0

        critical_failures = self._collect_critical_failures(confidence_map)
        fallback_required = self._needs_fallback(confidence_map, structure)

        report = ConfidenceReport(
            fields=confidence_map,
            overall_min=overall_min,
            overall_mean=overall_mean,
            arithmetic_consistent=structure.arithmetic_consistent,
            line_items_sum_consistent=structure.line_items_sum_consistent,
            fallback_required=fallback_required,
            critical_failures=critical_failures,
        )

        log.info(
            "confidence.report",
            **report.to_loggable(),
            has_invoice=invoice is not None,
        )
        return report

    # ---------- per-field scoring ----------

    def _score_field(
        self,
        ev: FieldEvidence,
        *,
        structure: StructureEvidence,
        is_amount_field: bool,
    ) -> FieldConfidence:
        base = max(0.0, min(1.0, ev.docling_score))

        validator_passed = _validate_field(ev.name, ev.value)
        validator_factor = self._validator_factor(validator_passed)

        judge_factor = self._judge_factor(ev.judge_agreed, ev.judge_confidence)

        # Structure-level: arithmetic violation collapses every amount field.
        structure_factor = 1.0
        if is_amount_field and not structure.arithmetic_consistent:
            structure_factor = 0.30  # not zero — we still want to surface the value
        # Line-items mismatch dings line item & netto
        if (
            structure.line_items_sum_consistent is False
            and ev.name in {"netto_betrag", "line_items"}
        ):
            structure_factor = min(structure_factor, 0.40)
        # Date plausibility
        if ev.name == "rechnungsdatum" and not structure.invoice_date_plausible:
            structure_factor = min(structure_factor, 0.25)
        if ev.name == "leistungsdatum" and not structure.leistungsdatum_plausible:
            structure_factor = min(structure_factor, 0.40)
        # Headline MwSt rate
        if ev.name == "mwst_satz" and not structure.headline_mwst_rate_plausible:
            structure_factor = min(structure_factor, 0.50)

        final_score = min(base, validator_factor, judge_factor, structure_factor)
        level = self._level(final_score, ev.name)

        triggered_fallback = (
            final_score < self.settings.fallback_threshold
            and ev.name in self.settings.critical_fields
        )

        return FieldConfidence(
            name=ev.name,
            docling_score=base,
            validator_passed=validator_passed,
            judge_agreed=ev.judge_agreed,
            judge_confidence=ev.judge_confidence,
            final_score=final_score,
            level=level,
            triggered_fallback=triggered_fallback,
        )

    def _validator_factor(self, passed: bool | None) -> float:
        if passed is None:
            return 1.0  # no validator => non-signal
        return 1.0 if passed else 0.10  # failing a deterministic validator is near-fatal

    def _judge_factor(
        self, agreed: bool | None, judge_confidence: float | None
    ) -> float:
        if agreed is None:
            return 1.0
        if agreed:
            # Mild positive boost ceiling — judge agreement is a noisy signal
            return 1.0
        # Judge disagrees: factor proportional to judge's own confidence in its disagreement
        jc = max(0.0, min(1.0, judge_confidence or 0.5))
        return 1.0 - (0.7 * jc)

    def _level(self, score: float, field_name: str) -> ConfidenceLevel:
        if score >= self.settings.critical_field_threshold:
            return "high"
        if score >= self.settings.fallback_threshold:
            return "medium"
        # Critical fields' "medium" still demands escalation; the field-level
        # `triggered_fallback` flag drives that.
        _ = field_name
        return "low"

    # ---------- report-level decisions ----------

    def _collect_critical_failures(
        self, confidence_map: dict[str, FieldConfidence]
    ) -> list[str]:
        out: list[str] = []
        for name in self.settings.critical_fields:
            fc = confidence_map.get(name)
            if fc is None:
                continue
            if fc.final_score < self.settings.critical_field_threshold:
                out.append(name)
        return out

    def _needs_fallback(
        self,
        confidence_map: dict[str, FieldConfidence],
        structure: StructureEvidence,
    ) -> bool:
        # Trigger fallback if any critical field is below the fallback threshold
        for name in self.settings.critical_fields:
            fc = confidence_map.get(name)
            if fc and fc.final_score < self.settings.fallback_threshold:
                return True
        # Or if invoice-level arithmetic doesn't reconcile
        if not structure.arithmetic_consistent:
            return True
        # Or if line items don't sum (only meaningful when we have line items)
        if (
            structure.line_items_count > 0
            and structure.line_items_sum_consistent is False
        ):
            return True
        return False


# ----- Structure evidence builder ---------------------------------------------------


def build_structure_evidence(invoice: InvoiceFields | None) -> StructureEvidence:
    """Compute structure-level signals from a parsed InvoiceFields.

    Safe to call with `None` (extractor failed to coerce into the schema) —
    returns the most conservative evidence.
    """
    if invoice is None:
        return StructureEvidence(
            arithmetic_consistent=False,
            line_items_sum_consistent=None,
            line_items_count=0,
            headline_mwst_rate_plausible=False,
            invoice_date_plausible=False,
            leistungsdatum_plausible=False,
        )

    arith = amounts_consistent(
        invoice.netto_betrag, invoice.mwst_betrag, invoice.brutto_betrag
    )
    if invoice.line_items:
        s = sum((li.gesamtpreis for li in invoice.line_items), Decimal("0"))
        li_sum = line_items_sum_to_netto(s, invoice.netto_betrag)
    else:
        li_sum = None

    date_ok = is_plausible_invoice_date(invoice.rechnungsdatum)
    leistung_ok = (
        is_plausible_leistungsdatum(invoice.leistungsdatum, invoice.rechnungsdatum)
        if invoice.leistungsdatum is not None
        else True
    )

    return StructureEvidence(
        arithmetic_consistent=arith,
        line_items_sum_consistent=li_sum,
        line_items_count=len(invoice.line_items),
        headline_mwst_rate_plausible=mwst_rate_plausible(invoice.mwst_satz),
        invoice_date_plausible=date_ok,
        leistungsdatum_plausible=leistung_ok,
    )
