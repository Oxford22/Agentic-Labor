"""Confidence calibrator tests.

These are the most strategic tests in the suite. The calibrator's behavior
gates downstream economics — every change here must be deliberate.
"""

from __future__ import annotations

from decimal import Decimal
from typing import Any

import pytest

from putsch_docs.confidence import (
    ConfidenceCalibrator,
    FieldEvidence,
    StructureEvidence,
    build_structure_evidence,
)
from putsch_docs.config import ConfidenceSettings
from putsch_docs.signatures import InvoiceFields


def _settings(**overrides: Any) -> ConfidenceSettings:
    return ConfidenceSettings(**overrides)


def _structure(**overrides: Any) -> StructureEvidence:
    base = {
        "arithmetic_consistent": True,
        "line_items_sum_consistent": True,
        "line_items_count": 2,
        "headline_mwst_rate_plausible": True,
        "invoice_date_plausible": True,
        "leistungsdatum_plausible": True,
    }
    base.update(overrides)
    return StructureEvidence(**base)


class TestPerFieldScoring:
    def test_high_when_all_signals_agree(self) -> None:
        cal = ConfidenceCalibrator(_settings())
        ev = [
            FieldEvidence(
                name="rechnungsnummer",
                value="2026-001",
                docling_score=0.97,
                judge_agreed=True,
                judge_confidence=0.95,
            )
        ]
        report = cal.build_report(
            invoice=None, field_evidence=ev, structure=_structure()
        )
        assert report.fields["rechnungsnummer"].level == "high"
        assert report.fields["rechnungsnummer"].final_score >= 0.90

    def test_validator_failure_collapses_score(self) -> None:
        cal = ConfidenceCalibrator(_settings())
        ev = [
            FieldEvidence(
                name="iban",
                value="DE89370400440532013001",  # MOD-97 off by 1
                docling_score=0.99,
            )
        ]
        report = cal.build_report(
            invoice=None, field_evidence=ev, structure=_structure()
        )
        fc = report.fields["iban"]
        assert fc.validator_passed is False
        # Failing a deterministic validator must collapse below the fallback threshold
        assert fc.final_score < 0.5
        assert fc.level == "low"

    def test_judge_disagreement_proportional_to_judge_confidence(self) -> None:
        cal = ConfidenceCalibrator(_settings())
        ev_low_conf_judge = FieldEvidence(
            name="rechnungsnummer",
            value="2026-001",
            docling_score=0.95,
            judge_agreed=False,
            judge_confidence=0.2,
        )
        ev_high_conf_judge = FieldEvidence(
            name="rechnungsnummer",
            value="2026-001",
            docling_score=0.95,
            judge_agreed=False,
            judge_confidence=0.95,
        )
        r1 = cal.build_report(
            invoice=None, field_evidence=[ev_low_conf_judge], structure=_structure()
        )
        r2 = cal.build_report(
            invoice=None, field_evidence=[ev_high_conf_judge], structure=_structure()
        )
        assert (
            r1.fields["rechnungsnummer"].final_score
            > r2.fields["rechnungsnummer"].final_score
        )

    def test_arithmetic_violation_collapses_amount_fields_only(self) -> None:
        cal = ConfidenceCalibrator(_settings())
        ev = [
            FieldEvidence(name="netto_betrag", value=Decimal("100"), docling_score=0.99),
            FieldEvidence(name="brutto_betrag", value=Decimal("119"), docling_score=0.99),
            FieldEvidence(name="rechnungsnummer", value="2026-001", docling_score=0.99),
        ]
        report = cal.build_report(
            invoice=None,
            field_evidence=ev,
            structure=_structure(arithmetic_consistent=False),
        )
        # Amounts collapse
        assert report.fields["netto_betrag"].final_score < 0.5
        assert report.fields["brutto_betrag"].final_score < 0.5
        # Non-amount field untouched
        assert report.fields["rechnungsnummer"].final_score >= 0.9

    def test_fallback_triggers_on_low_critical_field(self) -> None:
        cal = ConfidenceCalibrator(_settings(fallback_threshold=0.85))
        ev = [
            FieldEvidence(name="rechnungsnummer", value="2026-001", docling_score=0.80),
            FieldEvidence(name="rechnungsdatum", value="2026-04-18", docling_score=0.99),
            FieldEvidence(name="lieferant_ustid", value="DE129273398", docling_score=0.99),
            FieldEvidence(name="kunde_ustid", value="DE811184878", docling_score=0.99),
            FieldEvidence(name="iban", value="DE89370400440532013000", docling_score=0.99),
            FieldEvidence(name="netto_betrag", value=Decimal("100"), docling_score=0.99),
            FieldEvidence(name="mwst_betrag", value=Decimal("19"), docling_score=0.99),
            FieldEvidence(name="brutto_betrag", value=Decimal("119"), docling_score=0.99),
        ]
        report = cal.build_report(
            invoice=None, field_evidence=ev, structure=_structure()
        )
        assert report.fallback_required is True
        assert "rechnungsnummer" in (
            f.name for f in report.fields.values() if f.triggered_fallback
        )

    def test_critical_threshold_must_be_above_fallback(self) -> None:
        with pytest.raises(ValueError, match="critical_field_threshold"):
            from putsch_docs.config import Settings

            Settings(
                confidence=_settings(
                    fallback_threshold=0.90, critical_field_threshold=0.80
                )
            )


class TestStructureEvidenceBuilder:
    def test_build_from_canonical(self, canonical_invoice: InvoiceFields) -> None:
        s = build_structure_evidence(canonical_invoice)
        assert s.arithmetic_consistent is True
        assert s.line_items_sum_consistent is True
        assert s.invoice_date_plausible is True
        assert s.headline_mwst_rate_plausible is True

    def test_build_from_none(self) -> None:
        s = build_structure_evidence(None)
        assert s.arithmetic_consistent is False
        assert s.line_items_sum_consistent is None
        assert s.invoice_date_plausible is False


class TestOverallSignals:
    def test_overall_min_drives_decisions(self) -> None:
        cal = ConfidenceCalibrator(_settings())
        ev = [
            FieldEvidence(name="rechnungsnummer", value="X", docling_score=0.99),
            FieldEvidence(name="rechnungsdatum", value="2026-04-18", docling_score=0.30),
        ]
        report = cal.build_report(
            invoice=None, field_evidence=ev, structure=_structure()
        )
        assert report.overall_min < report.overall_mean
        assert report.overall_min <= 0.30
