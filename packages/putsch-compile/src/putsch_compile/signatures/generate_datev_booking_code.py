"""Determine the DATEV SKR03/SKR04 account assignment for an invoice line item."""

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


class Kontenrahmen(StrEnum):
    """Putsch books in SKR04; SKR03 supported for subsidiaries acquired pre-2018."""

    SKR03 = "SKR03"
    SKR04 = "SKR04"


class UstSchluessel(StrEnum):
    """DATEV USt-Schlüssel — the most common subset."""

    INLAND_VOLL = "9"  # 19 % Inland
    INLAND_ERMAESSIGT = "8"  # 7 % Inland
    INNERGEMEINSCHAFTLICH = "10"  # Erwerb innergemeinschaftlich
    DRITTLAND = "12"  # Einfuhr Drittland
    STEUERFREI = "0"


class LineItemContext(BaseModel):
    """Line item + commercial context the booking rule depends on."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    kontierungstext: str = Field(..., min_length=1)
    betrag_netto: Decimal = Field(..., ge=0)
    mwst_satz: Decimal = Field(..., ge=0, le=Decimal("0.30"))
    lieferant_kategorie: str = Field(
        ...,
        description="Putsch-interne Kategorie, z. B. 'Produktionsmaterial', 'Dienstleistung-IT'.",
    )
    kostenstelle_hinweis: str | None = Field(
        default=None,
        description="Optionaler Hinweis aus der Bestellung, z. B. 'Werk Hagen, Halle 3'.",
    )


@register
class GenerateDatevBookingCode(PutschSignature):
    """Bestimme die DATEV-Kontierung (Sachkonto, Kostenstelle, USt-Schlüssel).

    Vorgaben:

    * Sachkonto ist 4-stellig (SKR04) bzw. 4-stellig (SKR03). Niemals nur 3-stellig.
    * Kostenstelle ist 4-stellig nach Putsch-Konvention (Werk + Bereich).
    * Kostenträger optional; nur belegen wenn aus dem Kontext eindeutig.
    """

    line_item: LineItemContext = dspy.InputField()
    kontenrahmen: Kontenrahmen = dspy.InputField()

    sachkonto: str = dspy.OutputField(
        desc="4-stelliges Sachkonto. SKR04: z. B. '5400' für Wareneingang."
    )
    kostenstelle: str = dspy.OutputField(
        desc="4-stellige Putsch-Kostenstelle. Werk + Bereich, z. B. '1030' für Hagen-Produktion."
    )
    kostentraeger: str | None = dspy.OutputField(
        desc="Optional. Nur belegen wenn aus dem Kontext eindeutig zuordenbar."
    )
    ust_schluessel: UstSchluessel = dspy.OutputField()
    confidence: float = dspy.OutputField(
        desc="0.0–1.0. <0.8 erzwingt Sachbearbeiter-Review im DATEV-Workflow."
    )
    rationale: str = dspy.OutputField(
        desc="Knappe deutsche Begründung — Kontotyp + warum, in einem Satz."
    )

    _meta: ClassVar[SignatureMeta] = SignatureMeta(
        name="generate_datev_booking_code",
        owner_team=OwnerTeam.DATEV_PLATFORM,
        purpose=(
            "Vorschlag der DATEV-Kontierung (Sachkonto + Kostenstelle + USt-Schlüssel) je "
            "Rechnungsposition, basierend auf Kontierungstext und Lieferanten-Kategorie."
        ),
        version="1.0.0",
        accuracy_threshold=0.93,
        cost_ceiling_eur_per_1k_calls=0.20,
        instruction=(
            "Wähle ein Sachkonto aus dem angegebenen Kontenrahmen (SKR03 oder SKR04), eine "
            "Putsch-Kostenstelle, optional einen Kostenträger und den passenden USt-Schlüssel. "
            "Halte dich strikt an die Lieferanten-Kategorie als primären Diskriminator. Bei "
            "Unklarheit über Kostenstelle: nimm '9999' (Sammelkonto) und setze confidence < 0.7. "
            "Niemals erfundene Konten — wenn unsicher, das allgemeinste passende Konto."
        ),
        demos=(
            Demo(
                inputs={
                    "line_item": {
                        "kontierungstext": "Stahlträger HEA200 für Halle Werk Hagen",
                        "betrag_netto": "994.00",
                        "mwst_satz": "0.19",
                        "lieferant_kategorie": "Produktionsmaterial",
                        "kostenstelle_hinweis": "Werk Hagen, Halle 3",
                    },
                    "kontenrahmen": "SKR04",
                },
                outputs={
                    "sachkonto": "5400",
                    "kostenstelle": "1030",
                    "kostentraeger": None,
                    "ust_schluessel": "9",
                    "confidence": 0.95,
                    "rationale": (
                        "Produktionsmaterial Inland 19 % → Wareneingang (SKR04 5400), "
                        "Werk Hagen Produktion KSt 1030."
                    ),
                },
                labeled_by="b.koenig@putsch.example",
                rationale="Eindeutiger Wareneingang Inland.",
            ),
            Demo(
                inputs={
                    "line_item": {
                        "kontierungstext": "SaaS-Lizenz Atlassian Jira, Jahresabo",
                        "betrag_netto": "4200.00",
                        "mwst_satz": "0.0",
                        "lieferant_kategorie": "Dienstleistung-IT",
                        "kostenstelle_hinweis": None,
                    },
                    "kontenrahmen": "SKR04",
                },
                outputs={
                    "sachkonto": "6805",
                    "kostenstelle": "9100",
                    "kostentraeger": None,
                    "ust_schluessel": "10",
                    "confidence": 0.91,
                    "rationale": (
                        "IT-Dienstleistung aus EU-Ausland (Reverse Charge) → SKR04 6805 EDV-Software, "
                        "Kostenstelle IT 9100, USt-Schlüssel 10."
                    ),
                },
                labeled_by="b.koenig@putsch.example",
                rationale="Reverse-Charge-Klassiker.",
            ),
        ),
    )
