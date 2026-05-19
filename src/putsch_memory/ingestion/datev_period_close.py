"""Monthly DATEV period-close ingestion.

After Buchhaltung closes a period in DATEV, this pipeline ingests the
booking digest into the graph. The period itself becomes a
`Buchungsperiode` node; every Buchung gets a `BELONGS_TO_PERIOD` edge
into it; once `status='audited'`, downstream agents refuse to write new
facts referencing it (the period is sealed).

Idempotent on the (period, datev_doc_number) pair.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from datetime import datetime
from typing import TYPE_CHECKING, Any

from putsch_memory.graphiti_client import ProvenanceContext
from putsch_memory.logging import get_logger
from putsch_memory.ontology import (
    Buchung,
    Buchungsperiode,
    Provenance,
    SourceSystem,
    open_window,
)

if TYPE_CHECKING:
    from putsch_memory.graphiti_client import MemoryClient

logger = get_logger(__name__)


@dataclass(slots=True, frozen=True)
class DATEVPeriodDigest:
    period: str  # "YYYY-MM"
    status: str  # one of open/soft_closed/closed/audited
    bookings: list[dict[str, Any]]
    audited_by: str | None
    digest_at: datetime
    trace_id: str


async def ingest_datev_period_close(
    client: MemoryClient,
    digest: DATEVPeriodDigest,
) -> int:
    log = logger.bind(op="ingest_datev_period_close", period=digest.period, status=digest.status)

    period_fact = Buchungsperiode(
        id=Buchungsperiode.make_id(period=digest.period),
        period=digest.period,
        status=digest.status,  # type: ignore[arg-type]
        audited_by=digest.audited_by,
        validity=open_window(business_time_from=digest.digest_at),
        provenance=Provenance(
            source_system=SourceSystem.DATEV,
            source_id=digest.period,
            written_by_agent="datev_close_sync",
            written_at_trace_id=digest.trace_id,
            confidence=1.0,
        ),
    )

    bookings = list(_bookings_to_facts(digest))

    async with ProvenanceContext(
        source_system=SourceSystem.DATEV,
        written_by_agent="datev_close_sync",
        trace_id=digest.trace_id,
    ):
        await client.bulk_ingest([period_fact, *bookings])

    # Wire the BELONGS_TO_PERIOD edges in a single batch.
    await client._run_cypher(  # noqa: SLF001
        """
        UNWIND $rows AS row
        MATCH (b:Buchung {id: row.b})
        MATCH (p:Buchungsperiode {id: row.p})
        MERGE (b)-[r:BELONGS_TO_PERIOD]->(p)
        ON CREATE SET r.created_at = datetime()
        """,
        {"rows": [{"b": b.id, "p": period_fact.id} for b in bookings]},
        fetch=False,
    )

    log.info("datev_close_ingested", bookings=len(bookings))
    return len(bookings)


def _bookings_to_facts(digest: DATEVPeriodDigest) -> Iterable[Buchung]:
    for b in digest.bookings:
        yield Buchung(
            id=Buchung.make_id(datev_doc_number=b["datev_doc_number"], period=digest.period),
            datev_doc_number=b["datev_doc_number"],
            period=digest.period,
            debit_konto=b["debit_konto"],
            credit_konto=b["credit_konto"],
            amount_eur=float(b["amount_eur"]),
            text=b.get("text", "")[:512],
            rechnung_id=b.get("rechnung_id"),
            validity=open_window(),
            provenance=Provenance(
                source_system=SourceSystem.DATEV,
                source_id=b["datev_doc_number"],
                written_by_agent="datev_close_sync",
                written_at_trace_id=digest.trace_id,
                confidence=1.0,
            ),
        )
