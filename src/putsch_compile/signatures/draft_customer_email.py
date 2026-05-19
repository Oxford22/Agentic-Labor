"""Draft a German B2B customer email."""

from __future__ import annotations

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


class EmailTone(StrEnum):
    FORMAL = "formal"
    SACHLICH = "sachlich"
    PERSOENLICH = "persoenlich"


class KundenKontext(BaseModel):
    """Compact context the CRM hands the agent."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    kundenname: str
    ansprechpartner: str
    anrede: str = Field(..., description="z. B. 'Sehr geehrter Herr Dr. Albrecht,'.")
    branche: str | None = None
    kundensegment: str = Field(..., description="z. B. 'Schlüsselkunde', 'Standard', 'Neukunde'.")


@register
class DraftCustomerEmail(PutschSignature):
    """Verfasse eine deutsche B2B-Kunden-E-Mail.

    Kalibriert auf den ``ton``-Parameter:

    * ``formal`` — distanziert, dritte Person, kein 'Sie könnten...', stattdessen 'Wir bitten Sie'.
    * ``sachlich`` — knapp, faktenorientiert, max. 100 Wörter im Hauptteil.
    * ``persoenlich`` — Schlüsselkunde-Stil, persönliche Bezugnahme auf Historie wenn vorhanden.

    Niemals Anglizismen, niemals 'Hi'. Putsch-Briefkultur ist konservativ.
    """

    kundenkontext: KundenKontext = dspy.InputField()
    anliegen: str = dspy.InputField(
        desc="Knappe Beschreibung des Anliegens / der zu kommunizierenden Inhalte."
    )
    ton: EmailTone = dspy.InputField()
    historie: str = dspy.InputField(
        desc="Stichworte zu letzten Interaktionen mit dem Kunden. Leerstring wenn keine."
    )

    betreff: str = dspy.OutputField(desc="Konkreter Betreff ohne Re:/AW:-Präfixe.")
    anrede: str = dspy.OutputField(desc="Anrede-Zeile, abgeleitet aus ``kundenkontext.anrede``.")
    body: str = dspy.OutputField(desc="Hauptteil der E-Mail in Deutsch, ohne Anrede und Gruß.")
    gruss: str = dspy.OutputField(desc="Schlussformel, z. B. 'Mit freundlichen Grüßen'.")
    wortanzahl_body: int = dspy.OutputField(desc="Wortanzahl des Hauptteils zur Selbstkontrolle.")

    _meta: ClassVar[SignatureMeta] = SignatureMeta(
        name="draft_customer_email",
        owner_team=OwnerTeam.CRM,
        purpose=(
            "Erstellen deutscher B2B-Kunden-E-Mails im Putsch-Stil — formell, sachlich oder "
            "persönlich, je nach Kundensegment und Anliegen."
        ),
        version="1.0.0",
        accuracy_threshold=0.85,
        cost_ceiling_eur_per_1k_calls=0.90,
        instruction=(
            "Verfasse eine deutsche B2B-E-Mail. Übernimm die Anrede aus ``kundenkontext.anrede`` "
            "ungeändert. Halte den Ton strikt: formal = distanziert, sachlich = knapp, "
            "persönlich = Bezugnahme auf Historie. Keine Anglizismen, kein 'Hi'. Schließe mit "
            "'Mit freundlichen Grüßen', es sei denn die Historie deutet eine eingeführte "
            "persönlichere Formel an. Hauptteil maximal 150 Wörter."
        ),
        demos=(
            Demo(
                inputs={
                    "kundenkontext": {
                        "kundenname": "Maschinenbau Westfalen GmbH",
                        "ansprechpartner": "Dr. Albrecht",
                        "anrede": "Sehr geehrter Herr Dr. Albrecht,",
                        "branche": "Sondermaschinenbau",
                        "kundensegment": "Schlüsselkunde",
                    },
                    "anliegen": (
                        "Lieferzeit für Auftrag 4471 verschiebt sich um 5 Werktage auf KW 24 "
                        "aufgrund Materialverfügbarkeit Stahlträger."
                    ),
                    "ton": "persoenlich",
                    "historie": (
                        "Quartalsgespräch im April 2026; Hr. Albrecht hatte um proaktive "
                        "Kommunikation bei Lieferverzug ausdrücklich gebeten."
                    ),
                },
                outputs={
                    "betreff": "Auftrag 4471 — neue Liefertermin-Information",
                    "anrede": "Sehr geehrter Herr Dr. Albrecht,",
                    "body": (
                        "wie in unserem Quartalsgespräch im April abgesprochen, melde ich mich "
                        "frühzeitig: die Lieferung Ihres Auftrags 4471 verschiebt sich um fünf "
                        "Werktage auf Kalenderwoche 24, da unser Stahllieferant die Charge "
                        "HEA200 später bereitstellt. Wir haben die Engpassbeseitigung "
                        "intern eskaliert und stellen sicher, dass die Qualität der "
                        "Lieferung unverändert bleibt. Falls Sie Anpassungen an Ihrem "
                        "Produktionsplan benötigen, koordinieren wir das gerne kurzfristig."
                    ),
                    "gruss": "Mit freundlichen Grüßen",
                    "wortanzahl_body": 71,
                },
                labeled_by="l.kruger@putsch.example",
                rationale="Schlüsselkunde, Bezug zur Historie, proaktive Tonalität.",
            ),
            Demo(
                inputs={
                    "kundenkontext": {
                        "kundenname": "Auto-Teile Süd GmbH",
                        "ansprechpartner": "Buchhaltung",
                        "anrede": "Sehr geehrte Damen und Herren,",
                        "branche": "Großhandel",
                        "kundensegment": "Standard",
                    },
                    "anliegen": (
                        "Bitte um Übersendung der korrigierten Lieferadresse zur Aktualisierung "
                        "der Stammdaten."
                    ),
                    "ton": "sachlich",
                    "historie": "",
                },
                outputs={
                    "betreff": "Aktualisierung Lieferadresse — Bitte um Rückmeldung",
                    "anrede": "Sehr geehrte Damen und Herren,",
                    "body": (
                        "im Rahmen unserer Stammdatenpflege bitten wir Sie um eine kurze "
                        "Bestätigung der aktuellen Lieferadresse. Bitte senden Sie uns die "
                        "vollständige Anschrift (Straße, Hausnummer, PLZ, Ort) bis zum "
                        "Ende dieser Kalenderwoche per Antwortmail. Die Adresse wird "
                        "ausschließlich im SAP-Liefersatz hinterlegt."
                    ),
                    "gruss": "Mit freundlichen Grüßen",
                    "wortanzahl_body": 49,
                },
                labeled_by="l.kruger@putsch.example",
                rationale="Sachlich-knapper Stammdaten-Abgleich.",
            ),
        ),
    )
