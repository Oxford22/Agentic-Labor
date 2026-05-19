"""Cross-package call surfaces, as ``typing.Protocol``s.

These are the **only** import paths that sibling Putsch packages should
use to type a parameter that names another package's behaviour. They
let every consumer code against an interface and every producer pick its
own concrete class, without circular imports.

Each Protocol is ``@runtime_checkable`` so tests can ``isinstance()``
against a fake when needed, but production code should rely on static
type checking, not the runtime check.
"""

from __future__ import annotations

from typing import Any, Protocol, runtime_checkable

from pydantic import BaseModel, ConfigDict, Field

from putsch_contracts.compile import CompiledSignature
from putsch_contracts.invoice import Invoice
from putsch_contracts.memory import MemoryEpisode, Provenance, TemporalQuery
from putsch_contracts.observability import EvalRecord, LogLevel, RedactionPolicy, TraceContext
from putsch_contracts.orchestration import HumanReviewRequest, WorkflowState
from putsch_contracts.vendor import AccountRouting, CustomerRecord, VendorRecord


class ExtractionResult(BaseModel):
    """What an ``ExtractorProtocol`` implementation returns.

    The ``Invoice`` is the structured payload; ``per_field_confidence``
    travels alongside so the caller can decide when to fall back to a
    second model. ``raw_text`` is the OCR output preserved for audit.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    invoice: Invoice
    per_field_confidence: dict[str, float] = Field(default_factory=dict)
    extractor: str = Field(min_length=1, max_length=128)
    extractor_version: str = Field(min_length=1, max_length=64)
    raw_text_uri: str | None = Field(default=None, pattern=r"^s3://[a-z0-9\-./_]+$")
    fallback_used: bool = False


@runtime_checkable
class ExtractorProtocol(Protocol):
    """Implemented by ``putsch_docs.DoclingExtractor``.

    Concrete: takes a document URI (S3 or local path), returns an
    ``ExtractionResult``. ``trace`` is propagated so ``putsch-obs`` can
    attach the span. Must not raise on per-field confidence drops —
    those belong inside the result.
    """

    async def extract(
        self,
        document_uri: str,
        *,
        trace: TraceContext,
    ) -> ExtractionResult: ...


@runtime_checkable
class MemoryClientProtocol(Protocol):
    """Implemented by ``putsch_memory.graphiti_client.GraphitiClient``.

    Vendor / customer / account-routing lookups plus episode writes and
    bounded temporal queries. Implementations must enforce
    ``provenance`` (no anonymous writes) and bound traversals per
    ``TemporalQuery.max_depth``.
    """

    async def lookup_vendor(
        self,
        vat_id: str | None = None,
        *,
        iban: str | None = None,
        name: str | None = None,
        trace: TraceContext,
    ) -> VendorRecord | None: ...

    async def lookup_customer(
        self,
        vat_id: str | None = None,
        *,
        name: str | None = None,
        trace: TraceContext,
    ) -> CustomerRecord | None: ...

    async def lookup_account_routing(
        self,
        vendor_id: str,
        *,
        trace: TraceContext,
    ) -> AccountRouting | None: ...

    async def temporal_query(
        self,
        query: TemporalQuery,
        *,
        trace: TraceContext,
    ) -> list[MemoryEpisode]: ...

    async def write_episode(
        self,
        episode: MemoryEpisode,
        *,
        trace: TraceContext,
        provenance: Provenance,
    ) -> MemoryEpisode: ...


@runtime_checkable
class ObservabilityProtocol(Protocol):
    """Implemented by ``putsch_obs.instrumentation.Observability``.

    Every other module receives one of these on construction (constructor
    injection, not a global). ``span`` is an async context manager; the
    other methods are fire-and-forget from the caller's perspective.
    """

    def span(
        self,
        name: str,
        *,
        trace: TraceContext,
        attributes: dict[str, Any] | None = None,
    ) -> Any:  # AsyncContextManager[Span]; opaque on this side
        ...

    async def log(
        self,
        level: LogLevel,
        message: str,
        *,
        trace: TraceContext,
        attributes: dict[str, Any] | None = None,
    ) -> None: ...

    async def record_eval(
        self,
        record: EvalRecord,
        *,
        redaction: RedactionPolicy | None = None,
    ) -> None: ...


@runtime_checkable
class CompileRegistryProtocol(Protocol):
    """Implemented by ``putsch_compile.registry.Registry``.

    Look up a promoted compiled signature by name. ``invoke`` runs it
    through LiteLLM; the registry owns model routing per
    ``CompiledSignature.tier``. The opaque ``Any`` on input/output is
    intentional — each signature has its own Pydantic I/O schema, which
    the consumer validates after.
    """

    async def get(self, name: str) -> CompiledSignature: ...

    async def invoke(
        self,
        signature: CompiledSignature,
        inputs: dict[str, Any],
        *,
        trace: TraceContext,
    ) -> dict[str, Any]: ...


@runtime_checkable
class OrchestratorProtocol(Protocol):
    """Implemented by ``putsch_swarm.orchestrator.Orchestrator`` and by
    LangGraph-wrapped CrewAI crews.

    Top-level entry for running a workflow. ``request_human_review``
    is the synchronous side of ``LangGraph.interrupt()``; the runtime
    suspends until a Sachbearbeiter responds.
    """

    async def run(
        self,
        workflow: str,
        inputs: dict[str, Any],
        *,
        trace: TraceContext,
    ) -> WorkflowState: ...

    async def request_human_review(
        self,
        request: HumanReviewRequest,
    ) -> str: ...
