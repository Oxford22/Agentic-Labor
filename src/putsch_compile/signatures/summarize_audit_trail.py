"""Summarize a sequence of audit-trail events into a Wirtschaftsprüfer-facing narrative."""

from __future__ import annotations

from datetime import date, datetime
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


class AuditSeverity(StrEnum):
    INFO = "info"
    HINWEIS = "hinweis"
    KRITISCH = "kritisch"


class AuditEvent(BaseModel):
    """One event from the audit log. Source = process or agent that emitted it."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    timestamp: datetime
    actor: str = Field(..., description="Mensch (LDAP) oder Agent-Name.")
    action: str = Field(..., description="z. B. 'invoice_booked', 'manual_correction'.")
    object_ref: str = Field(..., description="Geschäftsvorfall-ID, z. B. 'INV-2026-0421'.")
    detail: str = Field(..., description="Kurze deutsche Beschreibung.")


class AuditFlag(BaseModel):
    """A risk or note callout in the summary."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    severity: AuditSeverity
    titel: str = Field(..., min_length=4, max_length=80)
    beschreibung: str = Field(..., min_length=10, max_length=400)
    bezogen_auf: list[str] = Field(
        ...,
        description="Liste von ``object_ref`` der Events, die diesen Hinweis stützen.",
    )


@register
class SummarizeAuditTrail(PutschSignature):
    """Erzeuge eine prüferorientierte Zusammenfassung der Audit-Ereignisse.

    Zielgruppe: externer Wirtschaftsprüfer. Stil: nüchtern, sachlich, ohne Wertung, ohne
    Schuldzuweisung. Belege jede Schlussfolgerung mit Verweisen auf ``object_ref``.
    """

    events: list[AuditEvent] = dspy.InputField(
        desc="Chronologische Liste der Ereignisse im betrachteten Zeitraum."
    )
    zeitraum_von: date = dspy.InputField()
    zeitraum_bis: date = dspy.InputField()
    geschaeftsbereich: str = dspy.InputField(desc="z. B. 'Kreditorenbuchhaltung', 'Vertrieb DACH'.")

    zusammenfassung: str = dspy.OutputField(
        desc=(
            "Zusammenhängender deutscher Prüf-Bericht-Absatz, 5–10 Sätze, "
            "Bezug auf Ereignisse mit object_ref in Klammern."
        )
    )
    schluessige_kette: bool = dspy.OutputField(
        desc=(
            "True wenn die Ereignisse eine lückenlose Vorgangskette ergeben, sonst false. "
            "False zwingt Wirtschaftsprüfer zur Detailprüfung."
        )
    )
    flags: list[AuditFlag] = dspy.OutputField(
        desc="Auffälligkeiten (Severity-skaliert), die der Prüfer beachten sollte."
    )

    _meta: ClassVar[SignatureMeta] = SignatureMeta(
        name="summarize_audit_trail",
        owner_team=OwnerTeam.AUDIT,
        purpose=(
            "Verdichtung von Audit-Ereignissen zu einem prüferorientierten Bericht für "
            "Wirtschaftsprüfer (Jahresabschluss, Sonderprüfung) inkl. Risiko-Flags."
        ),
        version="1.0.0",
        accuracy_threshold=0.88,
        cost_ceiling_eur_per_1k_calls=1.80,
        instruction=(
            "Schreibe einen sachlichen Prüfbericht-Absatz auf Basis der Ereignisliste. Zitiere "
            "jedes nicht-triviale Ereignis mit seiner object_ref in runden Klammern. Stelle "
            "lückenlos dar; wenn eine Lücke besteht (z. B. Buchung ohne vorgelagerte Prüfung), "
            "vermerke dies als kritisches Flag. Keine Wertungen, keine Schuldzuweisungen — nur "
            "Sachverhalt."
        ),
        demos=(
            Demo(
                inputs={
                    "events": [
                        {
                            "timestamp": "2026-02-12T09:14:00Z",
                            "actor": "agent.invoice-extractor",
                            "action": "invoice_extracted",
                            "object_ref": "INV-2026-0301",
                            "detail": "Felder extrahiert, OCR-Konfidenz 0.97.",
                        },
                        {
                            "timestamp": "2026-02-12T09:14:08Z",
                            "actor": "agent.three-way-match",
                            "action": "match_passed",
                            "object_ref": "INV-2026-0301",
                            "detail": "Bestellnr. PO-4477, keine Abweichung.",
                        },
                        {
                            "timestamp": "2026-02-12T09:14:11Z",
                            "actor": "agent.datev-poster",
                            "action": "invoice_booked",
                            "object_ref": "INV-2026-0301",
                            "detail": "Gebucht auf SKR04-5400, KSt 1030.",
                        },
                    ],
                    "zeitraum_von": "2026-02-01",
                    "zeitraum_bis": "2026-02-28",
                    "geschaeftsbereich": "Kreditorenbuchhaltung",
                },
                outputs={
                    "zusammenfassung": (
                        "Im Berichtszeitraum 01.02.–28.02.2026 wurde im Bereich Kreditorenbuchhaltung "
                        "der Vorgang INV-2026-0301 vollautomatisch durchlaufen. Die Extraktion durch "
                        "den Invoice-Extractor erfolgte am 12.02. mit hoher OCR-Konfidenz "
                        "(INV-2026-0301). Anschließend bestätigte der Three-Way-Match die "
                        "Übereinstimmung mit der Bestellung PO-4477 (INV-2026-0301), bevor die "
                        "Buchung auf SKR04-Konto 5400, Kostenstelle 1030, sekundengleich erfolgte "
                        "(INV-2026-0301). Die Vorgangskette ist lückenlos und nachvollziehbar."
                    ),
                    "schluessige_kette": True,
                    "flags": [],
                },
                labeled_by="a.haring@putsch.example",
                rationale="Lückenloser Standardfall; nichts auffällig.",
            ),
            Demo(
                inputs={
                    "events": [
                        {
                            "timestamp": "2026-02-15T11:00:00Z",
                            "actor": "agent.datev-poster",
                            "action": "invoice_booked",
                            "object_ref": "INV-2026-0388",
                            "detail": "Gebucht auf SKR04-5400.",
                        },
                        {
                            "timestamp": "2026-02-18T08:30:00Z",
                            "actor": "s.vogt",
                            "action": "manual_correction",
                            "object_ref": "INV-2026-0388",
                            "detail": "Kostenstelle nachträglich auf 1030 geändert.",
                        },
                    ],
                    "zeitraum_von": "2026-02-01",
                    "zeitraum_bis": "2026-02-28",
                    "geschaeftsbereich": "Kreditorenbuchhaltung",
                },
                outputs={
                    "zusammenfassung": (
                        "Im Berichtszeitraum wurde der Vorgang INV-2026-0388 am 15.02. ohne "
                        "vorgelagerten Match-Eintrag direkt durch den DATEV-Poster gebucht "
                        "(INV-2026-0388). Am 18.02. korrigierte Sachbearbeiterin s.vogt die "
                        "Kostenstellenzuordnung manuell (INV-2026-0388). Eine vollständige "
                        "Vorgangskette mit Three-Way-Match-Eintrag ist im Audit-Log nicht "
                        "dokumentiert."
                    ),
                    "schluessige_kette": False,
                    "flags": [
                        {
                            "severity": "kritisch",
                            "titel": "Fehlender Match-Eintrag vor Buchung",
                            "beschreibung": (
                                "Der Three-Way-Match-Schritt ist im Audit-Log für INV-2026-0388 "
                                "nicht enthalten. Buchung erfolgte ohne dokumentierte "
                                "Plausibilitätsprüfung."
                            ),
                            "bezogen_auf": ["INV-2026-0388"],
                        }
                    ],
                },
                labeled_by="a.haring@putsch.example",
                rationale="Lücke vor der Buchung → kritisches Flag für den Prüfer.",
            ),
        ),
    )
