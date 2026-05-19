# Workspace integration

This monorepo is a [uv workspace](https://docs.astral.sh/uv/concepts/projects/workspaces/).
Each package under `packages/` is independently versioned, independently
testable, and independently releasable. The workspace root exists to
unify dev-environment bootstrap, CI, and cross-package editable installs.

## Why a workspace, not five repos

Five repos buys nothing here. The packages share:

- one architectural authority (`docs/ARCHITECTURE.md`)
- one EU-sovereign deployment surface (Frankfurt VPC, single CI gate)
- one set of compliance invariants (GDPR redaction, WORM audit, residency)
- one shared eval flywheel (every trace from `putsch-obs` feeds
  `putsch-compile`'s datasets)

A single repo with module-scoped CI gives us atomic refactors when the
shared invariants change, while still letting each module ship its own
wheel.

## Why a workspace, not one fat package

The packages have genuinely different release cadences, different
runtime environments (Neo4j vs. vLLM vs. Postgres), and different
ownership rotations. Collapsing them into one wheel would force every
deployment to carry every transitive dependency — neo4j drivers in the
OCR pod, Docling in the memory pod, etc. The workspace keeps the seam.

## Dependency graph

```
                     swarm
                       │
            ┌──────────┼──────────┐
            ▼          ▼          ▼
       putsch-docs   putsch-     putsch-
                     compile      obs
            │          │          │
            └──────────┼──────────┘
                       ▼
                 putsch-memory
```

- `putsch-obs` and `putsch-memory` are leaves — they have **no
  intra-workspace dependencies**. They must be the first two packages to
  stand up in any fresh environment.
- `putsch-compile` consumes `putsch-obs` for its annotation feedback
  loop (Sachbearbeiter corrections → DSPy training rows).
- `putsch-docs` consumes `putsch-compile` (compiled extraction
  signatures) and `putsch-obs` (trace export).
- `swarm` is the top of the stack — it composes specialists that call
  into the other four.

The intra-workspace edges above are **logical**, not yet enforced as
hard imports in this foundation commit; per-package adapters that wire
the imports together land in the integration PR that follows this one.

## Bring-up order

In a clean environment:

1. `uv sync --all-packages` (or `pip install -e packages/putsch-obs[dev]`)
2. Stand up Langfuse + OTel collector from
   `packages/putsch-obs/deploy/langfuse/docker-compose.yml`.
3. Stand up Neo4j + Graphiti from
   `packages/putsch-memory/deploy/memory/docker-compose.yml`.
4. Run `putsch-memory-migrate up` and verify Cypher constraints exist.
5. Run `putsch-compile validate-datasets` against the seed JSONL.
6. Run the `examples/customs_case.py` swarm demo end-to-end.

## Roll-back order

The reverse: tear down `swarm` first, then `putsch-docs`, then
`putsch-compile`, leaving the two foundations (`putsch-obs`,
`putsch-memory`) running last. The foundations carry stateful audit
chains; bringing them down is a separate, documented procedure in each
package's `docs/runbook.md`.

## CI

`.github/workflows/ci.yml` runs a per-package matrix. A change inside
`packages/<name>/` triggers only that package's lane plus the workspace
sanity lane. Cross-package PRs (which import across packages) trigger
the full matrix.

The regression-gating eval workflows shipped by `putsch-compile` and
`putsch-obs` remain inside their packages (under
`packages/<name>/.github/workflows/`) as documentation; they are
re-expressed at the workspace root in `.github/workflows/ci.yml` so
they actually trigger.

## Supersession

This integration supersedes the six original draft PRs:

- #1 — EU-sovereign architecture (hoisted to `docs/ARCHITECTURE.md`)
- #2 — Swarm coordination (subtree-merged to `packages/swarm/`)
- #3 — `putsch-docs` (subtree-merged)
- #4 — `putsch-compile` (subtree-merged)
- #5 — `putsch-memory` (subtree-merged)
- #6 — `putsch-obs` (subtree-merged)

Each subtree merge preserved the originating PR's tree under its
package prefix. The original branches remain available on GitHub for
blame and audit.
