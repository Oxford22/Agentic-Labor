# ADR-005 — Temporal knowledge graph memory: Graphiti on Neo4j

* **Status:** Accepted
* **Date:** 2026-05-18
* **Authors:** Putsch Platform Engineering
* **Supersedes:** —
* **Superseded by:** —
* **Stack position:** Memory layer for the CrewAI + LangGraph hybrid stack
* **Related ADRs:** ADR-001 (orchestration runtime), ADR-002 (observability), ADR-003 (document layer), ADR-004 (extraction compiler)

---

## 1. Context

The Putsch agentic stack needs a memory layer that all crews (AP, Mahnverfahren,
Stammdaten, Zoll) read from and write to. The memory must answer questions that
are intrinsically *temporal* and *relational*:

* "What payment terms did vendor L-4711 have in Q2 2025, before the master-data
  reorg?"
* "Who was the responsible Sachbearbeiter for customer K-2230 in 2024, and what
  was the escalation history?"
* "What did the system know about employee X at the time it routed invoice Y?"
  (Betriebsrat / §87 BetrVG question — must have a defensible answer.)
* "Reconstruct the agent's belief state at trace `lf-trace-7c2a…`."
  (EU AI Act Art. 12 — logging and traceability of high-risk AI systems.)

The memory layer is also the **second moat** of the stack, after observability:
without it, every agent run starts cold. With it, the system accumulates
institutional knowledge about Putsch's vendors, customers, masters, and
processes — knowledge that compounds and which a competitor would have to
re-discover from scratch.

### 1.1 Hard constraints

| ID  | Constraint                                                                                 |
| --- | ------------------------------------------------------------------------------------------ |
| C1  | **Temporal:** every fact has a validity window; never overwrite, always supersede.         |
| C2  | **Relational:** the schema must mirror Putsch's actual entity-relationship reality.        |
| C3  | **Self-hostable:** Frankfurt VPC, no external storage, no cross-border replication.        |
| C4  | **Runtime-neutral:** must compose with LangGraph (our chosen runtime); cannot *be* one.    |
| C5  | **Auditable:** every write carries source, agent identity, and Langfuse trace correlation. |
| C6  | **Right-to-be-forgotten:** cascading deletion of natural-person facts with audit trail.    |
| C7  | **Mature:** Apache 2.0, > 10 k GitHub stars, multiple production deployments cited.        |
| C8  | **German-business shaped:** copes with SAP master data drift across subsidiaries.          |

## 2. Options considered

### 2.1 Graphiti on Neo4j (chosen)

* Apache 2.0, ~24 k stars, dual-licensed Zep server on top for managed UX.
* Native bitemporal: stores `valid_from` / `valid_to` plus `created_at`.
* Episode-based ingestion model: agents append episodes after each task,
  Graphiti extracts entities + relations and merges them into the graph.
* LongMemEval (ICLR 2025): **71.2 %** on GPT-4o.
* Neo4j is operationally heavier than Postgres, but already-planned for the
  Palantir-grade stack. We are not adding a new datastore class.

### 2.2 Mem0 (rejected)

* Faster setup, smaller token footprint per conversation (~1.8 k vs ~600 k).
* **Flat fact store.** No native validity windows, no graph traversal, no
  supersede semantics.
* LongMemEval: 49.0 % on GPT-4o — a **22-point** deficit driven specifically by
  temporal reasoning.
* The "memory" is essentially a dedup'd embedding cache. To answer
  "who-owned-this-before-Q1" you would re-implement temporal validity yourself
  in application code. We have been here before: it ends in a half-broken
  bespoke schema on top of a system that was the wrong shape from day one.
* **Why this ADR exists:** Mem0's simplicity will tempt whoever inherits this
  codebase in two years to "rip out the graph and just use Mem0". The reasoning
  goes "we don't use most of the temporal features". That is selection bias —
  you don't use them *because the queries that need them silently degrade or
  are never written*. The Betriebsrat question is rare *and* unanswerable
  without temporal validity. Rare-and-critical is the worst possible profile
  to optimize away.

### 2.3 Letta (rejected)

* Strong on temporal memory, but Letta is a *runtime* — it owns agent state,
  the control loop, and tool dispatch.
* Conflicts directly with our chosen runtime (LangGraph). Adopting Letta means
  re-running ADR-001 and unwinding the Magentic-One swarm orchestrator.
* The cost of "runtime split-brain" is far higher than the cost of running
  Graphiti as a passive store.

### 2.4 Vector-only RAG (rejected)

* No relational structure → constant entity disambiguation failures (Putsch
  has the same vendor in three SAP instances under three numbers).
* No temporal structure → cannot answer audit questions.
* Hallucinates on similar entities. "Müller GmbH" matches three real vendors
  in the cosine space and the agent picks one at random.
* Adequate as a *complement* to a graph (we use vector search inside Graphiti
  for fuzzy entity matching) but not as the substrate.

### 2.5 Cognee (rejected, kept as fallback)

* Interesting local-first option with full graph reasoning.
* Smaller community, less production usage, no published German enterprise
  references. Risk-adjusted, not the right pick today; revisit at v2.

### 2.6 OMEGA (rejected)

* Impressive LongMemEval numbers but ICLR 2025 paper-fresh — no production
  deployments at the scale we need. Re-evaluate in 18 months.

## 3. Decision

**Adopt Graphiti on self-hosted Neo4j 5 Enterprise, in the Frankfurt VPC.**
Zep (the managed layer) is *optional* and only added later if Putsch wants
the SaaS convenience — the OSS Graphiti engine alone covers our needs.

### 3.1 Why Neo4j specifically (over Memgraph, JanusGraph, ArangoDB)

* Graphiti's reference deployment and the libraries it depends on
  (`graphiti-core` + the `neo4j` driver) are first-class on Neo4j.
* APOC procedures are available and battle-tested.
* Online backup with point-in-time recovery is a paid Enterprise feature we
  are willing to pay for given the audit posture. (The OSS Community edition
  does not have online backup — for a memory store of record this is
  non-negotiable.)
* Cypher is the query language we are already training operators on; the
  alternative graph DBs would introduce a second query dialect.

### 3.2 Why temporal-by-default (instead of opt-in)

* Opt-in temporal-validity is the same trap as opt-in observability: in
  practice it's never opted in, and you discover the gap at the moment you
  need to answer the audit question.
* Adding validity windows to existing facts retroactively is *not possible*
  — the original `valid_from` is unrecoverable.
* The cost of temporal-by-default is one extra index and ~24 bytes per fact.
  This is irrelevant at our scale.

### 3.3 Why personnel memory is isolated

* `Mitarbeiter` data is Art. 9 GDPR–adjacent (employment) and §87 BetrVG–
  governed (works council co-determination on employee monitoring).
* Co-locating personnel facts with vendor/customer facts means any RBAC
  failure exposes employee data through a vendor-lookup path. Even with
  perfect RBAC, the *architectural posture* — "personnel data is just
  another fact in the graph" — is the wrong story to tell the Betriebsrat.
* Separate namespace + separate driver credentials + read-side audit log is
  the kind of design choice that survives a works council audit. The other
  way doesn't.

### 3.4 Why we reserve a `raw_observation` episode type

* Putsch's reality is messier than any schema we can write on day one.
* Agents that encounter a fact they cannot classify must have an escape
  hatch — otherwise they either drop the fact or invent a malformed entity.
* Reserving `raw_observation` (untyped, with text body + provenance) lets us
  *defer schema decisions until the second customer*. This is the same
  reason Foundry's ontology has a "loose" tier and the same reason Stripe's
  events table has a `data jsonb` column.

## 4. Consequences

### 4.1 Positive

* **Defensible audit answers** for every question we expect from
  Betriebsrat, Wirtschaftsprüfer, and regulators.
* **Agent reasoning quality compounds over time** — the longer the system
  runs, the better its decisions about familiar vendors and customers.
* **Cross-site reconciliation becomes tractable** — Hagen / Asheville /
  Poggibonsi / Valladolid disagreements are first-class objects we can
  surface, not silent corruption.
* **Replayability** as a real test in CI (`reconstruction_accuracy.py`),
  not a hand-wave.

### 4.2 Negative

* **Operational overhead.** Running Neo4j Enterprise with online backups is
  more work than running Mem0's hosted SaaS or a vector DB. Mitigated by:
  Terraform-managed Hetzner node, runbook, alerting, monthly restore drill.
* **Token footprint risk.** Naïve full-graph traversal into a prompt can
  exceed 600 k tokens per conversation. Mitigated by:
  bounded queries (`max_depth`, `max_results`), Graphiti's episode
  summarization, and never passing raw traversals into prompts — only the
  summarised, time-filtered slice.
* **Schema evolution discipline.** Adding a new entity type touches
  `ontology.py`, the eval set, and the runbook. We will pay for this
  discipline in slower feature velocity; we get audit defensibility back.
* **Cost.** A 64 GB NVMe Hetzner dedicated node + Neo4j Enterprise license is
  ~3–5× the cost of "just Postgres". This is a deliberate trade.

### 4.3 Neutral

* Knowledge graph engineering is its own subspecialty — we will need to
  hire or train at least one engineer who can hold the ontology in their
  head. This is true for any serious memory system.

## 5. Compliance hooks

| Regulation                | Implementation                                            |
| ------------------------- | --------------------------------------------------------- |
| GDPR Art. 5(1)(c) (minimisation) | Personnel namespace; shorter retention; ontology review |
| GDPR Art. 17 (RTBF)       | `gdpr.cascade_forget()` with audit tombstones             |
| GDPR Art. 30 (records)    | Every fact: source_system, written_by_agent, trace_id     |
| GDPR Art. 32 (security)   | Frankfurt-only, TLS, RBAC, read-side audit on personnel   |
| EU AI Act Art. 12 (logs)  | `reconstruction_accuracy.py` proves replay correctness    |
| EU AI Act Art. 14 (oversight) | Low-confidence facts gated behind Sachbearbeiter UI    |
| §87 BetrVG (works council)| Personnel namespace isolation, surfaced in DPIA           |

## 6. Reversal criteria

We will **revisit this decision** if any of the following are true:

* Neo4j p95 read latency exceeds 200 ms at our production volume for more
  than two consecutive weeks despite tuning. (We then evaluate Memgraph.)
* A future runtime decision (post-LangGraph) makes Letta strictly superior
  and the migration cost is bounded.
* Graphiti's maintenance velocity drops materially (no commits for > 3
  months, security CVEs unpatched). We then evaluate forking or migrating.
* The temporal correctness eval drops below 80 % on our German-business set
  due to upstream Graphiti changes. P0 incident; consider pinning + forking.

We will **not** revisit this decision because:

* "Mem0 is faster to set up" — yes, and that is the wrong axis to optimise.
* "We don't seem to use the temporal queries much" — see §2.2.
* "Neo4j is a lot of operations" — yes, and we accepted that knowingly.
