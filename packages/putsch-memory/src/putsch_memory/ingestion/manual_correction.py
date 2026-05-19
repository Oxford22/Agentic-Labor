"""Manual correction pipeline.

The Sachbearbeiter UI calls this when a human corrects a fact. The
contract is:

* Justification is mandatory (>= 8 chars, free-text but non-empty).
* The correction is written as a NEW fact, never an overwrite.
* `business_time_from` may be in the past — this is a backdated
  correction. We store both the world time (when the corrected value
  was actually true) and the system time (when the human wrote it).
* The prior fact is `SUPERSEDED_BY` the correction, and a
  `CORRECTION_OF` edge is added so audit replay can tell the two
  apart.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import TYPE_CHECKING, Any

from pydantic import BaseModel, ConfigDict, Field

from putsch_memory.graphiti_client import ProvenanceContext
from putsch_memory.logging import get_logger
from putsch_memory.ontology import SourceSystem

if TYPE_CHECKING:
    from putsch_memory.graphiti_client import MemoryClient

logger = get_logger(__name__)


class ManualCorrection(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    entity_id: str = Field(min_length=2, max_length=128)
    attribute: str = Field(min_length=1, max_length=64)
    new_value: Any
    business_time_from: datetime = Field(description="The world-time the correction takes effect.")
    sachbearbeiter_id: str = Field(min_length=2, max_length=64)
    justification: str = Field(min_length=8, max_length=2048)
    trace_id: str = Field(min_length=2, max_length=128)


class CorrectionResult(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    entity_id: str
    new_fact_id: str
    prior_fact_id: str | None
    is_backdated: bool


async def apply_manual_correction(
    client: MemoryClient, correction: ManualCorrection
) -> CorrectionResult:
    """Apply the correction. The Sachbearbeiter UI is responsible for
    showing the user what they are overwriting, including the source of
    the prior value.
    """
    now = datetime.now(tz=timezone.utc)
    is_backdated = correction.business_time_from < now
    log = logger.bind(
        op="manual_correction",
        entity_id=correction.entity_id,
        attribute=correction.attribute,
        is_backdated=is_backdated,
    )

    # Pull the currently-valid fact to record as superseded.
    prior_rows = await client._run_cypher(  # noqa: SLF001
        """
        MATCH (n {id: $id})
        WHERE n.business_time_to IS NULL
        RETURN n.id AS id, n[$attr] AS value
        LIMIT 1
        """,
        {"id": correction.entity_id, "attr": correction.attribute},
    )
    prior_fact_id = prior_rows[0]["id"] if prior_rows else None
    prior_value = prior_rows[0].get("value") if prior_rows else None

    async with ProvenanceContext(
        source_system=SourceSystem.MANUAL,
        written_by_agent=f"sachbearbeiter:{correction.sachbearbeiter_id}",
        trace_id=correction.trace_id,
        default_confidence=1.0,
    ):
        await client._run_cypher(  # noqa: SLF001
            """
            MATCH (n {id: $id})
            WHERE n.business_time_to IS NULL
            SET n.business_time_to = datetime($now),
                n.system_time_to   = datetime($now)
            CREATE (n2:Fact)
            SET n2 = properties(n),
                n2.business_time_from = datetime($bt),
                n2.business_time_to   = NULL,
                n2.system_time_from   = datetime($now),
                n2.system_time_to     = NULL,
                n2[$attr]             = $val,
                n2.source_system      = $src,
                n2.written_by_agent   = $by,
                n2.written_at_trace_id = $cor,
                n2.justification      = $jx,
                n2.confidence         = 1.0,
                n2.is_correction      = $backdated
            WITH n, n2
            CALL apoc.create.addLabels(n2, [head([l IN labels(n) WHERE l <> 'Fact'])]) YIELD node
            MERGE (n)-[:SUPERSEDED_BY]->(n2)
            MERGE (n)-[:CORRECTION_OF]->(n2)
            RETURN n2.id AS new_id
            """,
            {
                "id": correction.entity_id,
                "now": now.isoformat(),
                "bt": correction.business_time_from.isoformat(),
                "attr": correction.attribute,
                "val": correction.new_value,
                "src": SourceSystem.MANUAL.value,
                "by": f"sachbearbeiter:{correction.sachbearbeiter_id}",
                "cor": correction.trace_id,
                "jx": correction.justification,
                "backdated": is_backdated,
            },
            fetch=False,
        )

    log.warning(
        "manual_correction_applied",
        prior_value=prior_value,
        new_value=correction.new_value,
        sachbearbeiter=correction.sachbearbeiter_id,
    )

    return CorrectionResult(
        entity_id=correction.entity_id,
        new_fact_id=correction.entity_id,  # ID stays stable; the validity row changes.
        prior_fact_id=prior_fact_id,
        is_backdated=is_backdated,
    )
