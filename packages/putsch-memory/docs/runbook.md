# putsch-memory — on-call runbook

This document is the on-call playbook for the temporal-memory layer.
Audience: platform engineer on rotation. Assumed access:
SSH bastion in the Frankfurt VPC, Grafana + Loki, ArgoCD, Hetzner
console, secret-manager Vault.

If you are reading this during an incident, jump straight to the
relevant section. The TL;DR is at the top of each.

---

## Architecture (60-second briefing)

```
agents ──▶ MemoryClient ──┬──▶ Graphiti service ──▶ Neo4j Enterprise (Frankfurt, NVMe)
                         │                              │
                         │                              └──▶ daily snapshot + 15-min online backup
                         │                                    └──▶ Frankfurt S3 (KMS encrypted)
                         └──▶ read-only TTL cache (degraded mode fallback)
```

* All data is in Hetzner Frankfurt (`fsn1`). No cross-border replication.
* Graphiti is a stateless service in front of Neo4j; restarting it is
  always safe.
* Neo4j is the only stateful component; treat it like the database it is.

---

## Alerts and what they mean

| Alert                                 | TL;DR action                                  |
| ------------------------------------- | --------------------------------------------- |
| `putsch_memory_breaker_open`          | Read path failing → see "Circuit breaker open" below |
| `putsch_memory_p95_high`              | Latency regression → see "Latency"            |
| `neo4j_backup_failures_total >= 2`    | Two consecutive backup failures → wake operator |
| `neo4j_pagecache_hit_ratio < 0.85`    | Hot-set no longer fits → see "Pagecache"      |
| `personnel_audit_chain_broken`        | DPIA-relevant integrity violation → escalate to DPO immediately |
| `temporal_correctness_eval_failed`    | CI gate; not a runtime alert. Block release.  |

---

## Health probes

Two scripts under `deploy/memory/health/`.

* **Liveness** — `neo4j-healthcheck.sh liveness` — used by docker
  compose. Detects "process hung but port up". Expected: < 1 s.
* **Readiness** — `neo4j-healthcheck.sh readiness` — checks that
  required indexes are ONLINE and a write tx round-trips. Expected:
  < 3 s. Run before declaring a restored node "in service".

Quick triage:

```bash
ssh putsch-memory-neo4j-prod
docker exec putsch-memory-neo4j bash -lc \
    'NEO4J_PASSWORD=$NEO4J_AUTH_PASSWORD /usr/local/bin/neo4j-healthcheck.sh readiness'
```

---

## Common scenarios

### Circuit breaker open / "memory_degraded"

**Symptom:** agents emitting `memory_degraded` trace attribute, Langfuse
spans tagged with `memory.state = "open"`, Grafana alert
`putsch_memory_breaker_open`.

**Triage:**

1. Is Neo4j up? `systemctl status putsch-memory.service`
2. Bolt reachable? `cypher-shell -a bolt://neo4j.prod.memory.internal:7687 -u neo4j -p $PW 'RETURN 1'`
3. Slow queries? Grafana dashboard `neo4j-slow-queries`. Check for an
   unbounded traversal that slipped past `max_depth` / `max_results`.
4. Disk full? `df -h /var/lib/neo4j/data` — full disk causes immediate
   write failure and breaker open.

**Resolution playbook:**

* If Neo4j is up and healthy, the breaker auto-recovers after
  `breaker_recovery_seconds` (default 30 s) by allowing probe traffic.
  If you confirm health and want to short-circuit recovery, restart
  the agent runtime (CrewAI processes own their breakers).
* If Neo4j is down, restart it: `systemctl restart putsch-memory.service`.
  Wait for readiness probe to pass before re-enabling agent traffic.
* If the cause is repeated unbounded queries, grep the Graphiti logs
  for "max_results_clamped" and identify the offending agent. Open a
  P1 — the breaker should never trip from this.

### Latency regression (p95 > 200 ms)

**Triage:**

1. `MATCH (n) RETURN labels(n)[0] AS l, count(*) AS c ORDER BY c DESC LIMIT 20`
   — has any label grown unexpectedly?
2. `CALL db.indexes() YIELD name, state, populationPercent` — every
   index `ONLINE` and `100%`?
3. APOC slow-log: `CALL apoc.log.list({since: 'PT15M'}) YIELD entry RETURN entry`
4. Page cache hit ratio: `:sysinfo` in Browser, or
   `dbms.cluster.overview()` for replica details.

**Resolution playbook:**

* If hit ratio < 0.85 sustained, the hot set has outgrown the page
  cache. Two options: (a) bump RAM and `server_memory_pagecache_size`,
  (b) reduce the hot set (often: trim how much episode data each agent
  pulls; tighten Graphiti's episode summarization).
* If an index is missing or in `FAILED` state, `DROP INDEX <name>` and
  re-create from `BUSINESS_GRAPH.cypher_constraints()`.

### Pagecache miss rate climbing

Means the hot working set no longer fits in RAM. This is the most
common growth-driven incident.

* Short term: reduce `max_query_depth` in the affected agent for a few
  hours while you investigate.
* Medium term: identify the largest-by-edge-count labels and consider
  whether you really need them in the hot path:
  ```cypher
  MATCH ()-[r]->() RETURN type(r) AS rel, count(*) AS c ORDER BY c DESC LIMIT 10
  ```
* Long term: scale the node up (next Hetzner tier) OR introduce a
  vector pre-filter before the graph walk for high-cardinality reads.

### Backup failure

A single failure is non-fatal (the backup loop is at-least-once and
retries every 15 min). Two consecutive failures wake on-call.

1. SSH the node. Tail `/backups/<latest>/backup.log`.
2. Common causes:
   * Out of disk on `/backups` → prune older backups, check the S3 sync
     is succeeding.
   * Neo4j is under heavy write load → schedule the backup window or
     reduce concurrency.
   * Consistency check tripped (rare; indicates real corruption) →
     escalate to P0, do NOT delete the failing backup, take a
     read-replica copy of the data dir for forensics.
3. After the underlying cause is fixed, run a manual backup to confirm:
   `docker exec putsch-memory-neo4j-backup /usr/local/bin/neo4j-backup.sh`.

### Backup restore drill (monthly)

This MUST be exercised monthly. The drill is the only proof your
backups work.

1. Spin up a `stage` Hetzner box from the Terraform module with the
   `environment = stage` var.
2. Pull the latest backup from S3:
   `aws s3 cp --recursive s3://putsch-memory-backups-frankfurt/<ts>/ /var/lib/neo4j/data/restore/`
3. Stop the stage Neo4j: `systemctl stop putsch-memory.service`
4. Restore: `neo4j-admin database restore --from-path=/var/lib/neo4j/data/restore/<ts> --overwrite-destination neo4j`
5. Start: `systemctl start putsch-memory.service`
6. Run the readiness probe, then run
   `putsch-memory-eval reconstruction_accuracy --strict`. If the eval
   passes at the same threshold as prod, the drill succeeded.
7. Tear the stage box down. Log the drill date in `#platform-runbook`.

### Personnel audit chain broken

**Symptom:** `verify_audit_chain()` returns False, or the alert fires.

**This is a P0.** It indicates either (a) a bug that corrupted the
chain, or (b) someone tried to tamper with the audit log.

1. Do NOT restart anything. Do NOT clean up "to make the alert go
   away". Tampering evidence is fragile.
2. Page the DPO and the platform lead.
3. Snapshot the personnel Neo4j volume (Hetzner console → snapshot).
4. Run `verify_audit_chain()` and capture the failing audit_id.
5. Use Cypher to identify the broken link:
   ```cypher
   MATCH (a:_PersonnelReadAudit {audit_id: $aid})
   RETURN a.prev_hash, a.self_hash
   ```
6. Cross-reference with the Loki audit-log stream (we duplicate to
   Loki so a graph-side break is detectable from outside the graph).

### Right-to-be-forgotten request

The Sachbearbeiter UI initiates this. Operator only intervenes if the
UI flow stalled.

1. Confirm with DPO that the request is valid (legal basis, identity
   verified).
2. The function `cascade_forget(client, ForgetRequest(...))` does the
   work. It runs against both `neo4j` and `personnel` databases.
3. The tombstone record (`_RTBFTombstone`) stays for
   `rtbf_audit_retention_days` (default ten years), per
   Handelsgesetzbuch / Steuerrecht retention windows.

### Schema migration (additive)

Routine, low-risk:

1. Update `ontology.py` on a feature branch.
2. CI runs `tests/test_migrations.py` to confirm no destructive
   constraint was emitted.
3. After merge, the next deploy auto-runs `putsch-memory-migrate up`
   as a hook; you do not need to run it manually unless ArgoCD's hook
   policy is disabled.
4. Monitor the temporal_correctness eval for regressions.

### Schema migration (destructive)

Stop. Read ADR-005 §3.4 again. Then:

1. Write a hand-crafted migration in `migrations/_destructive/`.
2. Drill it on stage. Twice. From two different backups.
3. Schedule a maintenance window. Take a backup *immediately* before
   running.
4. Run with `--require-confirmation` (refuses to proceed without a
   matching incident ticket id).
5. Post-migration: run all three eval suites with `--strict`. If any
   fails, restore from the immediate-pre backup.

---

## On-call cheat sheet

| Task                                    | Command                                                                 |
| --------------------------------------- | ----------------------------------------------------------------------- |
| Quick health                            | `./deploy/memory/health/neo4j-healthcheck.sh readiness`                 |
| Tail backup log                         | `tail -F /backups/$(ls -1 /backups | tail -1)/backup.log`               |
| Force a backup                          | `docker exec putsch-memory-neo4j-backup /usr/local/bin/neo4j-backup.sh` |
| Show breaker state (via metrics)        | Grafana → "putsch-memory" dashboard → "Breaker state" panel             |
| Run temporal eval                       | `putsch-memory-eval temporal_correctness --strict`                      |
| Verify audit chain                      | `python -c "import asyncio, putsch_memory as m; asyncio.run(m.gdpr.verify_audit_chain(...))"` |
| Run reconstruction eval                 | `putsch-memory-eval reconstruction_accuracy --strict`                   |
| Restart Graphiti only                   | `docker restart putsch-memory-graphiti`                                 |
| Drain agent writes (maintenance window) | Toggle `MEMORY_MAINTENANCE=true` in ArgoCD → agents flush + go read-only |

---

## Contacts

* Platform on-call: PagerDuty schedule "putsch-platform"
* DPO: dpo@putsch.example
* Betriebsrat-IT: betriebsrat-it@putsch.example (P0 if chain broken)
* Graphiti upstream issues: github.com/getzep/graphiti (do not file
  Putsch-specific data; reproduce on synthetic data)
