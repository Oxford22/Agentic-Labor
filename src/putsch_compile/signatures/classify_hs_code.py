"""Classify a product into an 8-digit harmonized system code for EU customs."""

from __future__ import annotations

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


class HSAlternative(BaseModel):
    """Alternative HS code with its likelihood — surfaced to the customs Sachbearbeiter."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    hs_code: str = Field(..., pattern=r"^\d{8}$")
    confidence: float = Field(..., ge=0.0, le=1.0)
    rationale: str = Field(..., min_length=5, max_length=240)


@register
class ClassifyHSCode(PutschSignature):
    """Bestimme den 8-stelligen Zolltarif-HS-Code für eine Warenposition.

    Output: genau ein primärer Code + bis zu zwei Alternativen mit Begründung. Die Alternativen
    werden in der Customs-UI angezeigt; der Sachbearbeiter wählt aus oder bestätigt den primären.
    """

    produkt_beschreibung: str = dspy.InputField(
        desc="Freitext-Beschreibung der Ware, idealerweise aus dem Stamm- oder Bestelldatenfeld."
    )
    material: str = dspy.InputField(
        desc="Hauptmaterial (z. B. 'Stahl unlegiert', 'Aluminium', 'Polyethylen'). Leer wenn unbekannt."
    )
    verwendung: str = dspy.InputField(
        desc="Anwendungszweck (z. B. 'Maschinenbau', 'Lebensmittelindustrie'). Leer wenn unbekannt."
    )
    herkunftsland: str = dspy.InputField(desc="ISO-3166 Alpha-2 Code der Herkunft, z. B. 'DE'.")

    hs_code: str = dspy.OutputField(
        desc="8-stelliger HS/CN-Code laut Kombinierter Nomenklatur (EU), ohne Trennzeichen."
    )
    confidence: float = dspy.OutputField(desc="0.0–1.0. <0.7 erzwingt Sachbearbeiter-Review.")
    rationale: str = dspy.OutputField(
        desc=(
            "Knappe deutsche Begründung — welche Position der KN, welches Kapitel, "
            "welches Unterscheidungsmerkmal."
        )
    )
    alternativen: list[HSAlternative] = dspy.OutputField(
        desc="0–2 Alternativen, sortiert nach Konfidenz absteigend."
    )

    _meta: ClassVar[SignatureMeta] = SignatureMeta(
        name="classify_hs_code",
        owner_team=OwnerTeam.CUSTOMS,
        purpose=(
            "Automatisierte Zolltarif-Vorklassifikation (KN/HS 8-stellig) für die Erstanlage "
            "im Customs-Workflow. Endgültige Festlegung bleibt beim Sachbearbeiter."
        ),
        version="1.0.0",
        accuracy_threshold=0.88,
        cost_ceiling_eur_per_1k_calls=0.30,
        instruction=(
            "Bestimme den passenden 8-stelligen HS/CN-Code für die beschriebene Ware. Halte dich "
            "an die EU-Kombinierte-Nomenklatur. Falls Material oder Verwendung fehlen und der "
            "Code dadurch nicht eindeutig wird, wähle den allgemeinsten passenden Code und "
            "vermerke das in der Begründung. Liefere stets mindestens eine Alternative wenn die "
            "primäre Konfidenz unter 0.85 liegt."
        ),
        demos=(
            Demo(
                inputs={
                    "produkt_beschreibung": "Spiralbohrer HSS-Co8, Durchmesser 8 mm, Schaft zylindrisch",
                    "material": "Schnellarbeitsstahl mit 8 % Kobalt",
                    "verwendung": "Metallbearbeitung",
                    "herkunftsland": "DE",
                },
                outputs={
                    "hs_code": "82075019",
                    "confidence": 0.93,
                    "rationale": (
                        "Kapitel 82 (Werkzeuge), Position 8207 (auswechselbare Werkzeuge), "
                        "Unterposition 50 (Bohrwerkzeuge) — Spiralbohrer aus HSS-Co fallen "
                        "unter 8207 50 19."
                    ),
                    "alternativen": [],
                },
                labeled_by="r.weiss@putsch.example",
                rationale="Standardfall Metallwerkzeug.",
            ),
            Demo(
                inputs={
                    "produkt_beschreibung": "PE-Folie schwarz, Stärke 0.10 mm, Rolle 1,2 m × 100 m",
                    "material": "Polyethylen niedriger Dichte (LDPE)",
                    "verwendung": "Bauabdeckung",
                    "herkunftsland": "PL",
                },
                outputs={
                    "hs_code": "39201010",
                    "confidence": 0.78,
                    "rationale": (
                        "Kapitel 39 (Kunststoffe), Position 3920 (Tafeln/Folien aus Kunststoff, "
                        "ohne Verstärkung), Unterposition 10 für Polymere des Ethylens. "
                        "Stärke <0.125 mm passt zu 3920 10 23 — aber LDPE-Folie als "
                        "Bauabdeckung wird in der Praxis häufig auf 3920 10 10 gestellt."
                    ),
                    "alternativen": [
                        {
                            "hs_code": "39201023",
                            "confidence": 0.62,
                            "rationale": "Folie ohne Schaumstoff, Stärke <0.125 mm.",
                        }
                    ],
                },
                labeled_by="r.weiss@putsch.example",
                rationale="Stärke 0.10 mm liegt im Grenzbereich → Alternative explizit anbieten.",
            ),
        ),
    )
