"""Mahnverfahren (dunning) episode writer."""

from __future__ import annotations

from datetime import datetime
from typing import ClassVar, Literal

from pydantic import ConfigDict, Field

from putsch_memory.ontology import EUR
from putsch_memory.writers.base import EpisodeBase, EpisodeWriter


class MahnverfahrenEpisode(EpisodeBase):
    model_config = ConfigDict(extra="forbid", frozen=True)

    case_id: str
    kunde_id: str
    outstanding_eur: EUR
    stage_from: Literal["pre_dunning", "soft_reminder", "formal_dunning", "legal", "written_off"]
    stage_to: Literal["pre_dunning", "soft_reminder", "formal_dunning", "legal", "written_off"]
    action: Literal["letter_sent", "phone_attempted", "phone_completed", "escalated", "settled", "written_off"]
    channel: Literal["email", "post", "phone", "internal"]
    drafted_by_agent: str
    approved_by_human: str | None = Field(default=None, description="Mitarbeiter id of approver.")
    settlement_amount_eur: EUR | None = None
    notes: str = Field(default="", max_length=2048)


class MahnverfahrenEpisodeWriter(EpisodeWriter[MahnverfahrenEpisode]):
    episode_type: ClassVar[str] = "mahnverfahren_event"
    episode_model: ClassVar[type[EpisodeBase]] = MahnverfahrenEpisode

    def _summary(self, episode: MahnverfahrenEpisode) -> str:
        approval = (
            f" approved-by {episode.approved_by_human}"
            if episode.approved_by_human
            else " auto"
        )
        return (
            f"Mahnverfahren {episode.case_id} {episode.stage_from}->{episode.stage_to}: "
            f"{episode.action} via {episode.channel} ({episode.outstanding_eur:.2f} EUR){approval}"
        )

    async def write_action(
        self,
        *,
        case_id: str,
        kunde_id: str,
        outstanding_eur: float,
        stage_from: Literal["pre_dunning", "soft_reminder", "formal_dunning", "legal", "written_off"],
        stage_to: Literal["pre_dunning", "soft_reminder", "formal_dunning", "legal", "written_off"],
        action: Literal["letter_sent", "phone_attempted", "phone_completed", "escalated", "settled", "written_off"],
        channel: Literal["email", "post", "phone", "internal"],
        drafted_by_agent: str,
        occurred_at: datetime,
        correlation_id: str,
        approved_by_human: str | None = None,
        settlement_amount_eur: float | None = None,
        notes: str = "",
    ) -> str:
        episode = MahnverfahrenEpisode(
            case_id=case_id,
            kunde_id=kunde_id,
            outstanding_eur=outstanding_eur,
            stage_from=stage_from,
            stage_to=stage_to,
            action=action,
            channel=channel,
            drafted_by_agent=drafted_by_agent,
            approved_by_human=approved_by_human,
            settlement_amount_eur=settlement_amount_eur,
            notes=notes,
            occurred_at=occurred_at,
            correlation_id=correlation_id,
            summary=f"Mahnverfahren {case_id} {action}",
        )
        return await self.write(episode)
