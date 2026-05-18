"""Vendor lookup.

Returns the current master data plus the full validity-window history of
attribute changes (payment terms, addresses, bank details, ownership).

Used by:
* AP Crew, before routing an invoice
* Stammdaten Crew, for cross-subsidiary reconciliation
* Mahnverfahren swarm (vendor side: dispute / credit memo cases)
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import TYPE_CHECKING, Any

from pydantic import BaseModel, ConfigDict, Field, model_validator

from putsch_memory.logging import get_logger
from putsch_memory.ontology import Confidence, Lieferant, UStIdNr

if TYPE_CHECKING:
    from putsch_memory.graphiti_client import MemoryClient

logger = get_logger(__name__)


class VendorLookupInput(BaseModel):
    """Input contract for the vendor lookup. Either USt-IdNr or name+country."""

    model_config = ConfigDict(extra="forbid")

    ust_id_nr: UStIdNr | None = None
    name: str | None = Field(default=None, min_length=2, max_length=256)
    country_hint: str | None = Field(default=None, min_length=2, max_length=2)
    as_of: datetime | None = Field(
        default=None,
        description="Business time to resolve as of. Defaults to 'now'.",
    )
    include_history: bool = Field(default=True)
    max_history_items: int = Field(default=20, ge=1, le=100)

    @model_validator(mode="after")
    def _need_one_key(self) -> VendorLookupInput:
        if self.ust_id_nr is None and self.name is None:
            raise ValueError("Provide either ust_id_nr or name.")
        return self


class VendorAttributeChange(BaseModel):
    attribute: str
    old_value: Any
    new_value: Any
    changed_at: datetime
    source_system: str
    written_by_agent: str
    confidence: Confidence


class VendorLookupResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    found: bool
    vendor_id: str | None = None
    current: dict[str, Any] | None = None
    history: list[VendorAttributeChange] = Field(default_factory=list)
    cross_subsidiary_aliases: list[dict[str, str]] = Field(
        default_factory=list,
        description="If the same vendor exists under different SAP numbers across subsidiaries.",
    )
    as_of: datetime
    memory_degraded: bool = False


async def lookup_vendor(
    client: MemoryClient,
    *,
    ust_id_nr: str | None = None,
    name: str | None = None,
    country_hint: str | None = None,
    as_of: datetime | None = None,
    include_history: bool = True,
    max_history_items: int = 20,
) -> VendorLookupResult:
    inp = VendorLookupInput(
        ust_id_nr=ust_id_nr,
        name=name,
        country_hint=country_hint,
        as_of=as_of,
        include_history=include_history,
        max_history_items=max_history_items,
    )
    resolved_as_of = inp.as_of or datetime.now(tz=timezone.utc)

    log = logger.bind(op="lookup_vendor", as_of=resolved_as_of.isoformat())

    if inp.ust_id_nr is not None:
        vendor_id = Lieferant.make_id(ust_id_nr=inp.ust_id_nr)
        current = await client.as_of(vendor_id, business_time=resolved_as_of)
        if current is None:
            log.info("vendor_not_found", ust_id_nr=inp.ust_id_nr)
            return VendorLookupResult(found=False, as_of=resolved_as_of)
    else:
        # Soft lookup by name — uses Graphiti's hybrid search.
        candidates = await client.search(
            f"Lieferant name:{inp.name} country:{inp.country_hint or ''}",
            as_of=resolved_as_of,
            labels=("Lieferant",),
            max_results=5,
        )
        if not candidates:
            log.info("vendor_not_found", name=inp.name)
            return VendorLookupResult(found=False, as_of=resolved_as_of)
        current = candidates[0]
        vendor_id = current.get("id", "")

    history: list[VendorAttributeChange] = []
    if inp.include_history and vendor_id:
        raw_history = await client.get_entity_history(vendor_id)
        history = _diff_history(raw_history)[: inp.max_history_items]

    aliases = await _find_cross_subsidiary_aliases(client, vendor_id)

    log.info("vendor_found", vendor_id=vendor_id, history_n=len(history), aliases=len(aliases))
    return VendorLookupResult(
        found=True,
        vendor_id=vendor_id,
        current=current,
        history=history,
        cross_subsidiary_aliases=aliases,
        as_of=resolved_as_of,
    )


async def _find_cross_subsidiary_aliases(
    client: MemoryClient, vendor_id: str
) -> list[dict[str, str]]:
    rows = await client._run_cypher(  # noqa: SLF001 — same-package internal
        """
        MATCH (v:Lieferant {id: $id})-[:RECONCILES_WITH]-(other:Lieferant)
        RETURN other.id AS id,
               other.name AS name,
               other.provenance_source_system AS source_system
        """,
        {"id": vendor_id},
    )
    return [{"id": r["id"], "name": r.get("name", ""), "source_system": r.get("source_system", "")} for r in rows]


def _diff_history(raw_history: list[dict[str, Any]]) -> list[VendorAttributeChange]:
    """Compute attribute-level diffs across successive versions of an entity."""
    out: list[VendorAttributeChange] = []
    tracked = ("payment_terms_days", "primary_address", "bank_iban", "is_critical", "name")
    for prev, curr in zip(raw_history, raw_history[1:], strict=False):
        for attr in tracked:
            if prev.get(attr) != curr.get(attr):
                out.append(
                    VendorAttributeChange(
                        attribute=attr,
                        old_value=prev.get(attr),
                        new_value=curr.get(attr),
                        changed_at=datetime.fromisoformat(curr.get("business_time_from"))
                        if curr.get("business_time_from")
                        else datetime.now(tz=timezone.utc),
                        source_system=curr.get("source_system", "unknown"),
                        written_by_agent=curr.get("written_by_agent", "unknown"),
                        confidence=float(curr.get("confidence", 1.0)),
                    )
                )
    return out


# ---------------------------------------------------------------------------
# CrewAI Tool adapter
# ---------------------------------------------------------------------------


def as_crewai_tool(client: MemoryClient) -> Any:  # pragma: no cover — optional dep
    from crewai.tools import BaseTool  # type: ignore[import-not-found]

    class _LookupVendorTool(BaseTool):
        name: str = "lookup_vendor"
        description: str = (
            "Look up a Putsch vendor (Lieferant) by USt-IdNr or name. "
            "Returns current master data and history of changes."
        )

        async def _arun(self, **kwargs: Any) -> str:
            result = await lookup_vendor(client, **kwargs)
            return result.model_dump_json()

        def _run(self, **kwargs: Any) -> str:
            import asyncio

            return asyncio.run(self._arun(**kwargs))

    return _LookupVendorTool()


# ---------------------------------------------------------------------------
# LangGraph node adapter
# ---------------------------------------------------------------------------


def as_langgraph_node(client: MemoryClient) -> Any:  # pragma: no cover — optional dep
    async def _node(state: dict[str, Any]) -> dict[str, Any]:
        result = await lookup_vendor(client, **state.get("lookup_vendor_input", {}))
        return {"lookup_vendor_output": result.model_dump()}

    return _node
