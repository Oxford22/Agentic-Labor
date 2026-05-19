# putsch-contracts

Shared Pydantic v2 types and `typing.Protocol` interfaces for every Putsch
package. Lives at the bottom of the dependency graph; every other package
depends on this and nothing depends on any other package through it.

## Why this package exists

Six modules were authored independently and need to call each other:

```
putsch-swarm  --uses-->  putsch-docs       (Invoice produced here)
putsch-swarm  --uses-->  putsch-memory     (lookup_vendor, write_episode)
putsch-swarm  --uses-->  putsch-compile    (signature registry)
putsch-docs   --uses-->  putsch-memory     (reconcile against master data)
putsch-docs   --uses-->  putsch-compile    (DSPy signatures)
every module  --uses-->  putsch-obs        (trace, span, eval event)
```

Without a shared type vocabulary, each pairwise call needs a `from
putsch_other.module import ConcreteClass`, which makes the dependency graph
a complete graph. With this package, every module imports from
`putsch_contracts` and depends only on it.

## Layout

| Module | Contents |
|---|---|
| `invoice` | German `Invoice`, `InvoiceLineItem`, `BankDetails`, `PaymentTerms` |
| `vendor` | `VendorRecord`, `CustomerRecord`, `AccountRouting` |
| `memory` | `MemoryEpisode`, `Provenance`, `TemporalQuery` |
| `observability` | `TraceContext`, `EvalRecord`, `RedactionPolicy` |
| `compile` | `CompiledSignature`, `ModelTier`, `RegistryEntry` |
| `orchestration` | `WorkflowState`, `HumanReviewRequest`, `TaskLedger` |
| `protocols` | `ExtractorProtocol`, `MemoryClientProtocol`, `ObservabilityProtocol`, `CompileRegistryProtocol`, `OrchestratorProtocol` |
| `residency` | EU-region validators, GDPR data-class tags |

## Stability

Every type is versioned via `putsch_contracts.__version__`. Removals require
a deprecation cycle of one minor release. Sibling packages pin
`putsch-contracts>=0.X,<0.(X+1)` — never `*`.

See [`../../docs/INTEGRATION_ORDER.md`](../../docs/INTEGRATION_ORDER.md) for
how this package fits into the merge sequence.
