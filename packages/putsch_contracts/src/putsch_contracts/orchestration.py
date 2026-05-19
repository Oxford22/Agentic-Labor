"""Orchestration contracts.

Shapes that flow between LangGraph's durable runtime, the CrewAI crews,
and the Magentic-One-pattern swarm. ``TaskLedger`` / ``ProgressLedger``
mirror the paper; we keep them in contracts so any worker spec can
type its dispatch.
"""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from enum import StrEnum
from typing import Annotated, Any
from uuid import UUID, uuid4

from pydantic import BaseModel, ConfigDict, Field, StringConstraints


class WorkflowStatus(StrEnum):
    PENDING = "pending"
    RUNNING = "running"
    AWAITING_HUMAN = "awaiting_human"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"


_WorkflowName = Annotated[
    str,
    StringConstraints(pattern=r"^[a-z][a-z0-9_]{1,62}$"),
]


class HumanReviewRequest(BaseModel):
    """A ``LangGraph.interrupt()`` payload, awaiting a Sachbearbeiter.

    Built by an orchestrator node when policy demands human-in-the-loop
    (e.g. invoice > €10k per ARCHITECTURE.md week 3 plan). The
    ``decision_options`` list constrains what the UI may submit back so
    free-text decisions cannot bypass policy.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    request_id: UUID = Field(default_factory=uuid4)
    workflow: _WorkflowName
    correlation_id: str = Field(min_length=8, max_length=128)
    summary: str = Field(min_length=1, max_length=2048)
    payload: dict[str, Any] = Field(default_factory=dict)
    decision_options: list[str] = Field(min_length=1, max_length=8)
    requires_role: str = Field(min_length=1, max_length=64)
    sla_deadline: datetime | None = None
    estimated_value_eur: Decimal | None = Field(default=None, ge=0, max_digits=14, decimal_places=2)


class TaskLedger(BaseModel):
    """The Magentic-One outer-loop state.

    ``facts``, ``guesses``, and ``plan`` are the three slots the paper
    keeps separate. The orchestrator may rewrite ``plan`` on a replan
    but must preserve already-asserted facts.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    facts: list[str] = Field(default_factory=list, max_length=64)
    guesses: list[str] = Field(default_factory=list, max_length=64)
    plan: list[str] = Field(default_factory=list, max_length=64)
    replan_count: int = Field(ge=0, default=0)


class WorkflowState(BaseModel):
    """The durable state a LangGraph checkpointer rehydrates.

    ``checkpoint_id`` is the row Postgres holds; ``last_node`` is the
    node to resume at; ``ledger`` carries the orchestrator's outer
    loop. Only enough is modelled here to type a checkpoint write —
    the runtime owns the rest.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    workflow: _WorkflowName
    workflow_run_id: UUID = Field(default_factory=uuid4)
    status: WorkflowStatus = WorkflowStatus.PENDING
    checkpoint_id: str = Field(min_length=1, max_length=128)
    last_node: str = Field(min_length=1, max_length=128)
    ledger: TaskLedger = Field(default_factory=TaskLedger)
    pending_review: HumanReviewRequest | None = None
    started_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    updated_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
