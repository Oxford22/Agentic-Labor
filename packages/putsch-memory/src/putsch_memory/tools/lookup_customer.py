"""Customer lookup with payment behavior + escalation history.

Read before the Mahnverfahren swarm drafts any communication. The tone
of a dunning letter must reflect the relationship history, not just the
current overdue amount. A customer that has paid every invoice on time
for ten years and slipped once should not get the same letter as a
serial offender.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import TYPE_CHECKING, Any

from pydantic import BaseModel, ConfigDict, Field, model_validator

from putsch_memory.logging import get_logger
from putsch_memory.ontology import Kunde, UStIdNr

if TYPE_CHECKING:
    from putsch_memory.graphiti_client import MemoryClient

logger = get_logger(__name__)


class CustomerLookupInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    ust_id_nr: UStIdNr | None = None
    name: str | None = Field(default=None, min_length=2, max_length=256)
    as_of: datetime | None = None
    payment_behavior_window_days: int = Field(default=730, ge=30, le=365 * 5)

    @model_validator(mode="after")
    def _need_one_key(self) -> CustomerLookupInput:
        if self.ust_id_nr is None and self.name is None:
            raise ValueError("Provide either ust_id_nr or name.")
        return self


class PaymentBehaviorSummary(BaseModel):
    invoices_total: int = 0
    invoices_paid_on_time: int = 0
    invoices_paid_late: int = 0
    invoices_unpaid: int = 0
    average_days_to_pay: float | None = None
    longest_overdue_days: int | None = None


class EscalationEvent(BaseModel):
    case_id: str
    stage: str
    opened_at: datetime
    closed_at: datetime | None = None
    outstanding_eur: float


class CustomerLookupResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    found: bool
    customer_id: str | None = None
    current: dict[str, Any] | None = None
    payment_behavior: PaymentBehaviorSummary
    escalation_history: list[EscalationEvent] = Field(default_factory=list)
    recommended_tone: str = Field(
        default="neutral",
        description="One of: friendly, neutral, formal, firm, legal. Heuristic only — not a directive.",
    )
    as_of: datetime


async def lookup_customer(
    client: MemoryClient,
    *,
    ust_id_nr: str | None = None,
    name: str | None = None,
    as_of: datetime | None = None,
    payment_behavior_window_days: int = 730,
) -> CustomerLookupResult:
    inp = CustomerLookupInput(
        ust_id_nr=ust_id_nr,
        name=name,
        as_of=as_of,
        payment_behavior_window_days=payment_behavior_window_days,
    )
    resolved = inp.as_of or datetime.now(tz=timezone.utc)
    window_start = resolved - timedelta(days=inp.payment_behavior_window_days)
    log = logger.bind(op="lookup_customer", as_of=resolved.isoformat())

    if inp.ust_id_nr is not None:
        customer_id = Kunde.make_id(ust_id_nr=inp.ust_id_nr)
        current = await client.as_of(customer_id, business_time=resolved)
    else:
        candidates = await client.search(
            f"Kunde name:{inp.name}",
            as_of=resolved,
            labels=("Kunde",),
            max_results=5,
        )
        if not candidates:
            return CustomerLookupResult(
                found=False, payment_behavior=PaymentBehaviorSummary(), as_of=resolved
            )
        current = candidates[0]
        customer_id = current.get("id", "")

    if current is None:
        log.info("customer_not_found", input=inp.model_dump())
        return CustomerLookupResult(found=False, payment_behavior=PaymentBehaviorSummary(), as_of=resolved)

    pb = await _payment_behavior(client, customer_id, window_start, resolved)
    escalations = await _escalation_history(client, customer_id, window_start)
    tone = _recommend_tone(pb, escalations)

    log.info(
        "customer_found",
        customer_id=customer_id,
        on_time=pb.invoices_paid_on_time,
        late=pb.invoices_paid_late,
        unpaid=pb.invoices_unpaid,
        tone=tone,
    )
    return CustomerLookupResult(
        found=True,
        customer_id=customer_id,
        current=current,
        payment_behavior=pb,
        escalation_history=escalations,
        recommended_tone=tone,
        as_of=resolved,
    )


async def _payment_behavior(
    client: MemoryClient, customer_id: str, window_start: datetime, window_end: datetime
) -> PaymentBehaviorSummary:
    rows = await client._run_cypher(  # noqa: SLF001
        """
        MATCH (r:Rechnung {direction: 'outgoing'})
        WHERE r.issuing_party = $cust
          AND r.issued_at >= datetime($from)
          AND r.issued_at <= datetime($to)
        RETURN
          count(*) AS total,
          sum(CASE WHEN r.paid_at IS NOT NULL AND r.due_at IS NOT NULL
                   AND r.paid_at <= r.due_at THEN 1 ELSE 0 END) AS on_time,
          sum(CASE WHEN r.paid_at IS NOT NULL AND r.due_at IS NOT NULL
                   AND r.paid_at > r.due_at THEN 1 ELSE 0 END) AS late,
          sum(CASE WHEN r.paid_at IS NULL THEN 1 ELSE 0 END) AS unpaid,
          avg(CASE WHEN r.paid_at IS NOT NULL
                   THEN duration.between(r.issued_at, r.paid_at).days END) AS avg_days,
          max(CASE WHEN r.paid_at IS NULL
                   THEN duration.between(r.due_at, datetime($to)).days END) AS longest_overdue
        """,
        {"cust": customer_id, "from": window_start.isoformat(), "to": window_end.isoformat()},
    )
    if not rows:
        return PaymentBehaviorSummary()
    r = rows[0]
    return PaymentBehaviorSummary(
        invoices_total=int(r.get("total") or 0),
        invoices_paid_on_time=int(r.get("on_time") or 0),
        invoices_paid_late=int(r.get("late") or 0),
        invoices_unpaid=int(r.get("unpaid") or 0),
        average_days_to_pay=float(r["avg_days"]) if r.get("avg_days") is not None else None,
        longest_overdue_days=int(r["longest_overdue"]) if r.get("longest_overdue") is not None else None,
    )


async def _escalation_history(
    client: MemoryClient, customer_id: str, window_start: datetime
) -> list[EscalationEvent]:
    rows = await client._run_cypher(  # noqa: SLF001
        """
        MATCH (m:Mahnverfahren {kunde_id: $cust})
        WHERE m.opened_at >= datetime($from)
        RETURN m.case_id AS case_id,
               m.stage AS stage,
               m.opened_at AS opened_at,
               m.closed_at AS closed_at,
               m.outstanding_eur AS outstanding_eur
        ORDER BY m.opened_at DESC
        LIMIT 50
        """,
        {"cust": customer_id, "from": window_start.isoformat()},
    )
    out: list[EscalationEvent] = []
    for r in rows:
        out.append(
            EscalationEvent(
                case_id=r["case_id"],
                stage=r["stage"],
                opened_at=_iso(r["opened_at"]),
                closed_at=_iso(r.get("closed_at")),
                outstanding_eur=float(r["outstanding_eur"]),
            )
        )
    return out


def _recommend_tone(pb: PaymentBehaviorSummary, escalations: list[EscalationEvent]) -> str:
    """Heuristic only — NOT a directive. Human still authors the letter."""
    if any(e.stage == "legal" for e in escalations):
        return "legal"
    if pb.invoices_unpaid >= 3 or any(e.stage == "formal_dunning" for e in escalations):
        return "firm"
    if pb.invoices_paid_late > pb.invoices_paid_on_time:
        return "formal"
    if pb.invoices_total >= 10 and pb.invoices_paid_late <= 1:
        return "friendly"
    return "neutral"


def _iso(v: Any) -> datetime:
    if isinstance(v, datetime):
        return v if v.tzinfo else v.replace(tzinfo=timezone.utc)
    if isinstance(v, str):
        return datetime.fromisoformat(v)
    raise TypeError(f"cannot coerce {v!r} to datetime")
