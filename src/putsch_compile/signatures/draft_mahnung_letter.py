"""Draft a German dunning letter at the configured Mahnstufe."""

from __future__ import annotations

from datetime import date
from decimal import Decimal
from enum import IntEnum
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


class Mahnstufe(IntEnum):
    """Three escalation levels. Putsch convention. Stufe 4 = Inkasso, not handled by this signature."""

    ZAHLUNGSERINNERUNG = 1
    ERSTE_MAHNUNG = 2
    LETZTE_MAHNUNG = 3


class OffenerPosten(BaseModel):
    """A single overdue invoice on the dunning letter."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    rechnungsnummer: str
    rechnungsdatum: date
    faelligkeitsdatum: date
    offener_betrag: Decimal = Field(..., gt=0)


class Kontaktdaten(BaseModel):
    """Pflichtangaben für den Briefkopf — § 35a GmbHG-konform."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    bearbeiter_name: str
    bearbeiter_email: str
    bearbeiter_telefon: str


@register
class DraftMahnungLetter(PutschSignature):
    """Verfasse eine deutsche Mahnung im Putsch-Stil, kalibriert auf die Mahnstufe.

    Ton-Skala:

    * Stufe 1 — höflich, partnerschaftlich, Zahlungserinnerung; *keine* Verzugszinsen drohen.
    * Stufe 2 — sachlich-bestimmt, Verzugszinsen ankündigen, Hinweis auf Verzugsschadenpauschale.
    * Stufe 3 — formell-streng, letzte Frist (10 Tage), Hinweis auf gerichtliches Mahnverfahren /
      Übergabe an Rechtsabteilung.

    Verstöße gegen den Ton (z. B. drohend in Stufe 1, zahnlos in Stufe 3) sind eine Regression.
    """

    kunde_name: str = dspy.InputField(desc="Firmenname des Kunden.")
    kunde_anrede: str = dspy.InputField(
        desc="z. B. 'Sehr geehrte Damen und Herren,' oder 'Sehr geehrter Herr Müller,'."
    )
    offene_posten: list[OffenerPosten] = dspy.InputField(desc="Mind. ein offener Posten.")
    mahnstufe: Mahnstufe = dspy.InputField(desc="1, 2 oder 3.")
    kontaktdaten: Kontaktdaten = dspy.InputField(desc="Pflichtangaben für die Signatur.")
    historie: str = dspy.InputField(
        desc=(
            "Kurze Stichworte zu früheren Kontakten / Zahlungszusagen / Reklamationen. "
            "Leerstring wenn keine Historie vorliegt."
        )
    )

    betreff: str = dspy.OutputField(
        desc="Konkreter Betreff, z. B. 'Zahlungserinnerung Rechnung 2026-0421'."
    )
    body: str = dspy.OutputField(
        desc=(
            "Vollständiger Brieftext in Deutsch. Beinhaltet Anrede, Begründung, Tabelle der "
            "offenen Posten als Aufzählung, gesetzte Frist, Signatur."
        )
    )
    summe_offener_betrag: Decimal = dspy.OutputField(
        desc="Summe aller offenen Beträge — muss mit der Eingabe übereinstimmen."
    )
    ton_score: float = dspy.OutputField(
        desc=(
            "Selbsteinschätzung auf einer 0–1-Skala, 0=sehr höflich, 1=sehr streng. "
            "Stufe 1 ≈ 0.15, Stufe 2 ≈ 0.45, Stufe 3 ≈ 0.85."
        )
    )

    _meta: ClassVar[SignatureMeta] = SignatureMeta(
        name="draft_mahnung_letter",
        owner_team=OwnerTeam.AR_DUNNING,
        purpose=(
            "Verfassen deutscher Mahnungen (Stufe 1–3) im Putsch-Stil, ton-kalibriert auf die "
            "jeweilige Eskalationsstufe, mit korrekter rechtlicher Andeutung."
        ),
        version="1.0.0",
        accuracy_threshold=0.85,
        cost_ceiling_eur_per_1k_calls=1.20,
        instruction=(
            "Verfasse eine deutsche Mahnung im Putsch-Briefstil. Halte den Ton strikt zur "
            "Mahnstufe: höflich (1), bestimmt (2), formell-streng (3). Erwähne nie Inkasso oder "
            "gerichtliches Verfahren vor Stufe 3. Frist: Stufe 1 — 14 Tage, Stufe 2 — 10 Tage, "
            "Stufe 3 — 7 Tage. Liste alle offenen Posten als Aufzählung mit Rechnungsnummer, "
            "Datum, Betrag. Schließe mit der konfigurierten Bearbeiter-Signatur — niemals "
            "anonym. Keine Verzugszinsen-Beträge schätzen; nur ankündigen."
        ),
        demos=(
            Demo(
                inputs={
                    "kunde_name": "Maschinenbau Westfalen GmbH",
                    "kunde_anrede": "Sehr geehrter Herr Dr. Albrecht,",
                    "offene_posten": [
                        {
                            "rechnungsnummer": "2026-0301",
                            "rechnungsdatum": "2026-02-12",
                            "faelligkeitsdatum": "2026-03-14",
                            "offener_betrag": "4820.50",
                        }
                    ],
                    "mahnstufe": 1,
                    "kontaktdaten": {
                        "bearbeiter_name": "Sabine Vogt",
                        "bearbeiter_email": "s.vogt@putsch.example",
                        "bearbeiter_telefon": "+49 2331 555-204",
                    },
                    "historie": "",
                },
                outputs={
                    "betreff": "Zahlungserinnerung — Rechnung 2026-0301",
                    "body": (
                        "Sehr geehrter Herr Dr. Albrecht,\n\n"
                        "darf ich Sie freundlich daran erinnern, dass die folgende Rechnung "
                        "noch offen ist:\n\n"
                        "• Rechnung 2026-0301 vom 12.02.2026 — 4.820,50 EUR — fällig seit "
                        "14.03.2026\n\n"
                        "Eventuell hat sich die Zahlung mit unserer Erinnerung überschnitten — "
                        "in diesem Fall betrachten Sie dieses Schreiben bitte als gegenstandslos. "
                        "Andernfalls bitten wir Sie, den offenen Betrag bis zum 28.05.2026 auf "
                        "unser bekanntes Konto anzuweisen.\n\n"
                        "Bei Fragen erreichen Sie mich gerne unter +49 2331 555-204 oder per "
                        "E-Mail.\n\n"
                        "Mit freundlichen Grüßen\n"
                        "Sabine Vogt\nPutsch Group — Forderungsmanagement"
                    ),
                    "summe_offener_betrag": "4820.50",
                    "ton_score": 0.15,
                },
                labeled_by="s.vogt@putsch.example",
                rationale="Stufe-1-Standardvorlage. Keine Zinsen drohen, freundliche Frist 14 Tage.",
            ),
            Demo(
                inputs={
                    "kunde_name": "Logistik Süd OHG",
                    "kunde_anrede": "Sehr geehrte Damen und Herren,",
                    "offene_posten": [
                        {
                            "rechnungsnummer": "2025-1188",
                            "rechnungsdatum": "2025-11-05",
                            "faelligkeitsdatum": "2025-12-05",
                            "offener_betrag": "18750.00",
                        },
                        {
                            "rechnungsnummer": "2025-1244",
                            "rechnungsdatum": "2025-11-28",
                            "faelligkeitsdatum": "2025-12-28",
                            "offener_betrag": "9420.00",
                        },
                    ],
                    "mahnstufe": 3,
                    "kontaktdaten": {
                        "bearbeiter_name": "Sabine Vogt",
                        "bearbeiter_email": "s.vogt@putsch.example",
                        "bearbeiter_telefon": "+49 2331 555-204",
                    },
                    "historie": (
                        "Stufe 1 am 12.01.2026 verschickt, Stufe 2 am 02.02.2026, keine "
                        "Reaktion. Zahlungszusage vom 20.01. nicht eingehalten."
                    ),
                },
                outputs={
                    "betreff": "Letzte Mahnung — Forderung 28.170,00 EUR",
                    "body": (
                        "Sehr geehrte Damen und Herren,\n\n"
                        "trotz Zahlungserinnerung und erster Mahnung sind die nachstehenden "
                        "Forderungen weiterhin offen:\n\n"
                        "• Rechnung 2025-1188 vom 05.11.2025 — 18.750,00 EUR — fällig seit "
                        "05.12.2025\n"
                        "• Rechnung 2025-1244 vom 28.11.2025 —  9.420,00 EUR — fällig seit "
                        "28.12.2025\n\n"
                        "Wir fordern Sie hiermit letztmalig auf, den Gesamtbetrag von "
                        "28.170,00 EUR bis spätestens 25.05.2026 zu begleichen. Für den Fall, "
                        "dass die Zahlung nicht fristgerecht eingeht, wird die Forderung ohne "
                        "weitere Ankündigung an unsere Rechtsabteilung übergeben; Verzugszinsen "
                        "sowie eine Verzugsschadenpauschale gemäß § 288 Abs. 5 BGB werden "
                        "gesondert berechnet.\n\n"
                        "Sollte die Zahlung bereits erfolgt sein, übersenden Sie uns bitte "
                        "umgehend einen Zahlungsnachweis.\n\n"
                        "Mit freundlichen Grüßen\n"
                        "Sabine Vogt\nPutsch Group — Forderungsmanagement"
                    ),
                    "summe_offener_betrag": "28170.00",
                    "ton_score": 0.87,
                },
                labeled_by="s.vogt@putsch.example",
                rationale=(
                    "Stufe 3 mit ausdrücklicher Rechtsabteilung-Andeutung, § 288 BGB, "
                    "Frist 7 Tage gerechnet vom 18.05.2026."
                ),
            ),
        ),
    )
