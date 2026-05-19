# ADR-006: Compiled prompts via DSPy + GEPA + BAML

* **Status.** Accepted, 2026-05.
* **Context.** Putsch Group needs a production agentic stack that compounds value week over week
  rather than aging out as new model versions ship. The model landscape's effective half-life is
  ~90 days — hand-tuned prompts are technical debt with a known expiry date.
* **Decision.** DSPy as the programming model. GEPA as the optimizer. BAML as the structured-
  output adapter. Codified in this package as `putsch_compile`.

This ADR captures the *why*. The how is in `src/putsch_compile/` and `docs/runbook.md`.

---

## 1. Decision

For every reusable prompt in the Putsch agent stack:

* Declare it as a `dspy.Signature` subclass with Pydantic v2 typed input + output fields.
* Compile it with GEPA against a git-versioned eval dataset, with the BAML adapter selected as
  the default DSPy adapter.
* Walk the candidate-model ladder *cheapest-first*; promote the cheapest model that hits the
  signature's accuracy threshold and stays under its cost ceiling.
* Persist the compiled artifact in MinIO + Postgres, version-pinned, rollback-safe.
* Re-compile nightly when the model catalog changes, gated by a 2 %-regression CI check.

This is not a recommendation. It is the only path through which prompts ship to production at
Putsch.

---

## 2. Rejected alternatives

### 2a. Hand-tuned prompt strings (status-quo for ~95 % of LLM apps in 2026)

A "prompt engineer" writes the prompt as a string, commits it, lightly tweaks it whenever someone
complains. No compilation, no eval-driven improvement, no programmatic model swap.

**Rejected because of cost-of-drift.** The cost of running hand-tuned prompts in production
*scales worse than linearly with the age of the prompt*. Concrete numbers, conservative
assumptions:

| Horizon | Production agents | Touch-ups / agent / quarter | Senior eng hours / touch-up | Hours / quarter | Annualised cost @ €120/h |
|---|---|---|---|---|---|
| Month 6 | 8 | 1.5 | 6 | 72 | €34,560 |
| Month 12 | 18 | 2.5 | 6 | 270 | €129,600 |
| Month 24 | 35 | 4.0 | 6 | 840 | €403,200 |

The numbers don't include the *un-touched* prompts that silently degrade — every model swap
either breaks them outright or quietly costs accuracy until a customer complaint surfaces it. The
hidden cost is the bigger one.

By contrast, the DSPy + GEPA + BAML stack:

* Re-compilation runs nightly, unattended. Cost: ~€20 of GPU/API time per week per signature, no
  human hours.
* Model swaps are recompilations, not rewrites. A DeepSeek V5 release means a one-config-line
  update + a CI workflow run, not a sprint.
* Sachbearbeiter corrections are training data, not feedback. Every annotation in the Langfuse
  queue strengthens next week's compiled artifact. **The system gets better while you sleep.**

At month 24, the ratio is roughly 100×. This is the only argument that needed to land.

### 2b. Raw Instructor / Outlines

These libraries provide structured-output binding (Pydantic types → constrained JSON) but no
program abstraction and no optimizer. They are *part of* what BAML does, not a replacement for
DSPy + GEPA.

Rejected because: structured outputs without compilation get you to demo, not to a compounding
production system. We'd be back to hand-tuning the prompt strings that drive Instructor.

### 2c. LangChain prompt templates

String-based templates with Jinja-style interpolation. No compilation, no programmatic
optimization, no schema typing beyond what the developer hand-writes.

Rejected for the same reason as 2a, plus: LangChain's templating layer is the source of more
production agent bugs in 2025 than any other single dependency (per our Q4 2025 incident review).
Strings interpolated into prompts are a *string interpolation vulnerability surface*, full stop.

### 2d. BAML alone (without DSPy)

BAML standalone (BoundaryML, MIT) gives structured outputs with superior schema representation.
We use it — as DSPy's adapter — but using it *without* DSPy leaves us with hand-tuned prompts
again. The optimizer layer is the moat; the adapter is the lubricant.

### 2e. Build it ourselves

A custom compilation harness around vanilla `litellm` + `pydantic`. We considered this for ~four
hours.

Rejected because: GEPA's published numbers — ~14-point improvement over DSPy baseline, ~22-point
over raw OpenAI on structured extraction — are not numbers we will beat with a quarter of work.
The Stanford NLP group has spent two years on this. We will use the tool and contribute
upstream when we find gaps. We will not rebuild it.

---

## 3. Consequences

### Positive
* **Compounding moat.** Production traces → annotations → training data → next compile. The
  flywheel turns weekly without human intervention. By month 18 the artifact for a mature
  signature has incorporated ~100 Sachbearbeiter corrections — irreproducible by any
  competitor who didn't start collecting two years ago.
* **Cost-tied-to-task.** The cheapest-model-first ladder ensures we never pay Tier-1 reasoning
  prices for Tier-5 classification tasks. The optimizer's multi-objective scalarisation in
  `metrics.composite_objective` formalises this — accuracy is a gate, cost is the minimised
  axis above it.
* **Model swap is a config change.** When Mistral Large 3 lands or DeepSeek V5 ships,
  recompilation takes ~10 minutes per signature on a CI worker. Zero developer time.
* **Auditability.** Every signature call carries `compiled_artifact_id` + `version` in its
  Langfuse trace. Wirtschaftsprüfer ask "what prompt produced this booking on 2026-02-15?" — we
  answer with one query and a MinIO fetch.
* **Rollback as a registry UPDATE.** A regression is a single-row flip, not a deploy. The
  on-call playbook in `docs/runbook.md` § 4 is < 5 commands.

### Negative
* **Mental model overhead.** DSPy's signatures + modules + optimizers vocabulary is unfamiliar
  to most prompt engineers. We mitigate by (a) keeping the signature surface narrow — one
  declared signature per back-office task — and (b) having *one* engineer specialise as the
  optimizer lead. They review every signature PR.
* **Optimizer cost.** Nightly recompilation across the full signature catalog is a non-zero
  bill. We bound it via `compilation.max_compilation_seconds` and the cheapest-model-first
  ladder. At seed dataset sizes (50–100 rows), GEPA converges in ~5–10 minutes per signature.
* **Bug-class shift.** Bugs move from "prompt string typos" to "metric mis-specifications". A
  bad metric will optimise toward the wrong thing. We mitigate via (a) hand-written metrics in
  `metrics.py` rather than inferred from data, (b) a code review checklist for new metrics that
  forces the author to articulate what the metric *would* reward.

### Operational
* **Repo structure.** Signatures live in `src/putsch_compile/signatures/`. Datasets in
  `evals/datasets/`. Compiled artifacts in MinIO, indexed by Postgres. None of these surfaces
  are mixed — datasets in git, artifacts in MinIO, lookups in Postgres.
* **Ownership.** Each signature has an `OwnerTeam` in its `SignatureMeta`. CODEOWNERS routes
  signature changes accordingly. The compilation infrastructure (this package) is owned by
  the agentic-platform team.
* **CI gates.** A signature or dataset change triggers `compile-on-pr.yml`, which re-evaluates
  the affected signatures and blocks merge on > 2 % regression. A blocked merge is a feature,
  not a bug.

---

## 4. Compliance posture (GDPR / EU AI Act / Betriebsrat)

* **GDPR.** All compilation, artifact storage, and Langfuse observability is Frankfurt-hosted.
  No personal data leaves the EU. Dataset entries that may contain PII (customer email
  drafts, master-data records) are stored on MinIO under the same controls as the production
  data they were drawn from. Retention follows the same policy as the source.
* **EU AI Act.** Each signature carries a stated `purpose`, `accuracy_threshold`, and
  `cost_ceiling` — these are the operational metadata an AI Act risk assessment will request.
  The model used for each production call is logged in the Langfuse trace, so traceability of
  "which model decided this" is one query. The annotation feedback loop (Sachbearbeiter →
  training data) is documented in `docs/runbook.md` and observable via the
  `compile.feedback.examples_absorbed` metric.
* **Betriebsrat.** Hand-tuned prompts are opaque to a Betriebsrat reviewer; compiled artifacts
  with a labeled training set and a stated metric are not. The dataset entries' provenance
  fields (`labeled_by`, `labeled_at`, `label_confidence`, `source_trace_id`) make every
  decision traceable to a human source.

---

## 5. Open questions / things to revisit

* **GEPA alternatives.** When MIPRO-v3 or a successor optimizer ships, we re-evaluate. The
  decision to use GEPA is not the decision to use *only* GEPA — `optimizer.py` is designed to
  accept other teleprompters via the same harness.
* **Multi-stage signatures.** Some flows (e.g., audit narrative generation) might want
  composition of two signatures into a pipeline. DSPy supports this, but we have not exercised
  it. Defer until a concrete use case lands.
* **LLM-as-judge bias.** Prose metrics use an LLM judge. The judge model is fixed at Tier 1 to
  reduce the bias-toward-candidate-strengths effect, but a study is warranted. Target: Q1 2027.
* **Cost ceiling enforcement at runtime.** Today the ceiling is enforced at compile time; we
  should add a per-call budget check at the LiteLLM proxy level so a runaway agent cannot
  blow through monthly budget.
