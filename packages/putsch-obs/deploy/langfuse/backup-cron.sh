#!/usr/bin/env bash
# Nightly backup of Postgres + ClickHouse to Hetzner Object Storage.
#
# Scheduled via Hetzner's host-level cron (NOT inside the containers): the
# container layer is treated as ephemeral, the host owns the cron.
#
# /etc/cron.d/putsch-langfuse-backup:
#   30 02 * * *  langfuse  /opt/putsch/deploy/langfuse/backup-cron.sh
#
# Encryption: `age` with a recipient pubkey held in Hetzner Vault. The
# corresponding identity is held offline by the SRE on-call rotation.
# Restoration procedure: docs/runbook.md §clickhouse-restore.

set -euo pipefail
umask 077

TIMESTAMP="$(date -u +%Y%m%dT%H%M%SZ)"
WORK_DIR="$(mktemp -d -t lf-backup.XXXX)"
trap 'rm -rf "$WORK_DIR"' EXIT

cd "$(dirname "$0")"
source ./.env

: "${BACKUP_BUCKET:?must export BACKUP_BUCKET=putsch-langfuse-backups}"
: "${BACKUP_AGE_RECIPIENT:?must export BACKUP_AGE_RECIPIENT=<age recipient pubkey>}"
: "${HETZNER_OBJECT_ENDPOINT:?must export HETZNER_OBJECT_ENDPOINT=https://fsn1.your-objectstorage.com}"
: "${HETZNER_ACCESS_KEY:?}"
: "${HETZNER_SECRET_KEY:?}"

log() { printf '[%s] %s\n' "$(date -u +%FT%TZ)" "$*"; }

log "starting backup ${TIMESTAMP}"

# ── Postgres (Langfuse metadata) ────────────────────────────────────────
log "dumping langfuse postgres ..."
docker compose exec -T postgres \
    pg_dump -U "${POSTGRES_USER}" --format=custom "${POSTGRES_DB}" \
  > "$WORK_DIR/postgres-${TIMESTAMP}.pgdump"

# ── Postgres (Vault) ────────────────────────────────────────────────────
# Vault dumps are signed AND encrypted; restoration requires two operators.
log "dumping vault postgres ..."
docker compose exec -T postgres-vault \
    pg_dump -U "${VAULT_POSTGRES_USER}" --format=custom "${VAULT_POSTGRES_DB}" \
  > "$WORK_DIR/vault-${TIMESTAMP}.pgdump"

# ── ClickHouse ──────────────────────────────────────────────────────────
log "dumping clickhouse ..."
docker compose exec -T clickhouse \
    clickhouse-client --user "${CLICKHOUSE_USER}" --password "${CLICKHOUSE_PASSWORD}" \
    --query "BACKUP DATABASE ${CLICKHOUSE_DB} TO Disk('backups', 'clickhouse-${TIMESTAMP}.zip')"

docker compose cp "clickhouse:/var/lib/clickhouse/backups/clickhouse-${TIMESTAMP}.zip" \
  "$WORK_DIR/clickhouse-${TIMESTAMP}.zip"

# ── Encrypt with age ────────────────────────────────────────────────────
log "encrypting ..."
for f in "$WORK_DIR"/*; do
    age --encrypt --recipient "${BACKUP_AGE_RECIPIENT}" --output "${f}.age" "${f}"
    rm -f "${f}"
done

# ── Upload via mc (or aws s3, whichever is on the host) ─────────────────
log "uploading to ${BACKUP_BUCKET}/${TIMESTAMP} ..."
AWS_ACCESS_KEY_ID="${HETZNER_ACCESS_KEY}" \
AWS_SECRET_ACCESS_KEY="${HETZNER_SECRET_KEY}" \
aws s3 cp --recursive --endpoint-url "${HETZNER_OBJECT_ENDPOINT}" \
    "$WORK_DIR/" "s3://${BACKUP_BUCKET}/${TIMESTAMP}/"

log "backup ${TIMESTAMP} complete"

# Retention is enforced by the bucket lifecycle policy (provisioned via
# Terraform); this script never deletes.
