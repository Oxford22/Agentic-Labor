# The flywheel — extended notes

This document is a deep-dive on the eval loop that closes the
production-to-training cycle. If you only have 5 minutes, read the
README; come back here when you need to extend the loop.

## The loop, in detail

```
┌──────────────────────────────────────────────────────────────────┐
│  PRODUCTION                                                       │
│  - Every CrewAI / LangGraph / DSPy run emits an OTel trace        │
│  - PII redacted at the SDK boundary; tokens kept in vault         │
│  - Trace lands in self-hosted Langfuse                            │
│  - Cost, latency, model decision recorded as trace attributes     │
└────────────────────────────────┬─────────────────────────────────┘
                                 │
                                 ▼
┌──────────────────────────────────────────────────────────────────┐
│  CURATION                                                         │
│  - LLM-as-judge (DeepSeek V3) scores each trace against rubric    │
│  - Low-confidence + flagged traces enter the annotation queue     │
│  - Sachbearbeiter reviews and corrects (Langfuse UI)              │
│  - All corrections are kept in the queue history                  │
└────────────────────────────────┬─────────────────────────────────┘
                                 │ (annotations_to_training_set)
                                 ▼
┌──────────────────────────────────────────────────────────────────┐
│  TRAINING SET                                                     │
│  - Promoted annotations land in evals/datasets/*.jsonl            │
│  - Committed to git; reviewed in a PR                             │
│  - On merge, sync_to_langfuse upserts into the Langfuse dataset   │
└────────────────────────────────┬─────────────────────────────────┘
                                 │ (DSPy GEPA compile)
                                 ▼
┌──────────────────────────────────────────────────────────────────┐
│  COMPILATION                                                      │
│  - DSPy GEPA reads the dataset                                    │
│  - Optimizes prompts + few-shot demos for each signature          │
│  - Records compile_version on each predictor                      │
│  - Eval runs against the same dataset gate the compile output     │
└────────────────────────────────┬─────────────────────────────────┘
                                 │ (deploy)
                                 ▼
┌──────────────────────────────────────────────────────────────────┐
│  DEPLOY                                                           │
│  - New compiled artefact ships to production                      │
│  - dspy.compile_version on every trace = the deployed version     │
│  - The new traces feed the next loop iteration                    │
└────────────────────────────────┬─────────────────────────────────┘
                                 │
                                 └─►  goto PRODUCTION
```

## What makes the loop honest

- **One dataset artefact, two consumers**. The same JSONL feeds the
  eval and the compile. Diverging the two is the most common failure
  mode in agentic stacks and ours is structurally immune.
- **Annotations are inputs, not outputs**. We don't display Sachbearbeiter
  scores on a dashboard except aggregated by queue. The judgement
  itself is the artefact; we don't gamify it.
- **A trace is a hypothesis-test**. Every span answers "what did this
  component decide, and on what evidence?" The cost/latency/quality
  attributes are first-class; logging the inputs and outputs alone is
  not enough.

## Extending the loop

### Adding a new task type

1. Define a rubric in `judges.py::_SHIPPED_RUBRICS`. The rubric is
   German, written for DeepSeek V3, output forced to strict JSON.
2. Add a starter dataset at `evals/datasets/<task>.jsonl`. Minimum 10
   items; aim for 50 within a month.
3. Wire the task into the rubric library:
   `human_review.py::_rubric_id_for_queue` maps queue-prefixes to
   rubrics; add yours.
4. Update `eval-on-pr.yml`'s matrix.
5. Update `dashboards/putsch_model_routing.json` if the task uses a
   new model.

### Adding a new annotation flagging trigger

Edit `runners.py::_should_flag`. Triggers are first-class to keep them
explicit and reviewable. Anti-pattern: a trigger that always flags ("we
might miss something!") — flooding the queue is just as bad as
flagging nothing.

### Adding a new model to the routing dashboard

1. Add the (input, output) tuple to `integrations/_base.py::CostCalculator._MAP`.
2. Add the corresponding fields to `PricingPerMillionTokens` in `config.py`.
3. Re-run `putsch-obs-dashboards-apply` to refresh
   `putsch_model_routing.json`.

### Promoting annotations more aggressively

By default we promote items with `status ∈ {APPROVED, NEEDS_REVISION}`.
We do not promote `REJECTED` items because a rejection often means "this
prompt is wrong" not "this answer is wrong" — promoting it as a training
row would teach the model the wrong lesson. Override only with a
deliberate audit trail.

## Anti-patterns

| Anti-pattern                                  | Why it's wrong                                            |
| --------------------------------------------- | --------------------------------------------------------- |
| Editing Langfuse Datasets only in the UI      | Drift: the deployed model trained on a different set      |
| Using Langfuse "scores" as a dashboard metric | Sachbearbeiter throughput becomes a Goodhart target       |
| Promoting `REJECTED` annotations              | Teaches the model the wrong answer                        |
| Running eval against a target without OTel    | Cost / latency are missing; you can't diff trade-offs     |
| Disabling redaction "just for this eval"      | Mixed-redacted/raw dataset is a discovery-rule disaster   |
