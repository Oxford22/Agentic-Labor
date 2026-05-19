"""Memory-layer contracts (Graphiti / Neo4j substrate).

What ``putsch-memory`` writes and what every other package reads. The
two non-obvious shapes are ``Provenance`` (no anonymous facts) and the
bitemporal envelope on ``MemoryEpisode`` (``business_time`` vs.
``system_time``) — both are non-negotiable per ADR-005.
"""

from __future__ import annotations

from datetime import UTC, datetime
from enum import StrEnum
from typing import Any, Literal
from uuid import UUID, uuid4

from pydantic import BaseModel, ConfigDict, Field, model_validator


class EpisodeKind(StrEnum):
    """The crew that produced this episode, used for namespacing and routing."""

    AP = "ap"
    SALES = "sales"
    CUSTOMS = "customs"
    MASTER_DATA = "master_data"
    DUNNING = "dunning"
    AUDIT = "audit"


class Provenance(BaseModel):
    """Where a fact came from.

    Required on every write. ``source`` names the SDK boundary (e.g.
    ``putsch-docs``); ``source_id`` is whatever stable handle the source
    has (e.g. invoice number, email Message-ID). Together they form the
    idempotency key for memory writes.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    source: str = Field(min_length=1, max_length=128)
    source_id: str = Field(min_length=1, max_length=256)
    correlation_id: str = Field(min_length=8, max_length=128)
    recorded_by: str = Field(
        min_length=1,
        max_length=128,
        description="The package/agent that initiated the write (audit subject)",
    )
    confidence: float = Field(ge=0.0, le=1.0, default=1.0)


class MemoryEpisode(BaseModel):
    """A bitemporal fact written into Graphiti.

    ``business_time`` is when the fact was true in the world;
    ``system_time`` is when it was written. Both are stored because both
    matter for audit replay (EU AI Act Art. 12). Corrections are *new*
    episodes with a ``correction_of`` reference back, never overwrites.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    episode_id: UUID = Field(default_factory=uuid4)
    kind: EpisodeKind
    payload: dict[str, Any] = Field(default_factory=dict)
    business_time: datetime
    system_time: datetime = Field(default_factory=lambda: datetime.now(UTC))
    provenance: Provenance
    correction_of: UUID | None = None
    superseded_by: UUID | None = None

    @model_validator(mode="after")
    def _check_times(self) -> MemoryEpisode:
        if self.business_time > self.system_time:
            raise ValueError("business_time cannot be after system_time")
        if self.correction_of == self.episode_id:
            raise ValueError("correction_of must reference a different episode")
        return self


class TemporalQuery(BaseModel):
    """Bounded temporal query against the memory graph.

    ``max_depth`` and ``max_results`` are clamped client-side to keep
    unbounded traversals out of the driver. Use ``as_of_business_time``
    to ask "what did we know about X on date D in the real world?";
    ``as_of_system_time`` answers "what was in the graph at moment T?".
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    kind: Literal["vendor", "customer", "account_routing", "raw"]
    entity_key: str = Field(min_length=1, max_length=256)
    as_of_business_time: datetime | None = None
    as_of_system_time: datetime | None = None
    max_depth: int = Field(ge=1, le=5, default=2)
    max_results: int = Field(ge=1, le=200, default=20)
