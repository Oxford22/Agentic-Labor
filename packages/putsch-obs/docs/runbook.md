# `putsch-obs` Runbook

> 3am-pager-friendly. Required reading for every on-call rotation.

> The dashboards in `src/putsch_obs/dashboards/` are the *first* place
> you look. The runbook is for when the dashboards have told you what is
> broken and you need to fix it.

## On-call expectations

- Severity → response time:
  - **critical** (PagerDuty page): 15 min ack, 1 h restore-or-mitigate
  - **high** (Teams ping): 30 min ack, 4 h restore-or-mitigate
  - **warning** (Teams thread): 4 h ack, next business day
- Every page must produce either:
  - a code change merged within 5 days, OR
  - a runbook update explaining why no code change is warranted

---

## 1. Langfuse is down

**Symptoms**

- `putsch_ap_kpis` dashboard frozen, last-update > 5 min ago.
- Application logs show `instrumentation.export_failed` at WARN.
- `putsch_drift.dropped_spans` rising sharply.

**Triage**

```bash
ssh langfuse-fra-1
docker compose ps                          # all healthy?
docker compose logs --tail=200 langfuse-web
curl -sf http://localhost:3000/api/public/health || echo "DOWN"
```

**Most common causes**

| Cause                           | Fix                                                      |
| ------------------------------- | -------------------------------------------------------- |
| ClickHouse OOM-killed           | §2 below                                                 |
| Postgres connection-pool tapped | `docker compose restart langfuse-web` (restarts pool)    |
| Redis lost connection           | `docker compose restart redis`; check `requirepass` env  |
| Disk full on data volume        | §2 below                                                 |
| MinIO unhealthy                 | `docker compose restart minio`                           |

**The instrumentation is fail-safe**: application traffic continues
without trace export. The flywheel pauses but the business does not.
**Do not panic-roll the application services.**

**Resolution**

1. Bring the failing component back. If it's a config issue, fix and
   commit. *Never* edit `.env` in place on the host without committing
   the encrypted version to git.
2. Once `/api/public/health` returns 200, the in-flight OTel queue
   drains automatically (it's bounded; some older spans may be dropped).
3. Mark resolved in the incident channel with the time-to-restore.

---

## 2. ClickHouse disk full / OOM

**Symptoms**

- `clickhouse` container in `unhealthy` state.
- ClickHouse logs: `Cannot reserve … bytes`, `Too many parts`, or
  `MEMORY_LIMIT_EXCEEDED`.

**Triage**

```bash
df -h /var/lib/docker         # the data volume
docker stats clickhouse       # memory headroom
docker compose exec clickhouse \
  clickhouse-client --query "SELECT name, formatReadableSize(sum(bytes_on_disk)) AS size \
                              FROM system.parts WHERE active GROUP BY name ORDER BY sum(bytes_on_disk) DESC"
```

**Fixes**

- **Disk full**: the volume is sized 500 GiB by default (see
  `terraform/variables.tf::clickhouse_volume_gb`). Either:
  - Expand the volume via `hcloud volume resize` (no downtime) and run
    `resize2fs` inside the container's mount, OR
  - Force-materialize old TTL parts:

    ```sql
    ALTER TABLE traces MATERIALIZE TTL;
    ```

  This evicts rows older than the retention class. **Verify** the
  retention class is correct on the table before running this — if it
  was misconfigured, you'll vaporize traces you need to keep.

- **OOM**: bump `MARK_CACHE_SIZE` in ClickHouse config. Don't trim the
  TTL — that's an irreversible loss.

**Prevention**

- The drift dashboard's "dropped_spans" widget alerts at 100/hour. If
  you ever see drops, capacity-plan *before* the disk fills.

---

## 3. PII redaction is misfiring

There are two failure modes:

### 3a. False positive — redacting things that aren't PII

**Symptom**: a Sachbearbeiter says a trace is unreadable because legitimate
fields are tokenized.

**Triage**

```bash
docker compose exec postgres-vault psql -U putsch_vault_writer -d putsch_vault \
  -c "SELECT category, count(*) FROM putsch_vault.tokens GROUP BY category;"
```

A spike in `custom` (LLM-flagged) tokens often means the Qwen3 redactor
has drifted. Check the `audit_trail` dashboard for the most common
categories over the last hour.

**Fix**

- For a deterministic-pattern false positive, edit
  `src/putsch_obs/redaction.py::_DETERMINISTIC_PATTERNS`. Add the
  counter-case to `tests/fixtures/pii_corpus.py` as a negative.
- For an LLM false positive, capture the bad span via
  `putsch-obs unredact --token <T> --reason "investigation"` (audited),
  add it to the redactor's bad-case dataset, and re-prompt-tune the
  Qwen3 redactor on next training cycle.

### 3b. False negative — PII leaking into traces

**Symptom**: the `audit_trail.events_by_category` widget shows nothing
but the `putsch_ap_kpis` view contains an unredacted IBAN or email.

**This is a P0 incident.** Process:

1. **Containment**. Pause the offending service (`docker compose stop`).
   The instrumentation will buffer spans; the application is unaffected.
2. **Notification**. Tag `@datenschutz` in the incident channel. The
   72-hour Art. 33 GDPR clock starts now.
3. **Investigation**. Find the leak path. Two common ones:
   - The application stuffed raw PII into an attribute *not* in the
     allowlist but *not* a typical text field (e.g. a list of dicts).
     The deterministic stage only runs on strings.
   - The OTel collector's regex backstop didn't match because of an
     encoding subtlety. Capture the raw payload and add a test case.
4. **Eradication**. Patch the redactor / collector. Test with the
   reproducer. **No band-aid**.
5. **Recovery**. Purge the offending ClickHouse parts via `DELETE FROM
   traces WHERE …`. Run the chain-verify CLI on the vault audit log to
   confirm integrity.

---

## 4. Eval regression on main

**Symptom**: a merged PR's post-deploy run shows mean_score down by
> 5% on a dataset.

**Triage**

```bash
# Last 5 dataset runs for the affected dataset:
curl -sf "$LANGFUSE_URL/api/public/dataset-runs?datasetName=$DATASET&limit=5" \
  -u "$LANGFUSE_PUBLIC_KEY:$LANGFUSE_SECRET_KEY" | jq .
```

Cross-reference the timestamps with `git log` on the affected service.

**Fix**

1. Revert the offending merge. (Yes, revert. Don't patch forward when a
   revert is cheap.)
2. Re-run the eval against `main` to confirm the regression is gone.
3. Open a follow-up issue requiring the original change to ship with a
   passing eval before re-merge.

---

## 5. Model-routing config drift

**Symptom**: `putsch_model_routing.cost_by_model` shows a tier (e.g.
`mistral-large-latest`) far above its expected share, or `accuracy_by_model`
shows the wrong model for a task type.

**Triage**

```bash
git log -p -- models.py routing.py
```

Compare what's in production (last released tag) with what the dashboard
shows. Discrepancy means either:

- The routing-config service didn't pick up the new release (most common
  cause). Restart it.
- Someone bypassed routing by calling LiteLLM with an explicit `model=`.
  Search logs for direct LiteLLM calls outside the routing wrapper.

**Fix**

- Re-deploy with the correct routing config.
- If bypass: open a CR enforcing routing at the LiteLLM proxy layer
  (deny direct `model=` overrides that aren't on the allowlist).

---

## Vault chain-verify

```bash
docker compose exec postgres-vault psql -U putsch_vault_auditor -d putsch_vault \
  -At -c "SELECT row_to_json(t) FROM (SELECT * FROM putsch_vault.audit_log ORDER BY id) t" \
  | uv run python -c "
import json, sys
from putsch_obs.vault.audit import verify_chain
rows = [json.loads(l) for l in sys.stdin if l.strip()]
print('OK' if verify_chain(rows) else 'BROKEN')
"
```

If `BROKEN`, **escalate to Datenschutzbeauftragte immediately**. Do not
shut the vault down — the broken row is itself evidence.

---

## Pager-friendly cheatsheet

| Symptom                      | First command                                         | Most likely fix                           |
| ---------------------------- | ----------------------------------------------------- | ----------------------------------------- |
| Dashboards frozen            | `docker compose ps` on `langfuse-fra-1`               | Restart `langfuse-web`                    |
| Spans dropping > 100/hr      | `docker stats clickhouse`                             | ClickHouse OOM → bump cache size          |
| `audit_trail` empty + alerts | Tail `langfuse-worker` logs                           | Worker stuck → restart                    |
| Eval failing in CI           | Check the diff comment on the PR                      | Roll back the offending commit            |
| Cost spike alert             | `putsch_model_routing.cost_by_model` over last hour   | Inspect routing logs for fallback storms  |
| `redaction failed_closed`    | Tail `instrumentation.span_dropped_on_redaction`      | Bring Qwen3 redactor back; never bypass   |
