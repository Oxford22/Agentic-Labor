"""Stammdaten (master data) episode writer.

Carries master-data changes with provenance. Where multiple sites
disagree, the writer also emits a CONFLICT episode (see conflicts.py)
so the disagreement surfaces to the Sachbearbeiter UI for resolution.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, ClassVar, Literal

from pydantic import ConfigDict, Field

from putsch_memory.conflicts import maybe_record_conflict
from putsch_memory.writers.base import EpisodeBase, EpisodeWriter


class StammdatenEpisode(EpisodeBase):
    model_config = ConfigDict(extra="forbid", frozen=True)

    entity_label: Literal[
        "Lieferant", "Kunde", "Material", "Konto", "Standort", "Tochtergesellschaft"
    ]
    entity_id: str = Field(min_length=2, max_length=128)
    change_kind: Literal["create", "update", "supersede", "merge"]
    attributes_before: dict[str, Any] = Field(default_factory=dict)
    attributes_after: dict[str, Any] = Field(default_factory=dict)
    source_system: str
    is_correction: bool = False
    business_time_from: datetime | None = Field(
        default=None,
        description="Earlier than occurred_at iff this is a backdated correction.",
    )
    justification: str | None = Field(default=None, max_length=2048)


class StammdatenEpisodeWriter(EpisodeWriter[StammdatenEpisode]):
    episode_type: ClassVar[str] = "stammdaten_change"
    episode_model: ClassVar[type[EpisodeBase]] = StammdatenEpisode

    def _summary(self, episode: StammdatenEpisode) -> str:
        changed = sorted(
            k for k in episode.attributes_after if episode.attributes_before.get(k) != episode.attributes_after.get(k)
        )
        kind = "CORRECTION" if episode.is_correction else episode.change_kind.upper()
        return (
            f"Stammdaten {kind} on {episode.entity_label} {episode.entity_id} "
            f"({', '.join(changed) or 'no diff'}) "
            f"from {episode.source_system}"
        )

    async def write(self, episode: StammdatenEpisode) -> str:  # type: ignore[override]
        # Write the episode first, then check for cross-source conflicts.
        # If a conflict is recorded, the agent (Stammdaten crew) is
        # responsible for surfacing it to the Sachbearbeiter UI — this
        # writer does not auto-resolve.
        idem = await super().write(episode)
        await maybe_record_conflict(
            self._memory,
            entity_id=episode.entity_id,
            entity_label=episode.entity_label,
            asserted_at=episode.occurred_at,
            asserting_source=episode.source_system,
            asserted_attributes=episode.attributes_after,
            correlation_id=episode.correlation_id,
        )
        return idem
