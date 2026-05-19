# `putsch-compile` — on-call runbook

This is the on-call playbook for the compiled-prompt / GEPA / BAML module. Five most-likely
failure modes. Each section: **symptom → diagnose → fix → verify → escalate**. Read top-down.

The platform Slack channel for this module is `#agentic-platform-oncall`. The Langfuse traces
referenced below all live at <https://langfuse.frankfurt.putsch.internal>.

---

## 1. GEPA optimization stuck / not converging

**Symptom.** `putsch-compile compile <signature>` runs past
`compilation.max_compilation_seconds` (default 900 s); the candidate ladder log stops emitting
new `compile.candidate` lines; or every candidate logs `objective=0.0`.

**Diagnose.**
1. Pull the most recent compilation trace in Langfuse:
   `traces?name=compile.<signature>&order=created_at.desc&limit=1`. Look at the
   `compile.candidate` observations.
2. If *every* candidate's `holdout_score` is far below the signature's `accuracy_threshold`, the
   problem is the dataset, not the optimizer. Skip to §2.
3. If `train_score` is high but `holdout_score` collapses, the train/holdout split is leaking. Run
   `python -c "from putsch_compile.optimize import _split_dataset, _load_jsonl; ..."` against
   the dataset and inspect for duplicate rows across splits.
4. If candidates are all returning `cost=0`, LiteLLM did not record token counts — usually the
   proxy stripped them. Check the proxy logs at
   `kubectl logs -n litellm deploy/litellm-proxy`.

**Fix.**
* **Bad dataset.** PR a corrected dataset; rerun on the PR (compile-on-pr workflow auto-runs).
* **Leaking split.** Bump `PUTSCH_COMPILE_OPTIMIZER_SEED` *only* after dedup; document in the PR.
  Bumping the seed invalidates every golden artifact — coordinate with the signature owners.
* **GEPA hang.** The optimizer wraps GEPA in a thread; if it's truly stuck, cancel the job, file
  a ticket against `dspy-ai`, fall back to the previously-promoted artifact (no action needed —
  rollback only required if a regression was already promoted).

**Verify.** Re-run `putsch-compile compile <signature>` and confirm at least one candidate's
`objective > 0`. Confirm the new compilation report appears in Langfuse.

**Escalate.** If three consecutive nightly runs fail to converge on the same signature, page
`@agentic-platform-lead`. Recurring non-convergence means the metric is mis-specified, not the
optimizer.

---

## 2. Dataset drift detected

**Symptom.** CI's `compile-on-pr` job comments
`>2% regression on <signature>` on a PR; or the nightly compile halts with
`compile.regression` and `previous_holdout_accuracy` is materially higher.

**Diagnose.**
1. Compare the new and old dataset hashes:
   `git log -p evals/datasets/<signature>.jsonl | head -200`. Was a row appended or edited?
2. If the regression coincides with a `sync-feedback` commit by the bot, the annotation absorbed
   a low-quality correction. Inspect each new row's `labeled_by`, `label_confidence`, and
   `source_trace_id` in Langfuse.
3. If the regression coincides with a signature change (instruction edit, field added), the
   `signature_version_hash` has rolled — the *old artifact is no longer comparable*. Drop the
   comparison and use this run as the new baseline (set `regression_tolerance` to a wider value
   on this one PR via env var, do not commit).

**Fix.**
* **Bad annotation.** Open a PR removing the offending row (`git revert <bot-commit>` is fine).
  Note the Langfuse trace ID in the PR description so the Sachbearbeiter can be informed why
  their correction was reverted.
* **Genuine drift.** Some signatures (Mahnstufe templates, DATEV chart changes) really do
  evolve. Accept the regression by promoting the new artifact in `staging`, observe in shadow
  for a week, then promote to `prod`. Document the accepted regression in
  `docs/adr/ADR-006-dspy-gepa-baml.md` if structural.

**Verify.** Compile-on-pr passes. Holdout accuracy on the new artifact ≥ the signature's
threshold.

**Escalate.** If a regression auto-rolls out to prod (it should never), page
`@agentic-platform-lead` immediately and execute §4 (rollback).

---

## 3. BAMLAdapter schema parse failure

**Symptom.** Logs show `AdapterError: BAML parse failure on <signature>` with non-zero rate; a
spike in `signature.<signature>` traces with `level=ERROR` in Langfuse.

**Diagnose.**
1. Pull the raw model output from the failing trace:
   `langfuse.traces.get(<trace_id>).output` shows the unparsed string.
2. Common causes, in descending frequency:
   - **Trailing prose.** The model added a "Here is the JSON:" preamble that BAML rejects. The
     BAML adapter's `strict_mode` should reject this, but a model regression can re-introduce it.
   - **Field type mismatch.** Model returned `"19"` for a `Decimal` that expected `"0.19"`. The
     signature's `desc` was ambiguous.
   - **List vs. dict.** Model returned a single object where a list was expected. Usually
     correctable by tightening the demo.

**Fix.**
* **Preamble pollution.** Re-compile the signature on a different model in the same tier; if the
  issue is model-specific, drop that model from the tier's catalog (or pin a previous revision in
  `routing.py`).
* **Type ambiguity.** Tighten the field `desc` on the affected signature, bump
  `SignatureMeta.version`, recompile.

**Verify.** The 5-minute moving average of `AdapterError` in Grafana drops below 0.1 %. The
Langfuse query `traces?name=signature.<name>&level=ERROR&from_timestamp=<now-1h>` returns < 1 % of
total.

**Escalate.** If `AdapterError` rate exceeds 5 % for any signature, immediately rollback (§4)
to the previous artifact and page `@agentic-platform-lead`.

---

## 4. Rollback procedure

**Symptom.** A regressed or broken artifact was promoted to `prod` and is affecting customer
workflows. Or: a customer reports a fresh class of error and you need to revert quickly.

**Steps.**
1. Identify the active artifact:
   `putsch-compile history <signature>` — top row is current.
2. Confirm the previous one is the desired target. If unclear, ask in
   `#agentic-platform-oncall` before flipping.
3. Execute the rollback:
   ```bash
   putsch-compile rollback <signature> --env prod --actor "$(whoami)@putsch.example"
   ```
4. The rollback is a single-row UPDATE in `registry_entries`. No MinIO write, no recompilation,
   takes < 200 ms. Customer-facing traffic switches on the next agent call (no service restart).
5. Open a Jira ticket in `AGENT-OPS` with the artifact IDs of the regressed + restored versions,
   the Langfuse trace exemplifying the issue, and the symptom.
6. Notify the signature's owning team via the channel listed in `OwnerTeam` (in
   `src/putsch_compile/signatures/_base.py`).

**Verify.**
* `putsch-compile history <signature>` shows the rolled-back artifact at the top.
* Langfuse: `signature.<name>` traces in the next 15 minutes carry the new
  `compiled_artifact_id` metadata.
* The original symptom no longer reproduces.

**Escalate.** If the rollback target is missing (orphan `previous_artifact_id`), promote
manually to a known-good artifact ID from `putsch-compile history` and document the gap. Page
`@agentic-platform-lead` for a postmortem.

---

## 5. Model availability outage (LiteLLM / Mistral La Plateforme)

**Symptom.** All signature calls on a given tier start failing with HTTP 5xx / `Timeout`; the
LiteLLM proxy `/health` endpoint reports degraded; Mistral status page declares a Frankfurt
incident.

**Diagnose.**
1. Confirm scope: `kubectl exec deploy/litellm-proxy -- curl -sf localhost:8080/health/models`.
2. Identify which tiers are affected — Mistral outages typically take out Tier 1 + Tier 2
   simultaneously; Qwen-via-Together usually affects Tier 3–5.
3. Check the per-model success rate in Grafana
   `litellm-proxy / models { success_rate < 0.95 }`.

**Fix — fallback ladder.**

The router has *fallback alternates within each tier*. Failover within a tier requires no PR —
it is a runtime preference set per environment via the registry. To force a fallback to a
secondary model in a tier:

```bash
# Example: Mistral Large outage — point classify_invoice_exception's preferred tier at
# DeepSeek R1 (also Tier 1).
putsch-compile promote --artifact <deepseek-r1-artifact-id> --env prod --actor "$(whoami)@putsch.example"
```

This works because the registry stores artifacts *per (signature, model)*, so a compiled artifact
already exists for each catalog model after a nightly recompile. If no artifact exists for the
fallback model on this signature, compile-on-demand:

```bash
PUTSCH_COMPILE_OPTIMIZER_NUM_THREADS=4 \
  putsch-compile compile <signature> --env staging --actor "$(whoami)@putsch.example"
```

then promote.

**Cross-tier escalation.** If the entire La Plateforme is down (rare), every German-prose
signature must temporarily route to Tier 1 reasoning models, accepting higher cost. Edit
`src/putsch_compile/routing.py` `_SIGNATURE_TIER` table, PR, fast-track review, ship to staging,
promote.

**Verify.**
* Latency p95 on the affected signatures returns to baseline within 10 minutes of the rollback.
* No `RoutingError` in logs for 30 minutes.

**Escalate.** If La Plateforme is unavailable for > 1 hour and degraded routing is also failing,
trigger the platform-wide *degraded mode*: every agent that calls a signature checks
`registry.get_active(..., environment="degraded")` first, which serves the previous-week's
compiled artifact on whatever model is currently green. Page `@agentic-platform-lead` and
`@infra-oncall`.

---

## Appendix — Exit codes & error codes

| Exit code | Meaning |
|---|---|
| 0 | success |
| 1 | `OptimizerError` / `DatasetError` |
| 2 | `RegressionError` — pipeline halted, previous artifact stays active |
| 3 | unknown signature / missing dataset |
| 4 | `RegistryError` (Postgres / MinIO) |

`CompilationError.code` values map to the same families: `compile.optimizer`, `compile.dataset`,
`compile.adapter`, `compile.registry`, `compile.routing`, `compile.regression`. Use them to route
alerts in Grafana — never grep on error messages.
