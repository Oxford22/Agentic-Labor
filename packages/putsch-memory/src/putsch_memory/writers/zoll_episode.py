"""Customs (Zoll) episode writer."""

from __future__ import annotations

from datetime import datetime
from typing import ClassVar, Literal

from pydantic import ConfigDict, Field

from putsch_memory.ontology import EUR
from putsch_memory.writers.base import EpisodeBase, EpisodeWriter


class ZollEpisode(EpisodeBase):
    model_config = ConfigDict(extra="forbid", frozen=True)

    declaration_id: str
    bestellung_id: str | None = None
    direction: Literal["import", "export"]
    hs_code: str = Field(pattern=r"^\d{6,10}$")
    hs_code_source: Literal["taric_official", "agent_inferred", "manual_override"]
    statistical_value_eur: EUR
    country_origin: str = Field(min_length=2, max_length=2)
    country_destination: str = Field(min_length=2, max_length=2)
    submitted_to_authority: bool = False
    confidence: float = Field(ge=0.0, le=1.0, default=1.0)


class ZollEpisodeWriter(EpisodeWriter[ZollEpisode]):
    episode_type: ClassVar[str] = "zoll_declaration"
    episode_model: ClassVar[type[EpisodeBase]] = ZollEpisode

    def _summary(self, episode: ZollEpisode) -> str:
        submission = "submitted" if episode.submitted_to_authority else "drafted"
        return (
            f"Zoll {episode.direction} {submission}: "
            f"{episode.declaration_id} HS {episode.hs_code} "
            f"({episode.country_origin}->{episode.country_destination}, "
            f"{episode.statistical_value_eur:.2f} EUR, "
            f"source={episode.hs_code_source})"
        )

    async def write_declaration(
        self,
        *,
        declaration_id: str,
        direction: Literal["import", "export"],
        hs_code: str,
        hs_code_source: Literal["taric_official", "agent_inferred", "manual_override"],
        statistical_value_eur: float,
        country_origin: str,
        country_destination: str,
        occurred_at: datetime,
        correlation_id: str,
        bestellung_id: str | None = None,
        submitted_to_authority: bool = False,
        confidence: float = 1.0,
    ) -> str:
        episode = ZollEpisode(
            declaration_id=declaration_id,
            bestellung_id=bestellung_id,
            direction=direction,
            hs_code=hs_code,
            hs_code_source=hs_code_source,
            statistical_value_eur=statistical_value_eur,
            country_origin=country_origin,
            country_destination=country_destination,
            submitted_to_authority=submitted_to_authority,
            confidence=confidence,
            occurred_at=occurred_at,
            correlation_id=correlation_id,
            summary=f"Zoll {direction} {declaration_id} HS={hs_code}",
        )
        return await self.write(episode)
