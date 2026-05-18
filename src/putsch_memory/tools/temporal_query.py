"""Temporal point-query — "what did we know about X on date Y".

This is the audit-replay tool. The reconstruction_accuracy eval lives
or dies by this function returning bit-identical answers to what the
agent would have seen in production at `as_of_system_time`.

Two query modes:

* **business_time only:** "What was true on date Y?" — useful for
  reasoning about the past world, not for audit replay.
* **bitemporal:** "What did the system believe on system_time S was
  true on business_time B?" — the EU AI Act Art. 12 mode.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import TYPE_CHECKING, Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from putsch_memory.logging import get_logger

if TYPE_CHECKING:
    from putsch_memory.graphiti_client import MemoryClient

logger = get_logger(__name__)


class TemporalQueryInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    entity_id: str = Field(min_length=2, max_length=128)
    business_time: datetime = Field(description="The world-time the question is about.")
    system_time: datetime | None = Field(
        default=None,
        description="Audit-replay mode: system-time the answer must reflect.",
    )
    mode: Literal["world", "audit"] = "world"

    @model_validator(mode="after")
    def _coherent(self) -> TemporalQueryInput:
        if self.mode == "audit" and self.system_time is None:
            raise ValueError("audit mode requires system_time")
        if self.business_time.tzinfo is None or (
            self.system_time is not None and self.system_time.tzinfo is None
        ):
            raise ValueError("temporal_query times must be timezone-aware (UTC).")
        return self


class TemporalQueryResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    found: bool
    mode: Literal["world", "audit"]
    business_time: datetime
    system_time: datetime | None
    fact: dict[str, Any] | None
    provenance: dict[str, Any] | None


async def temporal_query(
    client: MemoryClient,
    *,
    entity_id: str,
    business_time: datetime,
    system_time: datetime | None = None,
    mode: Literal["world", "audit"] = "world",
) -> TemporalQueryResult:
    inp = TemporalQueryInput(
        entity_id=entity_id, business_time=business_time, system_time=system_time, mode=mode
    )
    log = logger.bind(op="temporal_query", mode=inp.mode, entity_id=entity_id)

    if inp.mode == "world":
        fact = await client.as_of(inp.entity_id, business_time=inp.business_time)
    else:
        assert inp.system_time is not None
        fact = await client.as_of_with_system_time(
            inp.entity_id, business_time=inp.business_time, system_time=inp.system_time
        )

    if fact is None:
        log.info("temporal_query_miss")
        return TemporalQueryResult(
            found=False,
            mode=inp.mode,
            business_time=inp.business_time,
            system_time=inp.system_time,
            fact=None,
            provenance=None,
        )

    log.info("temporal_query_hit", id=fact.get("id"))
    return TemporalQueryResult(
        found=True,
        mode=inp.mode,
        business_time=inp.business_time,
        system_time=inp.system_time,
        fact=fact,
        provenance={
            "source_system": fact.get("source_system"),
            "source_id": fact.get("source_id"),
            "written_by_agent": fact.get("written_by_agent"),
            "written_at_trace_id": fact.get("written_at_trace_id"),
            "confidence": fact.get("confidence"),
        },
    )
