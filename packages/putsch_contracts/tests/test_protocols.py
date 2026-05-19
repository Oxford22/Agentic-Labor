"""Protocols can be implemented by concrete classes (smoke tests).

These exist so that an accidental signature drift on a Protocol breaks
the workspace, not the downstream module's tests. They also serve as the
"at least one test imports putsch_contracts" gate for the bootstrap PR
itself.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from typing import Any
from uuid import uuid4

import pytest
from putsch_contracts import (
    CompiledSignature,
    EvalRecord,
    HumanReviewRequest,
    LogLevel,
    MemoryEpisode,
    ModelTier,
    Provenance,
    RedactionPolicy,
    SignatureMetric,
    TaskLedger,
    TemporalQuery,
    TraceContext,
    WorkflowState,
    WorkflowStatus,
)
from putsch_contracts.memory import EpisodeKind
from putsch_contracts.protocols import (
    CompileRegistryProtocol,
    ExtractionResult,
    ExtractorProtocol,
    MemoryClientProtocol,
    ObservabilityProtocol,
    OrchestratorProtocol,
)


class _FakeExtractor:
    async def extract(self, document_uri: str, *, trace: TraceContext) -> ExtractionResult:
        raise NotImplementedError


class _FakeMemory:
    async def lookup_vendor(
        self,
        vat_id: str | None = None,
        *,
        iban: str | None = None,
        name: str | None = None,
        trace: TraceContext,
    ) -> None:
        return None

    async def lookup_customer(
        self,
        vat_id: str | None = None,
        *,
        name: str | None = None,
        trace: TraceContext,
    ) -> None:
        return None

    async def lookup_account_routing(self, vendor_id: str, *, trace: TraceContext) -> None:
        return None

    async def temporal_query(
        self, query: TemporalQuery, *, trace: TraceContext
    ) -> list[MemoryEpisode]:
        return []

    async def write_episode(
        self,
        episode: MemoryEpisode,
        *,
        trace: TraceContext,
        provenance: Provenance,
    ) -> MemoryEpisode:
        return episode


class _FakeObs:
    @asynccontextmanager
    async def _span(self) -> AsyncIterator[None]:
        yield None

    def span(
        self,
        name: str,
        *,
        trace: TraceContext,
        attributes: dict[str, Any] | None = None,
    ) -> AsyncIterator[None]:
        return self._span()  # type: ignore[return-value]

    async def log(
        self,
        level: LogLevel,
        message: str,
        *,
        trace: TraceContext,
        attributes: dict[str, Any] | None = None,
    ) -> None:
        return None

    async def record_eval(
        self, record: EvalRecord, *, redaction: RedactionPolicy | None = None
    ) -> None:
        return None


class _FakeRegistry:
    async def get(self, name: str) -> CompiledSignature:
        return CompiledSignature(
            name=name,
            version="1.0.0",
            program_hash="a" * 40,
            artifact_uri="s3://putsch-compile/test",
            tier=ModelTier.SMALL,
            metric=SignatureMetric(name="f1", threshold=0.9),
            owner_team="ap-crew",
        )

    async def invoke(
        self,
        signature: CompiledSignature,
        inputs: dict[str, Any],
        *,
        trace: TraceContext,
    ) -> dict[str, Any]:
        return {}


class _FakeOrchestrator:
    async def run(
        self, workflow: str, inputs: dict[str, Any], *, trace: TraceContext
    ) -> WorkflowState:
        return WorkflowState(
            workflow=workflow,
            checkpoint_id="cp-1",
            last_node="start",
            status=WorkflowStatus.PENDING,
        )

    async def request_human_review(self, request: HumanReviewRequest) -> str:
        return request.decision_options[0]


@pytest.mark.parametrize(
    "instance,proto",
    [
        (_FakeExtractor(), ExtractorProtocol),
        (_FakeMemory(), MemoryClientProtocol),
        (_FakeObs(), ObservabilityProtocol),
        (_FakeRegistry(), CompileRegistryProtocol),
        (_FakeOrchestrator(), OrchestratorProtocol),
    ],
)
def test_runtime_checkable_protocols(instance: object, proto: type) -> None:
    assert isinstance(instance, proto)


def test_trace_context_propagation() -> None:
    parent = TraceContext(correlation_id="abc12345", tenant="putsch", workflow="ap")
    child = parent.child()
    assert child.correlation_id == parent.correlation_id
    assert child.trace_id == parent.trace_id
    assert child.parent_span_id is not None
    assert child.parent_span_id != parent.parent_span_id


def test_memory_episode_correction_self_reference_rejected() -> None:
    eid = uuid4()
    with pytest.raises(ValueError, match="correction_of"):
        MemoryEpisode(
            episode_id=eid,
            kind=EpisodeKind.AP,
            business_time=datetime.now(UTC),
            provenance=Provenance(
                source="putsch-docs",
                source_id="RE-1",
                correlation_id="abc12345",
                recorded_by="ap-crew",
            ),
            correction_of=eid,
        )


def test_task_ledger_defaults_are_empty() -> None:
    ledger = TaskLedger()
    assert ledger.facts == []
    assert ledger.guesses == []
    assert ledger.plan == []
    assert ledger.replan_count == 0
