#!/usr/bin/env bash
# deploy/memory/backup/neo4j-backup.sh
#
# Point-in-time backup driver for the putsch-memory Neo4j node.
# Designed to run inside the neo4j-backup sidecar container; can also be
# invoked manually for a one-shot backup.
#
# RPO target:  15 minutes (BACKUP_INTERVAL_MINUTES, default 15)
# RTO target:  30 minutes (see docs/runbook.md "Backup restore drill")
# Retention:   BACKUP_RETENTION_DAYS days (default 7)
#
# Failure of a single run is non-fatal; we log and continue, but two
# consecutive failures page on-call (PromQL: increase(neo4j_backup_failures[30m]) >= 2).

set -euo pipefail
shopt -s lastpipe

readonly NEO4J_HOST="${NEO4J_HOST:-neo4j}"
readonly NEO4J_BACKUP_PORT="${NEO4J_BACKUP_PORT:-6362}"
readonly NEO4J_DATABASE="${NEO4J_DATABASE:-neo4j}"
readonly NEO4J_PASSWORD="${NEO4J_PASSWORD:?NEO4J_PASSWORD must be set}"
readonly BACKUP_INTERVAL_MINUTES="${BACKUP_INTERVAL_MINUTES:-15}"
readonly BACKUP_RETENTION_DAYS="${BACKUP_RETENTION_DAYS:-7}"
readonly BACKUP_DIR="${BACKUP_DIR:-/backups}"
readonly BACKUP_S3_BUCKET="${BACKUP_S3_BUCKET:-}"             # optional Frankfurt S3
readonly METRICS_FILE="${METRICS_FILE:-/backups/.metrics.prom}"

log() {
  # Structured-ish log for the host journal. JSON to satisfy the central log shipper.
  local level="$1"; shift
  printf '{"ts":"%s","level":"%s","component":"neo4j-backup","msg":"%s"}\n' \
    "$(date -Iseconds)" "$level" "$*"
}

emit_metric() {
  # Atomic write to a node-exporter textfile-collector target.
  local name="$1" value="$2"
  printf '# TYPE %s counter\n%s %s\n' "$name" "$name" "$value" >> "${METRICS_FILE}.tmp"
}

flush_metrics() {
  mv -f "${METRICS_FILE}.tmp" "${METRICS_FILE}"
}

run_backup() {
  local ts
  ts="$(date -u +%Y%m%dT%H%M%SZ)"
  local target_dir="${BACKUP_DIR}/${ts}"
  mkdir -p "$target_dir"

  log INFO "starting backup ${ts} -> ${target_dir}"

  : > "${METRICS_FILE}.tmp"

  # `neo4j-admin database backup` is the Enterprise online backup tool.
  # --check-consistency=true is the default but we make it explicit; it
  # adds ~30% to the runtime but catches storage-corruption-on-write bugs
  # we would rather discover during backup than during restore.
  if /var/lib/neo4j/bin/neo4j-admin database backup \
        --from-uri="neo4j://${NEO4J_HOST}:${NEO4J_BACKUP_PORT}" \
        --to-path="$target_dir" \
        --include-metadata=all \
        --compress=true \
        --check-consistency=true \
        --parallel-recovery=true \
        --verbose \
        "$NEO4J_DATABASE" 2>&1 | tee -a "${target_dir}/backup.log"; then
    emit_metric "neo4j_backup_success_total" "$(($(get_counter neo4j_backup_success_total) + 1))"
    log INFO "backup succeeded ${ts}"
  else
    emit_metric "neo4j_backup_failures_total" "$(($(get_counter neo4j_backup_failures_total) + 1))"
    log ERROR "backup FAILED ${ts}"
    flush_metrics
    return 1
  fi

  # Optional offsite ship — encrypted with KMS, Frankfurt region only.
  if [[ -n "$BACKUP_S3_BUCKET" ]]; then
    if aws s3 sync "$target_dir" "s3://${BACKUP_S3_BUCKET}/${ts}/" \
        --sse aws:kms --only-show-errors; then
      log INFO "offsite ship succeeded -> s3://${BACKUP_S3_BUCKET}/${ts}/"
    else
      log ERROR "offsite ship FAILED — local copy retained"
    fi
  fi

  flush_metrics
}

prune_old() {
  log INFO "pruning backups older than ${BACKUP_RETENTION_DAYS} day(s)"
  find "$BACKUP_DIR" -maxdepth 1 -mindepth 1 -type d \
    -mtime "+${BACKUP_RETENTION_DAYS}" \
    -exec rm -rf {} \; \
    -print | while read -r removed; do
      log INFO "pruned ${removed}"
    done
}

get_counter() {
  # Read the last counter value (or 0) from the metrics file.
  local name="$1"
  if [[ -f "$METRICS_FILE" ]]; then
    awk -v n="$name" '$1==n {print $2}' "$METRICS_FILE" | tail -n1 || echo 0
  fi | grep -E '^[0-9]+$' || echo 0
}

main() {
  log INFO "neo4j-backup starting; interval=${BACKUP_INTERVAL_MINUTES}m retention=${BACKUP_RETENTION_DAYS}d dir=${BACKUP_DIR}"
  while true; do
    if ! run_backup; then
      log WARN "continuing after single backup failure; two consecutive failures pages on-call"
    fi
    prune_old
    sleep "$((BACKUP_INTERVAL_MINUTES * 60))"
  done
}

main "$@"
