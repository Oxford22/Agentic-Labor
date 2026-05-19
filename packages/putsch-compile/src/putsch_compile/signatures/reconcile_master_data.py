"""Reconcile two master-data records and surface conflicts."""

from __future__ import annotations

from enum import StrEnum
from typing import Any, ClassVar

import dspy
from pydantic import BaseModel, ConfigDict, Field

from putsch_compile.signatures._base import (
    Demo,
    OwnerTeam,
    PutschSignature,
    SignatureMeta,
    register,
)


class MasterDataSource(StrEnum):
    SAP_CUSTOMER = "sap_customer"
    SAP_VENDOR = "sap_vendor"
    CRM_HUBSPOT = "crm_hubspot"
    DATEV_DEBITOR = "datev_debitor"
    DATEV_KREDITOR = "datev_kreditor"
    HANDELSREGISTER = "handelsregister"
    EXTERNAL_FORM = "external_form"


class FieldConflict(BaseModel):
    """One field where the two records disagree."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    field: str
    value_a: str | None
    value_b: str | None
    resolution_source: MasterDataSource | None = Field(
        default=None,
        description="Which input was chosen as the merged value (null = manual review).",
    )
    confidence: float = Field(..., ge=0.0, le=1.0)


@register
class ReconcileMasterData(PutschSignature):
    """Vergleiche zwei Stammdaten-Sätze und schlage einen abgeglichenen Datensatz vor.

    Strikte Regel: bei Konflikt in einem Pflichtfeld (USt-IdNr, IBAN, Anschrift-Straße/PLZ/Ort,
    Firmenname) und Konfidenz <0.85 setze ``manual_review_required = true`` und liefere den
    konfliktbehafteten Datensatz unverändert zurück. Niemals raten bei Pflichtfeldern.
    """

    record_a: dict[str, Any] = dspy.InputField(
        desc="Stammdaten-Satz aus Quelle A. Flach, key=field, value=string."
    )
    record_b: dict[str, Any] = dspy.InputField(
        desc="Stammdaten-Satz aus Quelle B. Flach, key=field, value=string."
    )
    source_a: MasterDataSource = dspy.InputField()
    source_b: MasterDataSource = dspy.InputField()
    feldpriorisierung: dict[str, list[MasterDataSource]] = dspy.InputField(
        desc=(
            "Pro Feld eine Reihenfolge von Quellen, höchste Priorität zuerst. "
            "Beispiel: {'ustid': ['handelsregister', 'sap_vendor']}."
        )
    )

    merged_record: dict[str, Any] = dspy.OutputField(
        desc="Vorgeschlagener vereinigter Datensatz. Bei Konflikt mit niedriger Konfidenz: Feld leer."
    )
    conflicts: list[FieldConflict] = dspy.OutputField(
        desc="Alle Felder, in denen die Eingaben abweichen, inkl. Auflösungsbegründung."
    )
    manual_review_required: bool = dspy.OutputField(
        desc="True wenn mindestens ein Pflichtfeld unter 0.85 Konfidenz aufgelöst wurde."
    )
    overall_confidence: float = dspy.OutputField(desc="Aggregierte Konfidenz über alle Felder.")

    _meta: ClassVar[SignatureMeta] = SignatureMeta(
        name="reconcile_master_data",
        owner_team=OwnerTeam.MDM,
        purpose=(
            "Abgleich von Stammdaten-Sätzen aus zwei Quellen (z. B. SAP-Vendor vs. "
            "Handelsregister) inkl. Auflösung von Konflikten mit Feld-Prioritäten und "
            "Manual-Review-Flag bei niedriger Konfidenz."
        ),
        version="1.0.0",
        accuracy_threshold=0.93,
        cost_ceiling_eur_per_1k_calls=1.10,
        instruction=(
            "Vergleiche die beiden Datensätze Feld für Feld. Bei Übereinstimmung übernimm den "
            "Wert ohne Konflikt-Eintrag. Bei Abweichung wähle die höher priorisierte Quelle laut "
            "``feldpriorisierung``; ohne Eintrag in der Priorisierung gilt die offiziellere "
            "Quelle (handelsregister > sap > crm > external_form). Setze "
            "manual_review_required=true wenn ein Pflichtfeld (ustid, iban, "
            "strasse, plz, ort, firmenname) mit Konfidenz <0.85 aufgelöst wurde. "
            "Erfinde niemals Werte; leere Felder bleiben leer."
        ),
        demos=(
            Demo(
                inputs={
                    "record_a": {
                        "firmenname": "Mustermann GmbH",
                        "ustid": "DE123456789",
                        "strasse": "Industriestraße 12",
                        "plz": "58095",
                        "ort": "Hagen",
                        "iban": "DE12370400440532013000",
                    },
                    "record_b": {
                        "firmenname": "Mustermann GmbH & Co. KG",
                        "ustid": "DE123456789",
                        "strasse": "Industriestraße 12",
                        "plz": "58095",
                        "ort": "Hagen",
                        "iban": "DE12370400440532013000",
                    },
                    "source_a": "sap_vendor",
                    "source_b": "handelsregister",
                    "feldpriorisierung": {"firmenname": ["handelsregister", "sap_vendor"]},
                },
                outputs={
                    "merged_record": {
                        "firmenname": "Mustermann GmbH & Co. KG",
                        "ustid": "DE123456789",
                        "strasse": "Industriestraße 12",
                        "plz": "58095",
                        "ort": "Hagen",
                        "iban": "DE12370400440532013000",
                    },
                    "conflicts": [
                        {
                            "field": "firmenname",
                            "value_a": "Mustermann GmbH",
                            "value_b": "Mustermann GmbH & Co. KG",
                            "resolution_source": "handelsregister",
                            "confidence": 0.94,
                        }
                    ],
                    "manual_review_required": False,
                    "overall_confidence": 0.96,
                },
                labeled_by="j.engel@putsch.example",
                rationale="Handelsregister hat Vorrang beim Firmennamen.",
            ),
            Demo(
                inputs={
                    "record_a": {
                        "firmenname": "Tech-Solutions OHG",
                        "ustid": "DE555111222",
                        "iban": "DE88500105175407324931",
                    },
                    "record_b": {
                        "firmenname": "Tech Solutions OHG",
                        "ustid": "DE555111333",
                        "iban": "DE12370400440532013000",
                    },
                    "source_a": "datev_kreditor",
                    "source_b": "external_form",
                    "feldpriorisierung": {},
                },
                outputs={
                    "merged_record": {
                        "firmenname": "Tech-Solutions OHG",
                        "ustid": "",
                        "iban": "",
                    },
                    "conflicts": [
                        {
                            "field": "firmenname",
                            "value_a": "Tech-Solutions OHG",
                            "value_b": "Tech Solutions OHG",
                            "resolution_source": "datev_kreditor",
                            "confidence": 0.92,
                        },
                        {
                            "field": "ustid",
                            "value_a": "DE555111222",
                            "value_b": "DE555111333",
                            "resolution_source": None,
                            "confidence": 0.40,
                        },
                        {
                            "field": "iban",
                            "value_a": "DE88500105175407324931",
                            "value_b": "DE12370400440532013000",
                            "resolution_source": None,
                            "confidence": 0.35,
                        },
                    ],
                    "manual_review_required": True,
                    "overall_confidence": 0.55,
                },
                labeled_by="j.engel@putsch.example",
                rationale="Zwei Pflichtfelder (USt-IdNr, IBAN) im Konflikt → Manual Review.",
            ),
        ),
    )
