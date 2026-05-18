# `putsch-obs` — Observability + Evaluation Backbone

> The flywheel module of the Putsch agentic stack. Self-hosted Langfuse, OTel-native, GDPR-by-construction.

```
                       ┌──────────────────────────────────────────────┐
                       │             putsch-obs flywheel              │
                       │                                              │
       production ─►───┤  1. every run emits an OTel trace            │
       traffic         │  2. trace lands in self-hosted Langfuse      │
                       │  3. Sachbearbeiter annotates flagged traces  │
                       │  4. annotations → versioned eval dataset     │
       compiled  ◄──┐  │  5. DSPy GEPA compiles against the dataset   │
       prompts      │  │  6. compiled artefact deploys to production  │──┐
       & weights    │  │                                              │  │
                    └──┴──────────────────────────────────────────────┘  │
                                                                         │
                       ▲────────────────── goto 1 ◄──────────────────────┘
```

Without this loop, the agentic stack stagnates. With it, Putsch's agents
improve every week from their own production data. **This is the
compounding moat.** The SDK is the means; the flywheel is the value.

---

## What this is

A Python package and a deployment recipe that instrument every layer of
the Putsch agentic stack — CrewAI, LangGraph swarm, DSPy, LiteLLM,
Docling, Zep+Graphiti — and ship every trace, every cost, every
evaluation score into a self-hosted **Langfuse** running in the Putsch
Frankfurt VPC.

It is *not* a generic LLM-observability layer. It enforces opinions:

| Decision                       | Why                                                       |
| ------------------------------ | --------------------------------------------------------- |
| **Langfuse self-hosted**       | MIT, full feature parity vs. cloud, EU-sovereign          |
| **OTel as wire protocol**      | Swap to Tempo/Jaeger later as a config change             |
| **PII redaction at the SDK**   | Data must never leave the application boundary unredacted |
| **Reversible tokenization**    | Auditor un-redaction with append-only WORM audit chain    |
| **Datasets live in git**       | UI-only edits cause drift and kill reproducibility        |
| **Annotations → training set** | Every Sachbearbeiter judgement is a labeled example       |

ADR-004 in `docs/ADR-004.md` records the full rationale.

---

## Quick start

```bash
# 1. install
uv venv && uv pip install -e ".[integrations,dev]"

# 2. point at the Frankfurt Langfuse instance
cp deploy/langfuse/.env.template .env
$EDITOR .env   # set LANGFUSE_HOST, LANGFUSE_PUBLIC_KEY, LANGFUSE_SECRET_KEY,
               #     PUTSCH_OBS_VAULT_DSN, OTEL_EXPORTER_OTLP_ENDPOINT, ...

# 3. instrument any Putsch service
python -c "from putsch_obs import init; init(service_name='ap-crew')"
```

After `init()`:

- Every `litellm.completion(...)` call appears as a Langfuse generation,
  with token + EUR cost, latency, masked PII inputs/outputs.
- Every CrewAI agent step is a span; tasks, tools, outputs all chained.
- Every LangGraph node entry/exit is a span; `interrupt()` points show
  as `event` attributes; state mutations are recorded as diff payloads.
- Every DSPy `Predict` records its signature hash, optimizer version,
  and per-call score.
- Docling extractions record page count, OCR confidence, fallback
  triggers.
- Zep/Graphiti memory queries record episode count, traversal depth,
  temporal validity windows.

---

## Self-hosting Langfuse in the Putsch Frankfurt VPC

```bash
cd deploy/langfuse
cp .env.template .env && $EDITOR .env
docker compose up -d
./healthcheck.sh          # blocks until /api/public/health == ok
```

Provisioning the underlying Hetzner Frankfurt resources (cloud VM +
object storage for blob backups) is in `deploy/langfuse/terraform/`.
The Helm chart is intentionally not used: Postgres + ClickHouse +
MinIO + Langfuse server is small enough that `docker compose` on a
single Hetzner CCX33 (or HA pair behind a Hetzner LB) is the right
shape for ≤ 10⁹ spans/year.

Backups: `backup-cron.sh` ships nightly Postgres + ClickHouse dumps to
Hetzner Object Storage (S3-compatible), encrypted with `age` using a
KMS-managed key. Retention is enforced both at the ClickHouse TTL
layer (per-trace-type, see below) and at the object store.

---

## EU AI Act + DSGVO

| Concern                            | Where it's enforced                                                                  |
| ---------------------------------- | ------------------------------------------------------------------------------------ |
| Art. 12 AI Act log retention       | `ClickHouse TTL` per trace-type (HR/payroll 10y, limited-risk 3y, dev 90d)           |
| Art. 30 GDPR record-of-processing  | `putsch_obs.dsgvo` generates the Verzeichnis from the service registry on every push |
| Art. 5 GDPR data minimisation      | Default-redact-everything-uncertain in `putsch_obs.redaction`                        |
| Art. 32 GDPR auditability          | Append-only WORM audit log in the vault DB, hash-chained per row                     |
| BetrVG §87 Betriebsrat involvement | Annotation queues route to a Sachbearbeiter role, not individuals; no scoring of BR  |

PII redaction is fail-closed: if the redactor errors, the span is
dropped, not exported. Every un-redaction is recorded in the
`audit_trail` dashboard and is itself a Langfuse event.

---

## The eval loop in detail

```
evals/datasets/*.jsonl   ← git-versioned, reviewed in PRs
        │
        ▼
putsch-obs-eval --dataset invoice_extraction --target ap-crew@v3
        │
        ├─► spans go to Langfuse as a dataset-run
        ├─► LLM judge (DeepSeek V3 via Mistral La Plateforme EU)
        │     scores per item against the rubric
        ├─► Sachbearbeiter annotation queue is populated with
        │     low-confidence + flagged items
        └─► result diff posted on the PR; regressions block merge
```

DSPy compilation reads the same dataset on the next training run, so
the eval set and the training set are the same artefact. There is no
"silent dataset" hiding in the UI.

---

## Observability discipline

A trace is **not** a log. A trace is a hypothesis-test artefact. Every
span should answer: *what did this component decide, and on what
evidence?* Logging the inputs and outputs is necessary but not
sufficient — log the **decision** and its **justification**.

Cost, latency, and quality are first-class trace attributes, not
afterthoughts. Every LLM span carries:

- `gen_ai.usage.input_tokens` / `output_tokens` (OTel-standard)
- `gen_ai.usage.cost_eur` (priced via `putsch_obs.config.PRICING`)
- `putsch.quality_score` (if a Langfuse eval is attached)
- `putsch.routing.decision` and `putsch.routing.justification`

This is the contract for every integration in `src/putsch_obs/integrations/`.

---

## Repository map

| Path                                 | What lives there                                                  |
| ------------------------------------ | ----------------------------------------------------------------- |
| `src/putsch_obs/`                    | the SDK — `init()`, redaction, eval harness, dashboards           |
| `src/putsch_obs/integrations/`       | thin per-framework wrappers (CrewAI, LangGraph, DSPy, …)          |
| `src/putsch_obs/dsgvo/`              | Art. 30 GDPR record-of-processing generator                       |
| `evals/datasets/`                    | JSONL eval datasets, git-versioned                                |
| `deploy/langfuse/`                   | docker-compose, terraform stub, backup cron                       |
| `deploy/otel/`                       | OTel collector config with PII redaction pipeline                 |
| `docs/runbook.md`                    | on-call playbook for the 5 most likely failures                   |
| `docs/ADR-004.md`                    | architectural decision record for this module                     |
| `docs/flywheel.md`                   | extended explanation of the eval loop and how to extend it        |
| `tests/`                             | unit + integration + perf + chaos                                 |

---

## Performance budget

- `<2 ms` p99 added per LLM call
- `<500 µs` p99 added per tool call
- OTel `BatchSpanProcessor` with bounded queue + drop-on-overflow,
  drop counter exported as a metric

Asserted in `tests/perf/`.

---

## When you'd reject this

- You need a managed offering — Langfuse Cloud is not in scope here
  (data residency).
- You want browser-agent trace UX. Layer Laminar (Apache 2.0)
  alongside; do not replace.
- You want a closed-source vendor lock. That's the opposite of why
  this module exists.

---

## License

MIT. See `LICENSE`.
