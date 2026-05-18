"""Cross-source conflict detection and storage.

Putsch's master data lives in six SAP instances + DATEV + agent
inferences. Disagreements between them are *normal*; the wrong response
is "pick one and hope". The right response is:

1. Detect the disagreement at write time.
2. Store both facts intact, each tagged with its `source_system`.
3. Materialize the disagreement as a `CONFLICTS_WITH` edge between the
   two facts so it shows up in the Stammdaten queue.
4. Never auto-resolve. A human picks the winner, the resolution is
   written as a new fact with `source_system = RECONCILED_FACT` that
   points at both losing facts via `SUPERSEDED_BY`.

The "no auto-resolve" rule is non-negotiable. The system that
auto-resolves master-data conflicts is the system that quietly corrupts
master data for months before anyone notices.
"""

from __future__ import annotations

import hashlib
from collections.abc import Mapping
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Any

from pydantic import BaseModel, ConfigDict, Field

from putsch_memory.exceptions import ConflictDetected
from putsch_memory.logging import get_logger

if TYPE_CHECKING:
    from putsch_memory.graphiti_client import MemoryClient

logger = get_logger(__name__)


class ConflictRecord(BaseModel):
    """The persisted conflict — written into the graph as its own node."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    conflict_id: str
    entity_id: str
    entity_label: str
    attribute: str
    competing_values: dict[str, Any] = Field(
        description="Map of source_system -> value. Length >= 2."
    )
    detected_at: datetime
    correlation_id: str
    status: str = "open"


async def maybe_record_conflict(
    client: MemoryClient,
    *,
    entity_id: str,
    entity_label: str,
    asserted_at: datetime,
    asserting_source: str,
    asserted_attributes: Mapping[str, Any],
    correlation_id: str,
) -> list[ConflictRecord]:
    """Compare the asserted attributes against currently valid facts.

    For each attribute where another source currently asserts a
    different non-null value, emit a ConflictRecord and persist a
    `CONFLICTS_WITH` edge. Idempotent: the same disagreement only
    creates one open conflict record.
    """
    log = logger.bind(
        op="conflict_check",
        entity_id=entity_id,
        asserting_source=asserting_source,
    )

    cypher = """
        MATCH (n {id: $eid})
        WHERE n.business_time_to IS NULL
          AND n.source_system <> $src
        RETURN n.source_system AS source,
               n { .* } AS attrs
    """
    rows = await client._run_cypher(cypher, {"eid": entity_id, "src": asserting_source})  # noqa: SLF001

    out: list[ConflictRecord] = []
    for row in rows:
        other_source: str = row["source"]
        other_attrs: dict[str, Any] = row["attrs"]
        for attr, asserted in asserted_attributes.items():
            other = other_attrs.get(attr)
            if other is None or asserted is None:
                continue
            if _canonical(other) == _canonical(asserted):
                continue
            record = ConflictRecord(
                conflict_id=_conflict_id(entity_id, attr, asserting_source, other_source),
                entity_id=entity_id,
                entity_label=entity_label,
                attribute=attr,
                competing_values={asserting_source: asserted, other_source: other},
                detected_at=asserted_at,
                correlation_id=correlation_id,
            )
            await _persist_conflict(client, record)
            out.append(record)
            log.warning(
                "conflict_recorded",
                attribute=attr,
                competing_sources=[asserting_source, other_source],
            )

    return out


async def resolve_conflict(
    client: MemoryClient,
    *,
    conflict_id: str,
    winning_source: str,
    winning_value: Any,
    resolved_by: str,
    justification: str,
) -> None:
    """Write the human's reconciliation decision as a new fact.

    The losing facts are NOT deleted — they remain as historical records
    so the audit trail is intact. The resolution edge is what makes the
    winning value the one a fresh query sees.
    """
    if len(justification) < 8:
        raise ValueError("conflict resolution requires a substantive justification.")
    now = datetime.now(tz=timezone.utc)
    await client._run_cypher(  # noqa: SLF001
        """
        MATCH (c:_Conflict {conflict_id: $cid})
        SET c.status = 'resolved',
            c.resolved_at = datetime($now),
            c.resolved_by = $by,
            c.winning_source = $ws,
            c.winning_value = $wv,
            c.justification = $jx
        WITH c
        MATCH (loser {id: c.entity_id, source_system: c.competing_values_keys_other})
        MERGE (loser)-[:SUPERSEDED_BY {by_source: $ws, by: $by}]->(c)
        RETURN c.conflict_id AS cid
        """,
        {
            "cid": conflict_id,
            "now": now.isoformat(),
            "by": resolved_by,
            "ws": winning_source,
            "wv": str(winning_value),
            "jx": justification,
        },
        fetch=False,
    )
    logger.info(
        "conflict_resolved",
        conflict_id=conflict_id,
        winning_source=winning_source,
        resolved_by=resolved_by,
    )


async def _persist_conflict(client: MemoryClient, record: ConflictRecord) -> None:
    await client._run_cypher(  # noqa: SLF001
        """
        MERGE (c:_Conflict {conflict_id: $cid})
        ON CREATE SET c.entity_id = $eid,
                      c.entity_label = $label,
                      c.attribute = $attr,
                      c.competing_values = $vals,
                      c.detected_at = datetime($at),
                      c.correlation_id = $cor,
                      c.status = 'open'
        WITH c
        MATCH (e {id: $eid})
        MERGE (e)-[:CONFLICTS_WITH]->(c)
        RETURN c
        """,
        {
            "cid": record.conflict_id,
            "eid": record.entity_id,
            "label": record.entity_label,
            "attr": record.attribute,
            "vals": record.competing_values,
            "at": record.detected_at.isoformat(),
            "cor": record.correlation_id,
        },
        fetch=False,
    )


def _conflict_id(entity_id: str, attribute: str, src_a: str, src_b: str) -> str:
    """Deterministic ID so the same disagreement doesn't pile up."""
    pair = "|".join(sorted([src_a, src_b]))
    raw = f"{entity_id}::{attribute}::{pair}"
    return "conflict:" + hashlib.sha256(raw.encode("utf-8")).hexdigest()[:24]


def _canonical(v: Any) -> str:
    if isinstance(v, str):
        return " ".join(v.split()).casefold()
    return repr(v)


__all__ = [
    "ConflictDetected",
    "ConflictRecord",
    "maybe_record_conflict",
    "resolve_conflict",
]
