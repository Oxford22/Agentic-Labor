"""Shared types and Protocols for the Agentic Labor workspace.

The canonical doctrine for what this package implements lives in
``ARCHITECTURE.md`` at the repository root. See
``docs/INTEGRATION_ORDER.md`` for how this package gates the other
modules.
"""

from putsch_contracts.compile import (
    CompiledSignature,
    ModelTier,
    RegistryEntry,
    SignatureMetric,
)
from putsch_contracts.invoice import (
    BankDetails,
    Invoice,
    InvoiceLineItem,
    InvoiceTotals,
    PaymentTerms,
)
from putsch_contracts.memory import (
    EpisodeKind,
    MemoryEpisode,
    Provenance,
    TemporalQuery,
)
from putsch_contracts.observability import (
    EvalRecord,
    LogLevel,
    RedactionPolicy,
    SpanKind,
    TraceContext,
)
from putsch_contracts.orchestration import (
    HumanReviewRequest,
    TaskLedger,
    WorkflowState,
    WorkflowStatus,
)
from putsch_contracts.protocols import (
    CompileRegistryProtocol,
    ExtractionResult,
    ExtractorProtocol,
    MemoryClientProtocol,
    ObservabilityProtocol,
    OrchestratorProtocol,
)
from putsch_contracts.residency import (
    ALLOWED_REGIONS,
    DataClassification,
    ResidencyError,
    validate_region,
)
from putsch_contracts.vendor import (
    AccountRouting,
    CustomerRecord,
    VendorRecord,
)
from putsch_contracts.version import __version__

__all__ = [
    "ALLOWED_REGIONS",
    "AccountRouting",
    "BankDetails",
    "CompileRegistryProtocol",
    "CompiledSignature",
    "CustomerRecord",
    "DataClassification",
    "EpisodeKind",
    "EvalRecord",
    "ExtractionResult",
    "ExtractorProtocol",
    "HumanReviewRequest",
    "Invoice",
    "InvoiceLineItem",
    "InvoiceTotals",
    "LogLevel",
    "MemoryClientProtocol",
    "MemoryEpisode",
    "ModelTier",
    "ObservabilityProtocol",
    "OrchestratorProtocol",
    "PaymentTerms",
    "Provenance",
    "RedactionPolicy",
    "RegistryEntry",
    "ResidencyError",
    "SignatureMetric",
    "SpanKind",
    "TaskLedger",
    "TemporalQuery",
    "TraceContext",
    "VendorRecord",
    "WorkflowState",
    "WorkflowStatus",
    "__version__",
    "validate_region",
]
