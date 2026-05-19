#!/usr/bin/env bash
# Block until the Langfuse self-host is fully healthy.
#
# Usage: ./healthcheck.sh [timeout_seconds]   (default 300)
#
# Returns 0 on success, 1 on timeout. The CI deploy step calls this
# immediately after `docker compose up -d` to gate downstream tasks
# (dashboards apply, dataset sync) on the stack being live.

set -euo pipefail

TIMEOUT="${1:-300}"
LANGFUSE_URL="${LANGFUSE_URL:-http://localhost:3000}"
DEADLINE=$(( $(date +%s) + TIMEOUT ))

echo "Waiting up to ${TIMEOUT}s for Langfuse at ${LANGFUSE_URL} ..."

check() {
  local url="$1"
  curl --silent --show-error --fail --max-time 5 "$url" >/dev/null 2>&1
}

while (( $(date +%s) < DEADLINE )); do
  if check "${LANGFUSE_URL}/api/public/health" \
      && docker compose ps --status running --quiet \
           langfuse-web langfuse-worker clickhouse postgres redis minio otel-collector \
           | wc -l | grep -qE '^[ ]*7$'; then
    echo "Langfuse stack is healthy."
    exit 0
  fi
  sleep 5
done

echo "Healthcheck timed out after ${TIMEOUT}s." >&2
docker compose ps >&2 || true
exit 1
