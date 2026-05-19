# Integration Order

Defensible merge sequence for the six initial module PRs. **Read this before
merging anything.** If a later module needs to land out of order, justify it
in writing on the PR and update this document in the same commit — do not
silently reorder.

> The canonical architectural doctrine lives in
> [`ARCHITECTURE.md`](../ARCHITECTURE.md). Every section below references it
> rather than restating it. If integration order conflicts with the doctrine,
> the doctrine wins; amend `ARCHITECTURE.md` first.

## Merge sequence

| # | PR | Branch | Role | Depends on |
|---|----|----|---|---|
| 1 | #1 | `claude/eu-sovereign-architecture-BmCjy` | Doctrine: `ARCHITECTURE.md`, `README.md` | — |
| 2 | #7 | `claude/setup-agentic-labor-integration-R2VHr` | Bootstrap: workspace, `putsch_contracts`, CI guardrails, E2E scaffold | #1 |
| 3 | #6 | `claude/production-agentic-systems-v3HqM` | `putsch-obs` — Langfuse + OTel instrumentation backbone | #7 |
| 4 | #5 | `claude/production-agentic-systems-niSIS` | `putsch-memory` — Graphiti + Neo4j temporal memory | #7, #6 |
| 5 | #4 | `claude/production-agentic-systems-vhzcl` | `putsch-compile` — DSPy + GEPA + BAML compilation | #7, #6 |
| 6 | #3 | `claude/production-agentic-systems-Qpd0H` | `putsch-docs` — Docling + Qwen-VL extraction | #7, #6, #4 |
| 7 | #2 | `claude/swarm-coordination-agents-EkGuq` | `putsch-swarm` — Magentic-One on LangGraph | #7, #6, #5, #4, #3 |

## Why this order

The order minimises the rebase fan-out under the constraint that every module
PR must include `(c)` a test that imports `putsch_contracts` and `(d)` a test
that exercises a real cross-module call against a sibling Putsch package.

1. **#1 first** — every other PR references `ARCHITECTURE.md`. Merging it
   first lets module PRs link to anchors rather than copy the text. Per the
   hard rules, doctrine drift is fixed by amending #1, not by forking.

2. **#7 next** — none of the module PRs ship the shared workspace, the
   cross-package `putsch_contracts` types, or the CI guardrails (forbidden
   deps, EU residency, no `:latest`). Without this commit, the module PRs
   physically cannot satisfy rules (c) and (d). It is the only commit
   allowed to fast-track onto `main` because it unblocks every other PR.

3. **#6 (`putsch-obs`) before everything else** — every module imports
   `putsch_obs.instrumentation.init()` on startup and emits spans/events to
   Langfuse. It has no inbound dependency on any sibling Putsch package
   (it instruments via OTel APIs and CrewAI/LangGraph/DSPy/LiteLLM/Docling
   integration hooks). Landing it third means every subsequent module PR
   can wire real instrumentation in its cross-module test, not a stub.

4. **#5 (`putsch-memory`) before compile/docs/swarm** — Graphiti is the
   substrate that vendor / customer / account-routing lookups read from.
   `putsch-docs` reconciles extracted invoice fields against the memory
   layer's `lookup_vendor`. `putsch-swarm` specialists read it on every
   step. Landing memory before docs/swarm means their cross-module tests
   call the real `MemoryClientProtocol`, mocking only Neo4j at the driver
   boundary.

5. **#4 (`putsch-compile`) before docs/swarm** — `putsch-docs` consumes
   `CompiledSignatureRegistry.get("extract_invoice_fields")` to fetch its
   DSPy program. `putsch-swarm` worker prompts come from the registry too.
   Landing compile before its consumers means their tests exercise the
   real registry (with a Postgres test container), not a fake.

6. **#3 (`putsch-docs`) before #2** — `putsch-swarm`'s AP specialist
   invokes `DoclingExtractor.extract()` and feeds the result into the
   orchestrator's `TaskLedger`. Landing docs before swarm means swarm's
   E2E test consumes a real `Invoice` from a real extraction, not a
   hand-built one.

7. **#2 (`putsch-swarm`) last** — it depends on everything below. It owns
   the end-to-end Mahnverfahren / Customs / AP example, which is the
   integration test the rest of the platform is judged against.

## Gate per module PR

Before any module PR may merge, all of these must be green on the head
commit:

- [ ] Rebased onto current `main` (no merge commits).
- [ ] Workspace CI matrix green: `ruff`, `mypy --strict`, `pytest`.
- [ ] Guardrails CI green: forbidden-deps, EU-residency, no `:latest`.
- [ ] At least one test imports a `putsch_contracts` interface
      (Protocol, Pydantic model, or enum) — checked by
      `scripts/check_contracts_imported.py`.
- [ ] At least one test exercises a real cross-module call to a sibling
      Putsch package (external services may be mocked at the transport
      boundary; sibling Putsch packages may not be mocked).
- [ ] Doctrine compatibility: no contradiction with `ARCHITECTURE.md`. If
      one exists, the same PR or a fast-follow must amend the doctrine.

## What lives where

- `packages/putsch_contracts/` — typed Pydantic models, Protocols, enums
  shared across every other package. **Owned by the integration owner.**
  Changes require a CHANGELOG entry and a deprecation cycle for any
  removal — sibling packages pin a minor range, not `*`.
- `tests/integration/test_e2e_invoice.py` — the canonical end-to-end
  test. Every module PR appends its segment to the chain and the test
  expands from `pytest.skip` to a full assertion as packages land.
- `.github/workflows/guardrails.yml` — non-negotiable. Forbidden deps,
  forbidden regions, forbidden `:latest`. CI fails closed.

## Process notes

- The bootstrap PR (#7) is the **only** commit that may land directly on
  `main` without a review-by-default workflow. It still opens as a
  draft PR, but reviewers are expected to fast-track it (<24h) because
  every other PR is blocked behind it.
- After #7 lands, every module PR rebases onto `main` exactly once. If a
  conflict appears during the rebase, the module PR resolves it on its
  own branch — never on `main`, never via merge commit.
- `main` is protected: no force-push, ever. A PR that lands here is
  history.
