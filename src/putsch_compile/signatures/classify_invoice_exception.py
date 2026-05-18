"""Classify an invoice exception and recommend AP routing."""

from __future__ import annotations

from decimal import Decimal
from enum import StrEnum
from typing import ClassVar

import dspy
from pydantic import BaseModel, ConfigDict, Field

from putsch_compile.signatures._base import (
    Demo,
    OwnerTeam,
    PutschSignature,
    SignatureMeta,
    register,
)


class ExceptionCategory(StrEnum):
    PRICE_MISMATCH = "price_mismatch"
    QUANTITY_MISMATCH = "quantity_mismatch"
    MISSING_PO = "missing_po"
    DUPLICATE = "duplicate"
    USTID_INVALID = "ustid_invalid"
    IBAN_MISMATCH = "iban_mismatch"
    OUT_OF_TOLERANCE = "out_of_tolerance"
    OTHER = "other"


class RoutingTarget(StrEnum):
    AP_AUTOMATIC = "ap_automatic"
    AP_REVIEW = "ap_review"
    PROCUREMENT = "procurement"
    CONTROLLING = "controlling"


class MatchFinding(BaseModel):
    """A single finding from the three-way match (PO ↔ goods receipt ↔ invoice)."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    field: str = Field(..., description="z. B. einzelpreis, menge, lieferdatum")
    expected: str
    actual: str
    delta_abs: Decimal | None = None
    delta_pct: Decimal | None = None


class InvoiceSummary(BaseModel):
    """Compact projection of the extracted invoice. Don't re-pass the full text."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    rechnungsnummer: str
    lieferant_name: str
    lieferant_ustid: str | None = None
    brutto_betrag: Decimal
    netto_betrag: Decimal
    bestellnummer: str | None = None


@register
class ClassifyInvoiceException(PutschSignature):
    """Klassifiziere die AP-Ausnahme und schlage einen Routing-Pfad vor.

    Hochriskante Routings (PROCUREMENT, CONTROLLING) erfordern eine knappe Begründung — sie werden
    in der Sachbearbeiter-UI direkt angezeigt.
    """

    invoice: InvoiceSummary = dspy.InputField(desc="Kompakte Zusammenfassung der Rechnung.")
    pruefung_findings: list[MatchFinding] = dspy.InputField(
        desc="Befunde aus dem 3-Wege-Match — leere Liste wenn keine Abweichung."
    )
    toleranz_pct: Decimal = dspy.InputField(
        desc="Konfigurierte Toleranzschwelle für Preisabweichungen (z. B. 0.02 für 2 %)."
    )
    duplikate_im_30_tage_fenster: int = dspy.InputField(
        desc="Anzahl ähnlicher Rechnungen desselben Lieferanten in den letzten 30 Tagen."
    )

    category: ExceptionCategory = dspy.OutputField(
        desc="Genau eine Kategorie — die dominante Ursache."
    )
    routing: RoutingTarget = dspy.OutputField(
        desc=(
            "ap_automatic = automatisch buchen; ap_review = Sachbearbeiter prüft; "
            "procurement = Einkauf prüft Konditionen; controlling = Controlling prüft Compliance."
        )
    )
    confidence: float = dspy.OutputField(
        desc="0.0–1.0. <0.7 zwingt Routing nach ap_review unabhängig von der Kategorie."
    )
    rationale: str = dspy.OutputField(
        desc="Eine kurze deutsche Begründung (max. 240 Zeichen) — wird in der UI angezeigt."
    )

    _meta: ClassVar[SignatureMeta] = SignatureMeta(
        name="classify_invoice_exception",
        owner_team=OwnerTeam.AP_AUTOMATION,
        purpose=(
            "Klassifizierung von AP-Ausnahmen (Preis-/Mengen-Abweichung, fehlende Bestellung, "
            "Duplikate, USt-IdNr ungültig) und Routing-Empfehlung für den Workflow."
        ),
        version="1.0.0",
        accuracy_threshold=0.92,
        cost_ceiling_eur_per_1k_calls=0.80,
        instruction=(
            "Wähle genau eine Ausnahme-Kategorie. Wenn mehrere zutreffen, die mit dem höchsten "
            "monetären Risiko (Preis > Menge > USt-IdNr > Duplikat > sonstige). Routing folgt der "
            "Kategorie nach dem Putsch-AP-Playbook. Bei Konfidenz <0.7 immer ap_review wählen — "
            "Automatik bei Unsicherheit ist die teuerste Fehlentscheidung."
        ),
        demos=(
            Demo(
                inputs={
                    "invoice": {
                        "rechnungsnummer": "2026-0091",
                        "lieferant_name": "Stahlhandel Nord GmbH",
                        "lieferant_ustid": "DE111222333",
                        "brutto_betrag": "12500.00",
                        "netto_betrag": "10504.20",
                        "bestellnummer": "PO-4477",
                    },
                    "pruefung_findings": [
                        {
                            "field": "einzelpreis",
                            "expected": "248.50",
                            "actual": "265.00",
                            "delta_abs": "16.50",
                            "delta_pct": "0.066",
                        }
                    ],
                    "toleranz_pct": "0.02",
                    "duplikate_im_30_tage_fenster": 0,
                },
                outputs={
                    "category": "price_mismatch",
                    "routing": "procurement",
                    "confidence": 0.93,
                    "rationale": (
                        "Einzelpreisabweichung 6,6 % > Toleranz 2 %. Einkauf prüft, "
                        "ob Konditionsänderung vereinbart wurde."
                    ),
                },
                labeled_by="t.becker@putsch.example",
                rationale="Klassischer Preis-Mismatch oberhalb Toleranz → Einkauf.",
            ),
            Demo(
                inputs={
                    "invoice": {
                        "rechnungsnummer": "RE-559912",
                        "lieferant_name": "Bürobedarf Ost OHG",
                        "lieferant_ustid": "DE777888999",
                        "brutto_betrag": "421.30",
                        "netto_betrag": "354.03",
                        "bestellnummer": None,
                    },
                    "pruefung_findings": [],
                    "toleranz_pct": "0.02",
                    "duplikate_im_30_tage_fenster": 1,
                },
                outputs={
                    "category": "missing_po",
                    "routing": "ap_review",
                    "confidence": 0.86,
                    "rationale": (
                        "Keine Bestellnummer im Beleg, jedoch nur eine weitere ähnliche "
                        "Rechnung im 30-Tage-Fenster — kein klares Duplikat."
                    ),
                },
                labeled_by="t.becker@putsch.example",
                rationale="Fehlende PO → ap_review; Duplikat-Heuristik schwach.",
            ),
        ),
    )
