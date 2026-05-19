# Changelog — putsch-contracts

All notable changes to the shared-contracts package. Sibling Putsch
packages pin a minor range on this; removals require a deprecation
cycle of one minor release.

The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/)
and the project follows semantic versioning at the package level
(`MAJOR.MINOR.PATCH`).

## [0.1.0] — 2026-05-19 (bootstrap)

### Added
- Initial Pydantic v2 models: `Invoice`, `InvoiceLineItem`, `InvoiceTotals`,
  `BankDetails`, `PaymentTerms`, `PartyAddress`, `Currency`.
- Master-data types: `VendorRecord`, `CustomerRecord`, `AccountRouting`.
- Memory primitives: `MemoryEpisode`, `Provenance`, `TemporalQuery`,
  `EpisodeKind`.
- Observability primitives: `TraceContext`, `RedactionPolicy`, `EvalRecord`,
  `LogLevel`, `SpanKind`.
- Compile primitives: `CompiledSignature`, `RegistryEntry`,
  `SignatureMetric`, `ModelTier`.
- Orchestration primitives: `WorkflowState`, `WorkflowStatus`,
  `HumanReviewRequest`, `TaskLedger`.
- Cross-package Protocols: `ExtractorProtocol`, `MemoryClientProtocol`,
  `ObservabilityProtocol`, `CompileRegistryProtocol`,
  `OrchestratorProtocol`, plus `ExtractionResult` Pydantic envelope.
- Residency primitives: `ALLOWED_REGIONS`, `FORBIDDEN_REGION_PREFIXES`,
  `validate_region()`, `ResidencyError`, `DataClassification` enum.
- `py.typed` marker so consumers get strict type-checking out of the box.
