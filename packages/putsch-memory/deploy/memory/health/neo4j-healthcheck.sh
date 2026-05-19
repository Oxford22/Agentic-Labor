#!/usr/bin/env bash
# Liveness + readiness probe for the Neo4j node.
#
# Liveness  -> "process is responsive at all"     (lenient)
# Readiness -> "process can serve real queries"  (strict)
#
# Used by:
#   - docker compose healthcheck
#   - the k8s sidecar if/when we move off plain Docker
#   - on-call manual triage (./neo4j-healthcheck.sh readiness)

set -euo pipefail

readonly MODE="${1:-readiness}"
readonly NEO4J_URL="${NEO4J_URL:-bolt://localhost:7687}"
readonly NEO4J_USER="${NEO4J_USER:-neo4j}"
readonly NEO4J_PASSWORD="${NEO4J_PASSWORD:?NEO4J_PASSWORD must be set}"
readonly TIMEOUT="${TIMEOUT:-5}"

case "$MODE" in
  liveness)
    # Cheapest possible round-trip. Detects "process is hung but the port is up".
    exec timeout "$TIMEOUT" cypher-shell \
      -a "$NEO4J_URL" -u "$NEO4J_USER" -p "$NEO4J_PASSWORD" \
      --non-interactive --format plain "RETURN 1 AS ok;" > /dev/null
    ;;

  readiness)
    # 1. Round-trip works.
    # 2. The bitemporal indexes used by every hot-path query exist.
    # 3. Write transactions complete (uses a tombstone-only label so we
    #    never pollute the real graph with healthcheck nodes).
    timeout "$TIMEOUT" cypher-shell \
      -a "$NEO4J_URL" -u "$NEO4J_USER" -p "$NEO4J_PASSWORD" \
      --non-interactive --format plain --fail-fast \
      "CALL db.indexes() YIELD name, state
       WHERE name IN ['ent_valid_window','ent_source_system','ent_idempotency']
         AND state <> 'ONLINE'
       WITH count(*) AS bad
       CALL apoc.util.validate(bad > 0, 'critical index not online', [])
       RETURN 'indexes ok' AS step;
       MERGE (h:_HealthProbe {key:'readiness'})
       SET h.last_seen = datetime()
       WITH h
       MATCH (h2:_HealthProbe {key:'readiness'})
       RETURN h2.last_seen AS last_seen;" > /dev/null
    ;;

  *)
    echo "usage: $0 {liveness|readiness}" >&2
    exit 2
    ;;
esac
