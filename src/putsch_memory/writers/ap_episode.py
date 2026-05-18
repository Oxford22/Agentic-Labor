"""AP Crew episode writer.

Called by the AP crew after each completed invoice processing run.
Captures: which Rechnung, against which Bestellung + Wareneingang, posted
to which Konto / Buchungsperiode, the three-way-match outcome, and any
manual overrides applied by a Sachbearbeiter.
"""

from __future__ import annotations

from datetime import datetime
from typing import ClassVar, Literal

from pydantic import ConfigDict, Field

from putsch_memory.ontology import EUR
from putsch_memory.writers.base import EpisodeBase, EpisodeWriter


class APThreeWayMatch(EpisodeBase):
    """The three-way-match outcome embedded in an AP episode."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    matched_lines: int = Field(ge=0)
    quantity_variance: float = Field(default=0.0)
    price_variance_eur: float = Field(default=0.0)
    decision: Literal["auto_post", "hold_for_review", "reject"]
    decision_reason: str = Field(max_length=512)


class APEpisode(EpisodeBase):
    """An AP Crew completion record. Schema-validated before write."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    rechnung_id: str = Field(min_length=2, max_length=64)
    bestellung_id: str | None = None
    wareneingang_id: str | None = None
    posted_to_konto: str | None = Field(default=None, pattern=r"^\d{3,8}$")
    posted_to_period: str | None = Field(default=None, pattern=r"^\d{4}-(0[1-9]|1[0-2])$")
    gross_eur: EUR
    currency: Literal["EUR", "USD", "CHF", "GBP", "CZK", "NOK"] = "EUR"
    three_way_match: APThreeWayMatch
    manual_override_by: str | None = Field(
        default=None,
        description="Mitarbeiter id if a human overrode the agent's decision.",
    )
    manual_override_justification: str | None = Field(default=None, max_length=1024)
    confidence: float = Field(ge=0.0, le=1.0, default=1.0)


class APEpisodeWriter(EpisodeWriter[APEpisode]):
    episode_type: ClassVar[str] = "ap_completion"
    episode_model: ClassVar[type[EpisodeBase]] = APEpisode

    def _summary(self, episode: APEpisode) -> str:
        decision = episode.three_way_match.decision
        return (
            f"AP {decision} for Rechnung {episode.rechnung_id} "
            f"(PO {episode.bestellung_id or '∅'}) "
            f"@ {episode.gross_eur:.2f} {episode.currency} "
            f"-> Konto {episode.posted_to_konto or 'PENDING'} "
            f"period {episode.posted_to_period or 'PENDING'}"
        )

    async def write_completion(
        self,
        *,
        rechnung_id: str,
        bestellung_id: str | None,
        posted_to_konto: str | None,
        posted_to_period: str | None,
        gross_eur: float,
        decision: Literal["auto_post", "hold_for_review", "reject"],
        decision_reason: str,
        matched_lines: int,
        occurred_at: datetime,
        correlation_id: str,
        currency: str = "EUR",
        wareneingang_id: str | None = None,
        quantity_variance: float = 0.0,
        price_variance_eur: float = 0.0,
        confidence: float = 1.0,
        manual_override_by: str | None = None,
        manual_override_justification: str | None = None,
    ) -> str:
        """Ergonomic helper — most callers go through this rather than
        constructing the model by hand."""
        episode = APEpisode(
            rechnung_id=rechnung_id,
            bestellung_id=bestellung_id,
            wareneingang_id=wareneingang_id,
            posted_to_konto=posted_to_konto,
            posted_to_period=posted_to_period,
            gross_eur=gross_eur,
            currency=currency,  # type: ignore[arg-type]
            three_way_match=APThreeWayMatch(
                matched_lines=matched_lines,
                quantity_variance=quantity_variance,
                price_variance_eur=price_variance_eur,
                decision=decision,
                decision_reason=decision_reason,
                occurred_at=occurred_at,
                correlation_id=correlation_id,
                summary="three-way match",
            ),
            manual_override_by=manual_override_by,
            manual_override_justification=manual_override_justification,
            confidence=confidence,
            occurred_at=occurred_at,
            correlation_id=correlation_id,
            summary=(
                f"AP completion {rechnung_id} -> {decision}"
                + (f" (override by {manual_override_by})" if manual_override_by else "")
            ),
        )
        return await self.write(episode)
