"""Nightly SAP master-data ingestion.

Driven from the SAP-MCP server (separate component). This module owns:

1. Pulling the current master-data snapshot from one SAP instance.
2. Diffing it against the currently-valid facts in the graph for that
   source_system.
3. Writing only the changed entities; for those, supersede the prior
   currently-valid fact and write the new one.

Diffing is critical — without it, every nightly run rewrites every
entity and the audit trail becomes noise. With it, the audit trail
shows actual changes.

This is the safe place to live with the entity-resolution complexity:
the same vendor in three SAPs gets *three* graph rows (one per source),
linked via `RECONCILES_WITH` after the human confirms they are the same
vendor. We do not deduplicate at ingest time; deduplication is a
separate, explicit, conflict-mediated step.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Any

from putsch_memory.graphiti_client import ProvenanceContext
from putsch_memory.logging import get_logger
from putsch_memory.ontology import (
    Fact,
    Lieferant,
    Material,
    Provenance,
    SourceSystem,
    open_window,
)

if TYPE_CHECKING:
    from putsch_memory.graphiti_client import MemoryClient

logger = get_logger(__name__)


@dataclass(slots=True, frozen=True)
class SAPMasterSnapshot:
    source: SourceSystem
    vendors: list[dict[str, Any]]
    materials: list[dict[str, Any]]
    snapshot_at: datetime
    trace_id: str


async def ingest_sap_master_data(
    client: MemoryClient,
    snapshot: SAPMasterSnapshot,
) -> dict[str, int]:
    """Diff-then-write. Returns counts of vendors/materials actually written."""
    log = logger.bind(op="ingest_sap_master_data", source=snapshot.source.value)
    written_vendors = 0
    written_materials = 0

    async with ProvenanceContext(
        source_system=snapshot.source,
        written_by_agent=f"sap_sync/{snapshot.source.value}",
        trace_id=snapshot.trace_id,
    ):
        async for batch in _chunks(_vendors_to_facts(snapshot), 200):
            changed = await _filter_to_changed(client, batch)
            if not changed:
                continue
            await client.bulk_ingest(changed)
            written_vendors += len(changed)

        async for batch in _chunks(_materials_to_facts(snapshot), 200):
            changed = await _filter_to_changed(client, batch)
            if not changed:
                continue
            await client.bulk_ingest(changed)
            written_materials += len(changed)

    log.info("sap_ingestion_done", vendors=written_vendors, materials=written_materials)
    return {"vendors": written_vendors, "materials": written_materials}


def _vendors_to_facts(snap: SAPMasterSnapshot) -> list[Lieferant]:
    out: list[Lieferant] = []
    for v in snap.vendors:
        # Defensive: SAP exports can have whitespace + casing drift on USt-IdNr.
        ust = str(v["ust_id_nr"]).replace(" ", "").upper()
        out.append(
            Lieferant(
                id=Lieferant.make_id(ust_id_nr=ust),
                name=v["name"],
                legal_name=v.get("legal_name"),
                ust_id_nr=ust,
                hrb_nummer=v.get("hrb_nummer"),
                duns=v.get("duns"),
                sap_vendor_numbers={snap.source: v["sap_vendor_number"]},
                primary_address=v.get("primary_address"),
                bank_iban=v.get("bank_iban"),
                payment_terms_days=v.get("payment_terms_days"),
                is_critical=bool(v.get("is_critical", False)),
                validity=open_window(),
                provenance=Provenance(
                    source_system=snap.source,
                    source_id=v["sap_vendor_number"],
                    written_by_agent=f"sap_sync/{snap.source.value}",
                    written_at_trace_id=snap.trace_id,
                    confidence=1.0,
                ),
            )
        )
    return out


def _materials_to_facts(snap: SAPMasterSnapshot) -> list[Material]:
    out: list[Material] = []
    for m in snap.materials:
        out.append(
            Material(
                id=Material.make_id(sap_material_number=m["sap_material_number"]),
                sap_material_number=m["sap_material_number"],
                description=m["description"],
                hs_code=m.get("hs_code"),
                unit_of_measure=m.get("unit_of_measure", "ST"),
                list_price_eur=m.get("list_price_eur"),
                validity=open_window(),
                provenance=Provenance(
                    source_system=snap.source,
                    source_id=m["sap_material_number"],
                    written_by_agent=f"sap_sync/{snap.source.value}",
                    written_at_trace_id=snap.trace_id,
                    confidence=1.0,
                ),
            )
        )
    return out


async def _filter_to_changed(client: MemoryClient, batch: list[Fact]) -> list[Fact]:
    """Return only the facts that differ from what's currently in the graph
    for the same (id, source_system)."""
    if not batch:
        return []
    now = datetime.now(tz=timezone.utc)
    ids = [f.id for f in batch]
    sources = list({f.provenance.source_system.value for f in batch})
    rows = await client._run_cypher(  # noqa: SLF001
        """
        MATCH (n)
        WHERE n.id IN $ids
          AND n.source_system IN $sources
          AND n.business_time_to IS NULL
        RETURN n.id AS id,
               n.idempotency_key AS idem
        """,
        {"ids": ids, "sources": sources},
    )
    existing_idems = {r["id"]: r.get("idem") for r in rows}

    changed: list[Fact] = []
    for f in batch:
        from putsch_memory.ontology import make_idempotency_key

        idem = make_idempotency_key(
            source_system=f.provenance.source_system,
            source_id=f.id,
            event_time=now,
        )
        if existing_idems.get(f.id) != idem:
            changed.append(f)
    return changed


async def _chunks(items: list[Fact], size: int) -> AsyncIterator[list[Fact]]:
    for i in range(0, len(items), size):
        yield items[i : i + size]
