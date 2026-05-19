# putsch-compile

**Compiled-prompts, model-routing, and continuous-optimization layer for the Putsch Group agent stack.**

DSPy as the programming model. GEPA as the optimizer. BAML as the structured-output adapter.
Mistral La Plateforme (Frankfurt) as the only external API. Langfuse for observability. Zep+Graphiti
for memory. Postgres + MinIO for the compiled-artifact registry.

This module is *the compounding moat* of the platform. Every other module ships value once; this one
ships value that compounds week over week.

---

## Compilation philosophy

> Prompts are compiled artifacts. Signatures are source code.

If you take one thing from this README, take that.

### 1. Prompts are not source code

A prompt string is the *output* of a compiler. The compiler is GEPA. The input is a `dspy.Signature`
plus a labeled dataset plus a metric. Hand-editing a compiled prompt is the equivalent of editing
assembly — possible, but a smell, and a violation of the audit trail. If a prompt needs to change,
change the signature declaration, change the dataset, or change the metric. Then re-compile.

### 2. Datasets are git, never UI

Eval datasets live in `evals/datasets/<signature>.jsonl`, versioned by git, reviewed in PRs, with
strict Pydantic schemas enforced by CI. The Langfuse annotation queue is a *staging area*: a
Sachbearbeiter correction lands there, is pulled by the feedback loop (`putsch_compile.feedback`),
validated, appended to the dataset file, and committed by the platform service account. Datasets
that drift in a UI are datasets that cannot be reproduced; reproducibility is the whole point.

### 3. Cheapest model first, always

Every new signature compiles against the cheapest tier first (Tier 5 — Qwen3-14B). Only escalates to
a more expensive tier if the cheap tier fails the accuracy threshold on holdout. The default is the
bottom of the routing ladder; climbing requires proof. See `src/putsch_compile/routing.py` for the
tier definitions.

### 4. Multi-objective optimization

The optimizer's objective is *not* accuracy. It is:

```
(accuracy >= signature.threshold)  ∧  minimize(cost_per_call)
```

A 0.5% accuracy gain that doubles cost-per-call is a regression in disguise. Reported in the
compilation report, blocked at the gate.

### 5. Annotations are training data

Every Sachbearbeiter correction in the Langfuse annotation queue becomes a training example with
full provenance — `labeled_by`, `labeled_at`, `label_confidence`, `source_trace_id`. The annotator
is a teacher, the system is a student. This is the entire flywheel. A metric in Langfuse,
`compile.feedback.examples_absorbed`, proves the loop is closed every week.

### 6. A regression is a P0

>2% accuracy regression on holdout halts the pipeline. The previous artifact stays in production.
On-call investigates. **Auto-rollout of regressed prompts is the most common production agent failure mode** and we will not be that team.

---

## Architecture

```
                       Langfuse traces (production calls)
                                  │
                                  ▼
                ┌─ Annotation queue (Sachbearbeiter UI) ─┐
                │                                        │
                ▼                                        │
        feedback.py: pull, validate, dedupe              │
                │                                        │
                ▼                                        │
        evals/datasets/<signature>.jsonl ──── git PR ────┘
                │
                ▼
        optimize.py: GEPA compilation
        (cheapest-model-first ladder, multi-objective)
                │
                ▼
        registry.py: (signature, model, version) → artifact_id
        (Postgres index + MinIO blob)
                │
                ▼
        Production agents (CrewAI + LangGraph) ── load ── compiled artifact
```

Every production signature call carries `compiled_artifact_id` and `compiled_artifact_version` in
its Langfuse trace. Rollback = one row UPDATE in `registry_entries`. No code deploy.

## Quick start (local)

```bash
uv sync --extra dev
docker compose up -d postgres minio langfuse-server   # platform infra, ops repo
alembic upgrade head
putsch-compile compile extract_invoice_fields --dataset evals/datasets/extract_invoice_fields.jsonl
putsch-compile promote --artifact <artifact_id>
```

## Module index

| Module | Purpose |
|---|---|
| `putsch_compile.signatures` | Eight reusable `dspy.Signature` declarations. The strategic asset. |
| `putsch_compile.adapters` | `BAMLAdapter` configured as the default DSPy adapter. |
| `putsch_compile.routing` | Per-signature model-tier preferences, resolved at call time via LiteLLM. |
| `putsch_compile.optimize` | GEPA compilation harness: dataset → optimized artifact, cheapest-model-first. |
| `putsch_compile.registry` | Postgres + MinIO registry of compiled artifacts. Versioned. Rollback-safe. |
| `putsch_compile.feedback` | Langfuse annotations → dataset commits → scheduled recompilation. |
| `putsch_compile.metrics` | Per-signature evaluation metrics. Composite (accuracy, cost). |
| `putsch_compile.artifacts` | MinIO/S3-compatible blob store for compiled artifacts. |
| `putsch_compile.tracing` | Langfuse correlation IDs, signature call instrumentation. |
| `putsch_compile.config` | 12-factor settings via `pydantic-settings`. |

## See also

- `docs/adr/ADR-006-dspy-gepa-baml.md` — why DSPy + GEPA + BAML, with rejection rationale.
- `docs/runbook.md` — the 5 most likely failures and what to do about them.
- The spec: this module operationalizes the "swap the best model per task and evolve over time"
  requirement from the platform spec. See § 6 (Wildcard — DSPy with GEPA + BAML Adapter).
