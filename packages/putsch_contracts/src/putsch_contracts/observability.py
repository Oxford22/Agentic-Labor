"""Observability primitives shared across packages.

``TraceContext`` is the propagation envelope every cross-package call
carries. ``RedactionPolicy`` is read by ``putsch_obs`` at the SDK
boundary, so callers tag intent rather than each module re-implementing
PII rules.
"""

from __future__ import annotations

from datetime import UTC, datetime
from enum import StrEnum
from typing import Annotated
from uuid import UUID, uuid4

from pydantic import BaseModel, ConfigDict, Field, StringConstraints

from putsch_contracts.residency import DataClassification


class LogLevel(StrEnum):
    DEBUG = "debug"
    INFO = "info"
    WARNING = "warning"
    ERROR = "error"
    CRITICAL = "critical"


class SpanKind(StrEnum):
    SERVER = "server"
    CLIENT = "client"
    INTERNAL = "internal"
    PRODUCER = "producer"
    CONSUMER = "consumer"


_CorrelationId = Annotated[
    str,
    StringConstraints(min_length=8, max_length=128, pattern=r"^[A-Za-z0-9_\-]+$"),
]


class TraceContext(BaseModel):
    """The propagation envelope for cross-package calls.

    Every module accepts this as its first positional kwarg on any public
    boundary call (``async def extract(self, payload, *, trace: TraceContext)``).
    ``putsch_obs`` reads it to attach Langfuse trace IDs and OTel span IDs.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    correlation_id: _CorrelationId = Field(
        description="Stable per-workflow id, propagated end-to-end."
    )
    trace_id: UUID = Field(default_factory=uuid4)
    parent_span_id: UUID | None = None
    tenant: str = Field(min_length=1, max_length=64)
    workflow: str = Field(min_length=1, max_length=128)
    user_id: str | None = Field(default=None, max_length=128)
    classification: DataClassification = DataClassification.INTERNAL

    def child(self, *, span_id: UUID | None = None) -> TraceContext:
        """Return a new context with this span as the parent."""
        return self.model_copy(
            update={"parent_span_id": span_id or uuid4()},
        )


class RedactionPolicy(BaseModel):
    """How ``putsch_obs`` should redact a payload before persisting.

    ``mode='allowlist'`` is the safe default per ADR-004: only fields
    explicitly enumerated in ``visible_fields`` survive redaction; the
    rest are hashed or tokenized.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    mode: str = Field(default="allowlist", pattern=r"^(allowlist|blocklist)$")
    visible_fields: frozenset[str] = Field(default_factory=frozenset)
    tokenize_fields: frozenset[str] = Field(default_factory=frozenset)
    hash_fields: frozenset[str] = Field(default_factory=frozenset)
    classification: DataClassification = DataClassification.INTERNAL


class EvalRecord(BaseModel):
    """A single evaluation outcome, written by any module to ``putsch_obs``.

    The eval flywheel (per ARCHITECTURE.md, strategic anchor #3) ingests
    one of these per traced unit of work. Modules don't need to know
    Langfuse's API — they emit ``EvalRecord``s.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    correlation_id: _CorrelationId
    dataset: str = Field(min_length=1, max_length=128)
    item_id: str = Field(min_length=1, max_length=128)
    score: float = Field(ge=0.0, le=1.0)
    metric: str = Field(min_length=1, max_length=64)
    judge: str | None = Field(default=None, max_length=128)
    notes: str | None = Field(default=None, max_length=2048)
    recorded_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
