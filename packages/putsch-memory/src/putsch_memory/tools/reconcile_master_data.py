"""Cross-site master-data reconciliation.

Putsch runs SAP at six sites. The "same" vendor often exists three
times under three vendor numbers with three slightly different
addresses. This tool surfaces the disagreement so a Sachbearbeiter can
resolve it (either confirm "yes, same vendor, link them" or "no,
different vendor, keep separate").

The agent never auto-resolves. Conflicts are stored as facts of their
own (see conflicts.py), and humans pick the winner.
"""

from __future__ import annotations

from collections import defaultdict
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from putsch_memory.logging import get_logger

if TYPE_CHECKING:
    from putsch_memory.graphiti_client import MemoryClient

logger = get_logger(__name__)


class ReconcileInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    entity_type: Literal["Lieferant", "Kunde", "Material"]
    candidate_key: str = Field(
        description="The shared key used to find candidates: USt-IdNr, name, IBAN, SAP material number.",
        min_length=2,
        max_length=256,
    )
    key_kind: Literal["ust_id_nr", "iban", "name", "sap_material_number"] = "ust_id_nr"
    as_of: datetime | None = None


class SiteView(BaseModel):
    """One subsidiary's view of an entity."""

    source_system: str
    source_id: str
    attributes: dict[str, Any]


class AttributeDisagreement(BaseModel):
    attribute: str
    values_by_source: dict[str, Any] = Field(
        description="Map of source_system → value. Length > 1 means a real disagreement."
    )


class ReconcileResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    entity_type: str
    candidate_key: str
    site_views: list[SiteView]
    disagreements: list[AttributeDisagreement]
    requires_human: bool
    as_of: datetime


async def reconcile_master_data(
    client: MemoryClient,
    *,
    entity_type: Literal["Lieferant", "Kunde", "Material"],
    candidate_key: str,
    key_kind: Literal["ust_id_nr", "iban", "name", "sap_material_number"] = "ust_id_nr",
    as_of: datetime | None = None,
) -> ReconcileResult:
    inp = ReconcileInput(
        entity_type=entity_type, candidate_key=candidate_key, key_kind=key_kind, as_of=as_of
    )
    resolved = inp.as_of or datetime.now(tz=timezone.utc)
    log = logger.bind(op="reconcile_master_data", entity=entity_type, key=key_kind)

    cypher = """
        MATCH (n)
        WHERE $label IN labels(n)
          AND n[$key_field] = $key_value
          AND n.business_time_from <= datetime($t)
          AND (n.business_time_to IS NULL OR n.business_time_to > datetime($t))
        RETURN n { .*, labels: labels(n) } AS fact
    """
    rows = await client._run_cypher(  # noqa: SLF001
        cypher,
        {
            "label": inp.entity_type,
            "key_field": inp.key_kind,
            "key_value": inp.candidate_key,
            "t": resolved.isoformat(),
        },
    )

    site_views: list[SiteView] = []
    for row in rows:
        f = row["fact"]
        site_views.append(
            SiteView(
                source_system=f.get("source_system", "unknown"),
                source_id=f.get("source_id", f.get("id", "")),
                attributes={k: v for k, v in f.items() if not k.startswith("system_") and k not in {"labels"}},
            )
        )

    disagreements = _detect_disagreements(site_views)
    requires_human = len(disagreements) > 0

    log.info(
        "reconciliation_summary",
        sites=len(site_views),
        disagreements=len(disagreements),
        requires_human=requires_human,
    )

    return ReconcileResult(
        entity_type=entity_type,
        candidate_key=candidate_key,
        site_views=site_views,
        disagreements=disagreements,
        requires_human=requires_human,
        as_of=resolved,
    )


def _detect_disagreements(views: list[SiteView]) -> list[AttributeDisagreement]:
    """Identify attributes where two or more sites hold different non-null values."""
    by_attr: dict[str, dict[str, Any]] = defaultdict(dict)
    tracked = (
        "name",
        "legal_name",
        "primary_address",
        "bank_iban",
        "payment_terms_days",
        "hs_code",
        "list_price_eur",
        "credit_limit_eur",
    )
    for v in views:
        for attr in tracked:
            val = v.attributes.get(attr)
            if val is not None:
                by_attr[attr][v.source_system] = val

    out: list[AttributeDisagreement] = []
    for attr, by_source in by_attr.items():
        distinct = set(_canonical(x) for x in by_source.values())
        if len(distinct) > 1:
            out.append(AttributeDisagreement(attribute=attr, values_by_source=by_source))
    return out


def _canonical(v: Any) -> str:
    """Light normalization so trivial casing/whitespace doesn't trigger a false conflict."""
    if isinstance(v, str):
        return " ".join(v.split()).casefold()
    return repr(v)
