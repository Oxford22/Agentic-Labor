"""Append-only, hash-chained audit log.

Every row's ``row_hash`` is ``sha256(prev_row_hash || row_payload)``. The
database trigger ``audit_no_update_delete`` rejects any UPDATE or DELETE on
the table, so a compromised process cannot rewrite history without DROP
privileges — and DROP is gated by a separate DB role.

The chain is verified offline by the runbook's chain-verify script (see
``docs/runbook.md``).
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Literal

from putsch_obs.config import PutschObsSettings, get_settings

if TYPE_CHECKING:
    import psycopg


AuditOutcome = Literal["ok", "not_found", "decrypt_failed", "denied"]


@dataclass(slots=True, frozen=True)
class AuditEvent:
    """A single un-redaction event."""

    actor: str           # email or service-account principal
    reason: str          # human-readable, indexed in the dashboard
    token: str
    category: str
    outcome: AuditOutcome
    ticket: str | None = None  # cross-reference to a Putsch JIRA ticket


def _canonical_json(payload: dict[str, object]) -> bytes:
    """Sort keys + no whitespace, so the hash is reproducible."""
    return json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")


class AuditLogger:
    """Writes hash-chained audit rows. Uses an existing transaction so the
    audit row commits atomically with the vault lookup it audits.
    """

    def __init__(self, settings: PutschObsSettings | None = None) -> None:
        self._settings = settings or get_settings()

    def write(self, conn: "psycopg.Connection", event: AuditEvent) -> None:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT row_hash FROM putsch_vault.audit_log "
                "ORDER BY id DESC LIMIT 1 FOR UPDATE"
            )
            prev = cur.fetchone()
            prev_hash: str = prev[0] if prev else "0" * 64

            now = datetime.now(timezone.utc).isoformat()
            payload = {
                "actor": event.actor,
                "reason": event.reason,
                "token": event.token,
                "category": event.category,
                "outcome": event.outcome,
                "ticket": event.ticket,
                "ts": now,
                "service": self._settings.service_name,
                "env": self._settings.deployment_environment,
            }
            row_payload = _canonical_json(payload)
            row_hash = hashlib.sha256(
                prev_hash.encode("ascii") + row_payload
            ).hexdigest()

            cur.execute(
                """
                INSERT INTO putsch_vault.audit_log
                    (occurred_at, actor, reason, ticket, token, category,
                     outcome, prev_hash, row_hash, payload)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s::jsonb)
                """,
                (
                    now,
                    event.actor,
                    event.reason,
                    event.ticket,
                    event.token,
                    event.category,
                    event.outcome,
                    prev_hash,
                    row_hash,
                    row_payload.decode("utf-8"),
                ),
            )


def verify_chain(rows: list[dict[str, object]]) -> bool:
    """Re-compute each row's hash and compare. Used by the chain-verify CLI.

    Pass rows in insertion order. Returns ``True`` iff the chain is intact.
    """
    prev_hash = "0" * 64
    for row in rows:
        payload_bytes = _canonical_json(  # type: ignore[arg-type]
            row["payload"] if isinstance(row.get("payload"), dict) else json.loads(str(row["payload"]))
        )
        expected = hashlib.sha256(prev_hash.encode("ascii") + payload_bytes).hexdigest()
        if expected != row["row_hash"]:
            return False
        prev_hash = str(row["row_hash"])
    return True


__all__ = ["AuditEvent", "AuditLogger", "AuditOutcome", "verify_chain"]


_ = asdict  # keep dataclasses.asdict importable for the CLI
