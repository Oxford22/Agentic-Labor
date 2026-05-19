"""Service registry for DSGVO Art. 30.

Every traced Putsch service must register itself. The registration is the
single source of truth from which the Verzeichnis is generated, the
ClickHouse TTL policy is provisioned, and the Betriebsrat memo is
auto-drafted.

A registration is a small Pydantic model declared at module load time, so
forgetting it produces an obvious CI failure (the generator step fails
fast). The registry is process-local; in production, the CI step gathers
registrations from every service and emits a single Verzeichnis.
"""

from __future__ import annotations

from enum import StrEnum
from typing import Iterable

from pydantic import BaseModel, ConfigDict, Field, field_validator

from putsch_obs.config import TraceRetentionClass


class LegalBasis(StrEnum):
    """GDPR Art. 6(1) legal bases. Plus a Betrieb-specific note for §26 BDSG."""

    CONSENT = "consent"                                # Art. 6(1)(a)
    CONTRACT = "contract"                              # Art. 6(1)(b)
    LEGAL_OBLIGATION = "legal_obligation"              # Art. 6(1)(c)
    VITAL_INTEREST = "vital_interest"                  # Art. 6(1)(d)
    PUBLIC_TASK = "public_task"                        # Art. 6(1)(e)
    LEGITIMATE_INTEREST = "legitimate_interest"        # Art. 6(1)(f)
    EMPLOYMENT_RELATIONSHIP = "employment_relationship"  # §26 BDSG
    BETRVG_KI_EINSATZ = "betrvg_ki_einsatz"            # Putsch Betriebsvereinbarung


class DataCategory(StrEnum):
    """Categories used in the Verzeichnis. Map ~1:1 to PII categories
    detected by the redactor, plus business-side categories.
    """

    CONTACT_DATA = "kontaktdaten"
    EMPLOYEE_IDENTIFIERS = "mitarbeiteridentifikatoren"
    FINANCIAL = "finanzdaten"
    TAX_IDS = "steueridentifikatoren"
    INVOICE_LINE_ITEMS = "rechnungspositionen"
    CUSTOMS_DATA = "zolldaten"
    COMMUNICATION_CONTENT = "kommunikationsinhalte"
    METADATA = "metadaten"


class DataSubject(StrEnum):
    EMPLOYEES = "beschaeftigte"
    CUSTOMERS = "kunden"
    SUPPLIERS = "lieferanten"
    AUTHORITIES = "behoerden"


class ProcessingActivity(BaseModel):
    """One service's processing-activity entry, as required by Art. 30(1).

    Field names are German for direct copy-paste into the Verzeichnis PDF
    the Datenschutzbeauftragte hands to authorities.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    service_name: str
    bezeichnung: str = Field(..., description="Bezeichnung der Verarbeitung")
    zweck: str = Field(..., description="Zweck der Verarbeitung (Art. 30(1)(b))")
    rechtsgrundlage: LegalBasis = Field(..., description="Art. 6(1) GDPR (or §26 BDSG)")
    betroffene_personen: tuple[DataSubject, ...] = Field(default_factory=tuple)
    datenkategorien: tuple[DataCategory, ...] = Field(default_factory=tuple)
    empfaenger: tuple[str, ...] = Field(
        default=("Putsch Group Buchhaltung", "Datenschutzbeauftragte"),
        description="Empfänger oder Kategorien (Art. 30(1)(d))",
    )
    drittland_transfers: tuple[str, ...] = Field(
        default=(),
        description="Drittlandstransfers (Art. 30(1)(e)). Should be empty.",
    )
    retention_class: TraceRetentionClass = TraceRetentionClass.LIMITED_RISK
    aufbewahrungsfrist: str = Field(
        default="3 Jahre nach Vorgangsende",
        description="Aufbewahrungsfrist (Art. 30(1)(f))",
    )
    technische_maßnahmen: tuple[str, ...] = Field(
        default=(
            "PII-Redaction am Ausgang der Anwendungsgrenze",
            "Reversible Tokenisierung mit Audit-Trail (Vault, WORM)",
            "Self-hosted Langfuse in der Frankfurter VPC",
            "TLS 1.3 für alle internen Verbindungen",
        ),
        description="TOM (Art. 30(1)(g))",
    )
    ai_act_risk_class: str = Field(
        default="limited_risk",
        description="EU AI Act 2024/1689 risk classification",
    )

    @field_validator("drittland_transfers")
    @classmethod
    def _no_third_country(cls, v: tuple[str, ...]) -> tuple[str, ...]:
        # Putsch's stance: no third-country transfers. If you set this,
        # CI will fail — exactly what we want as a tripwire.
        if v:
            raise ValueError(
                "Drittlandstransfers verboten. Wenn unvermeidbar: "
                "ADR und Datenschutz-Folgenabschätzung anhängen, dann override."
            )
        return v


_REGISTRY: dict[str, ProcessingActivity] = {}


def register_service(activity: ProcessingActivity) -> None:
    """Register a service. Last-wins per service_name.

    Conventionally invoked at module import time, e.g. in your service's
    ``__init__.py``::

        from putsch_obs.dsgvo import register_service, ProcessingActivity, ...
        register_service(ProcessingActivity(
            service_name="ap-crew",
            bezeichnung="KI-gestützte Rechnungsverarbeitung",
            zweck="Automatische Extraktion und DATEV-Buchung eingehender Rechnungen",
            rechtsgrundlage=LegalBasis.CONTRACT,
            ...
        ))
    """
    _REGISTRY[activity.service_name] = activity


def registered_activities() -> Iterable[ProcessingActivity]:
    return tuple(_REGISTRY.values())


def reset_registry_for_test() -> None:
    _REGISTRY.clear()


__all__ = [
    "DataCategory",
    "DataSubject",
    "LegalBasis",
    "ProcessingActivity",
    "register_service",
    "registered_activities",
    "reset_registry_for_test",
]
