"""Extract structured fields from a German Eingangsrechnung."""

from __future__ import annotations

from datetime import date
from decimal import Decimal
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


class MwStBreakdown(BaseModel):
    """Per-rate VAT breakdown. Most German invoices have 19 % and 7 % lines side by side."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    satz: Decimal = Field(..., ge=0, le=Decimal("0.30"), description="Steuersatz als Dezimal, z. B. 0.19.")
    netto: Decimal = Field(..., description="Netto-Bemessungsgrundlage für diesen Satz.")
    mwst: Decimal = Field(..., description="Mehrwertsteuerbetrag für diesen Satz.")


class LineItem(BaseModel):
    """Single ``Position`` on an invoice."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    position: int = Field(..., ge=1)
    bezeichnung: str = Field(..., min_length=1)
    menge: Decimal = Field(..., gt=0)
    einheit: str = Field(..., min_length=1, max_length=8, description="z. B. Stk, kg, h, m.")
    einzelpreis_netto: Decimal = Field(..., ge=0)
    gesamt_netto: Decimal = Field(..., ge=0)
    mwst_satz: Decimal = Field(..., ge=0, le=Decimal("0.30"))


@register
class ExtractInvoiceFields(PutschSignature):
    """Extrahiere die Stammdaten und Positionen einer deutschen Eingangsrechnung.

    Ergebnisse müssen Buchhaltungs-belastbar sein: Beträge stimmen mit Brutto = Summe(Netto) +
    Summe(MwSt) überein, USt-IdNr ist eine gültige DE/EU-Form, Datum ist ISO-8601. Wenn ein Feld
    auf der Rechnung nicht aufgeführt ist, gib ``null`` zurück anstatt zu raten.
    """

    invoice_text: str = dspy.InputField(
        desc=(
            "OCR-Text einer Eingangsrechnung. Reihenfolge entspricht ungefähr dem Dokumentenfluss "
            "(Briefkopf → Positionen → Summen → Bankverbindung)."
        )
    )
    ocr_confidence: float = dspy.InputField(
        desc="Mittlere Zeichen-Konfidenz aus Docling (0.0–1.0). Tief = ggf. Felder verifizieren."
    )

    rechnungsnummer: str = dspy.OutputField(
        desc="Rechnungsnummer exakt wie aufgedruckt, ohne Präfixe wie 'Re-Nr.'."
    )
    lieferant_name: str = dspy.OutputField(desc="Firmenname laut Briefkopf.")
    lieferant_ustid: str | None = dspy.OutputField(
        desc="USt-IdNr (z. B. DE123456789). null falls nicht ausgewiesen."
    )
    lieferant_steuernummer: str | None = dspy.OutputField(desc="Inländische Steuernummer (z. B. 121/567/89012). null falls nicht ausgewiesen.")
    rechnungsdatum: date = dspy.OutputField(desc="ISO-8601, YYYY-MM-DD.")
    leistungsdatum: date = dspy.OutputField(
        desc="Leistungs- oder Lieferdatum. Falls nicht separat ausgewiesen → Rechnungsdatum übernehmen."
    )
    waehrung: str = dspy.OutputField(desc="ISO-4217, dreistellig. Standard EUR.")
    netto_betrag: Decimal = dspy.OutputField()
    brutto_betrag: Decimal = dspy.OutputField()
    mwst_aufschluesselung: list[MwStBreakdown] = dspy.OutputField(
        desc="Eine Zeile je MwSt-Satz. Summen müssen mit den Positionen übereinstimmen."
    )
    line_items: list[LineItem] = dspy.OutputField(desc="Alle Rechnungspositionen, geordnet.")
    iban: str | None = dspy.OutputField(desc="IBAN ohne Leerzeichen, oder null.")
    skontosatz: Decimal | None = dspy.OutputField(
        desc="Skontosatz als Dezimal (z. B. 0.02 für 2 %), oder null."
    )
    skonto_frist_tage: int | None = dspy.OutputField(
        desc="Skontofrist in Tagen, oder null wenn nicht ausgewiesen."
    )

    _meta: ClassVar[SignatureMeta] = SignatureMeta(
        name="extract_invoice_fields",
        owner_team=OwnerTeam.AP_AUTOMATION,
        purpose=(
            "Strukturierte Extraktion aller buchungsrelevanten Felder einer deutschen "
            "Eingangsrechnung für den AP-Workflow."
        ),
        version="1.0.0",
        accuracy_threshold=0.95,
        cost_ceiling_eur_per_1k_calls=0.30,
        instruction=(
            "Extrahiere alle buchungsrelevanten Felder einer deutschen Eingangsrechnung. "
            "Beträge sind in Euro mit Punkt als Dezimaltrenner. Datum ist ISO-8601. "
            "Wenn ein Feld nicht ausgewiesen ist, gib null zurück — niemals raten. "
            "Brutto muss Summe(Netto)+Summe(MwSt) entsprechen; bei Inkonsistenz vermerke "
            "trotzdem die Werte wie ausgedruckt und überlasse den Match-Check der nachgelagerten "
            "Prüfung."
        ),
        demos=(
            Demo(
                inputs={
                    "invoice_text": (
                        "Mustermann GmbH\nUSt-IdNr: DE123456789\nRechnung Nr. 2025-0421\n"
                        "Rechnungsdatum: 12.01.2026\nLeistungsdatum: 10.01.2026\n"
                        "Pos. 1: Stahlträger HEA200 — 4 Stk × 248,50 € = 994,00 €  19 %\n"
                        "Netto 994,00 €\nMwSt 19 % 188,86 €\nBrutto 1.182,86 €\n"
                        "IBAN: DE12 3704 0044 0532 0130 00"
                    ),
                    "ocr_confidence": 0.97,
                },
                outputs={
                    "rechnungsnummer": "2025-0421",
                    "lieferant_name": "Mustermann GmbH",
                    "lieferant_ustid": "DE123456789",
                    "lieferant_steuernummer": None,
                    "rechnungsdatum": "2026-01-12",
                    "leistungsdatum": "2026-01-10",
                    "waehrung": "EUR",
                    "netto_betrag": "994.00",
                    "brutto_betrag": "1182.86",
                    "mwst_aufschluesselung": [
                        {"satz": "0.19", "netto": "994.00", "mwst": "188.86"}
                    ],
                    "line_items": [
                        {
                            "position": 1,
                            "bezeichnung": "Stahlträger HEA200",
                            "menge": "4",
                            "einheit": "Stk",
                            "einzelpreis_netto": "248.50",
                            "gesamt_netto": "994.00",
                            "mwst_satz": "0.19",
                        }
                    ],
                    "iban": "DE12370400440532013000",
                    "skontosatz": None,
                    "skonto_frist_tage": None,
                },
                labeled_by="m.lehner@putsch.example",
                rationale=(
                    "Standard-Eingangsrechnung. Skonto fehlt → null. IBAN ohne Leerzeichen "
                    "normalisiert."
                ),
            ),
            Demo(
                inputs={
                    "invoice_text": (
                        "Schmidt Werkzeuge KG\nSteuernummer: 121/567/89012\n"
                        "USt-IdNr: DE987654321\nRechnung 2026/3301\n"
                        "Datum: 02.03.2026  Leistung: Februar 2026\n"
                        "1) Bohrer 8mm  10 Stk × 4,90 = 49,00 €  19 %\n"
                        "2) Versandkosten  1 × 6,50 = 6,50 €  19 %\n"
                        "Netto 55,50  MwSt 10,55  Brutto 66,05  EUR\n"
                        "Zahlbar binnen 14 Tagen, 2 % Skonto bei Zahlung in 7 Tagen.\n"
                        "IBAN DE88 5001 0517 5407 3249 31"
                    ),
                    "ocr_confidence": 0.94,
                },
                outputs={
                    "rechnungsnummer": "2026/3301",
                    "lieferant_name": "Schmidt Werkzeuge KG",
                    "lieferant_ustid": "DE987654321",
                    "lieferant_steuernummer": "121/567/89012",
                    "rechnungsdatum": "2026-03-02",
                    "leistungsdatum": "2026-02-28",
                    "waehrung": "EUR",
                    "netto_betrag": "55.50",
                    "brutto_betrag": "66.05",
                    "mwst_aufschluesselung": [
                        {"satz": "0.19", "netto": "55.50", "mwst": "10.55"}
                    ],
                    "line_items": [
                        {
                            "position": 1,
                            "bezeichnung": "Bohrer 8mm",
                            "menge": "10",
                            "einheit": "Stk",
                            "einzelpreis_netto": "4.90",
                            "gesamt_netto": "49.00",
                            "mwst_satz": "0.19",
                        },
                        {
                            "position": 2,
                            "bezeichnung": "Versandkosten",
                            "menge": "1",
                            "einheit": "Stk",
                            "einzelpreis_netto": "6.50",
                            "gesamt_netto": "6.50",
                            "mwst_satz": "0.19",
                        },
                    ],
                    "iban": "DE88500105175407324931",
                    "skontosatz": "0.02",
                    "skonto_frist_tage": 7,
                },
                labeled_by="m.lehner@putsch.example",
                rationale=(
                    "Leistungszeitraum 'Februar 2026' → letzter Tag des Monats als "
                    "Leistungsdatum (Konvention)."
                ),
            ),
        ),
    )
