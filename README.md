# putsch-memory

**Temporal knowledge graph memory backbone for the Putsch agentic stack.**
Self-hosted Graphiti on Neo4j 5 Enterprise, Frankfurt VPC, GDPR + EU AI Act
Art. 12 compliant. The substrate every other agent reads from and writes to.

> If you are about to "simplify this to Mem0", read `docs/adr/ADR-005-zep-graphiti-temporal-memory.md`
> first. It exists specifically to stop you. Mem0 is flat. Putsch is not.

---

## Why a graph, not a vector store

Putsch's back office is *relational*:

```
                ┌──────────────┐                ┌──────────────┐
                │  Lieferant   │ SUPPLIES ───▶  │   Material   │
                └──────┬───────┘                └──────┬───────┘
                       │                                │
                INVOICES                            ORDERS
                       │                                │
                       ▼                                ▼
                ┌──────────────┐  BELONGS_TO_PERIOD  ┌──────────────┐
                │   Rechnung   │ ──────────────────▶ │ Bestellung   │
                └──────┬───────┘                     └──────┬───────┘
                       │                                     │
                       │  AUDITED_BY                         │
                       ▼                                     ▼
                ┌──────────────┐    RESPONSIBLE_FOR   ┌──────────────┐
                │Wirtschafts-  │ ◀─────────────────── │  Mitarbeiter │
                │  prüfer      │                      │  (isolated)  │
                └──────────────┘                      └──────────────┘
```

A vector store flattens this into "similar text". It cannot answer
**"who owned this account before Q1?"** because it has no concept of *who* or
*before*. A knowledge graph keeps the structure. A *temporal* knowledge graph
keeps the structure **as it was on any past date**.

## Why temporal by default

Every fact in `putsch-memory` is stored with a **validity window**:

```
fact: Lieferant L-4711  payment_terms = NETTO_30
      valid_from = 2024-01-01
      valid_to   = 2025-03-31     (superseded by reorg)
      source_system = SAP-HAGEN
      written_by_agent = sap_sync_v1
      written_at_trace_id = lf-trace-7c2a...
```

Facts are never overwritten — they are **superseded**. That guarantees:

1. **Audit replay (EU AI Act Art. 12):** for any past agent decision, we can
   reconstruct exactly what the system *believed* at that moment.
2. **Betriebsrat answerability:** "What did the system know about employee X
   when it made decision Y?" has a single, defensible answer.
3. **Cross-period reasoning:** "Vendor X's payment terms in Q2 2025" is a
   first-class query, not an archaeology project.

## Why Graphiti, not Mem0 / Letta / vector-RAG

Short version (full ADR in `docs/adr/ADR-005`):

| System          | Temporal? | Graph? | Runtime conflict? | LongMemEval / GPT-4o |
| --------------- | :-------: | :----: | :---------------: | :------------------: |
| **Graphiti**    |     ✔     |    ✔   |        no         |    **71.2 %**        |
| Mem0            |     ✘     |    ✘   |        no         |      49.0 %          |
| Letta           |     ✔     |    ✔   | yes (own runtime) |      n/a             |
| Vector-only RAG |     ✘     |    ✘   |        no         |      < 40 %          |

Graphiti is Apache 2.0, open source, runs in our Frankfurt VPC, and composes
cleanly with LangGraph (our runtime) and CrewAI (our crew layer). It is the
only option that satisfies all four hard constraints: temporal, relational,
self-hostable, and runtime-neutral.

---

## Package layout

```
putsch-memory/
├── pyproject.toml
├── README.md
├── deploy/memory/                 # Self-hosted Neo4j + Graphiti, Frankfurt VPC
│   ├── docker-compose.yml
│   ├── terraform/                 # Hetzner provisioning, 64 GB NVMe
│   ├── backup/neo4j-backup.sh     # Point-in-time online backup
│   └── health/                    # Liveness + readiness probes
├── src/putsch_memory/
│   ├── ontology.py                # The constitution. Read this first.
│   ├── graphiti_client.py         # Async wrapped client, bounded queries
│   ├── conflicts.py               # Cross-site disagreement handling
│   ├── gdpr.py                    # Personnel isolation, RTBF, residency
│   ├── tools/                     # CrewAI Tools + LangGraph nodes
│   ├── writers/                   # Typed per-crew episode writers
│   ├── ingestion/                 # SAP / DATEV / email / manual
│   └── eval/                      # Temporal, disambiguation, reconstruction
├── tests/                         # pytest + hypothesis + chaos
└── docs/
    ├── runbook.md
    └── adr/ADR-005-zep-graphiti-temporal-memory.md
```

## Quickstart (local dev)

```bash
# 1. Stand up Neo4j + Graphiti
docker compose -f deploy/memory/docker-compose.yml up -d

# 2. Install the package
pip install -e ".[dev,crewai,langgraph,observability]"

# 3. Bootstrap the ontology indexes
putsch-memory-migrate up

# 4. Run the temporal correctness eval
putsch-memory-eval temporal_correctness --top-k 5
```

## Quickstart (agents using the memory)

```python
from putsch_memory import MemoryClient, ProvenanceContext
from putsch_memory.writers import APEpisodeWriter

async with MemoryClient.from_env() as memory:
    # Read — temporally aware
    vendor = await memory.tools.lookup_vendor(
        ust_id_nr="DE123456789",
        as_of="2025-04-01T00:00:00Z",
    )
    # → vendor master data as it was on 1 Apr 2025

    # Write — provenance mandatory
    async with ProvenanceContext(
        source_system="agent:ap_crew",
        written_by_agent="ap_crew/v3",
        trace_id="lf-trace-7c2a...",
    ):
        await APEpisodeWriter(memory).write(
            rechnung_id="RE-2026-04-1847",
            bestellung_id="4500001234",
            account="1200",
            posted_at="2026-04-15T14:23:00Z",
        )
```

The writer enforces schema, idempotency, and provenance at the SDK boundary.
There is no way to put an anonymous fact into the graph from agent code.

---

## Hard guarantees this module makes

| Guarantee                              | How it's enforced                                  |
| -------------------------------------- | -------------------------------------------------- |
| No overwritten facts                   | All writes go through `supersede()`; tested with hypothesis |
| No anonymous facts                     | `ProvenanceContext` required at SDK boundary       |
| No unbounded traversals                | `max_depth` + `max_results` enforced client-side   |
| No cross-border data movement          | Neo4j pinned to Frankfurt; no replication targets  |
| Personnel data isolated                | Separate `personnel_memory` namespace + RBAC       |
| Right-to-be-forgotten                  | `gdpr.cascade_forget()` + tombstone audit trail    |
| Idempotent writes                      | `idempotency_key = sha256(source, src_id, ts)`     |
| Degraded-mode availability             | Circuit breaker → read-only cache + trace flag     |

## Operating envelope

- **Hot path target:** p95 `lookup_vendor` < 80 ms, p95 `temporal_query` < 200 ms.
- **Write path:** episodes batched every 250 ms by the writer pool.
- **Backup RPO:** 15 minutes (online backup + WAL ship).
- **Backup RTO:** 30 minutes from Frankfurt cold standby.

See `docs/runbook.md` for the on-call playbook.

## Compliance map

| Requirement              | Implementation                                          |
| ------------------------ | ------------------------------------------------------- |
| GDPR Art. 17 (RTBF)      | `gdpr.cascade_forget()`                                 |
| GDPR Art. 30 (records)   | Every write carries source, agent, trace; Cypher audit |
| EU AI Act Art. 12 (logs) | `eval/reconstruction_accuracy.py` proves replayability  |
| EU AI Act Art. 14 (human oversight) | Low-confidence facts gated on Sachbearbeiter UI |
| Betriebsrat (§87 BetrVG) | Personnel namespace isolation + read-side audit log     |

## License

Apache 2.0. See `LICENSE`.
