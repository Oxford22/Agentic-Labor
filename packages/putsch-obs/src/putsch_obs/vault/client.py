"""Postgres-backed reversible-tokenization vault.

Schema (see ``migrations/001_init.sql``):

  * ``putsch_vault.tokens``     — token → (category, ciphertext) mapping
  * ``putsch_vault.audit_log``  — append-only, hash-chained, WORM-enforced
    via a Postgres trigger that rejects UPDATE/DELETE.

Encryption: Fernet (AES-128-CBC + HMAC-SHA256), key from
``PUTSCH_OBS_VAULT_ENCRYPTION_KEY``. Key rotation is a dual-key migration
(decrypt with old, encrypt with new in a new table, swap). Never rotate in
place — there is no atomic swap and a crash would leave half-rotated rows.
"""

from __future__ import annotations

import contextlib
from collections.abc import Iterator
from typing import TYPE_CHECKING

from cryptography.fernet import Fernet, InvalidToken

from putsch_obs.config import PutschObsSettings, get_settings
from putsch_obs.exceptions import VaultError
from putsch_obs.logging import get_logger
from putsch_obs.redaction import PIICategory, VaultProtocol
from putsch_obs.vault.audit import AuditEvent, AuditLogger

if TYPE_CHECKING:
    import psycopg

log = get_logger(__name__)


class PostgresVault(VaultProtocol):
    """Synchronous + async vault backed by Postgres.

    The connection pool is built lazily on first use; this keeps imports
    cheap and lets test fixtures construct an instance without a live DB.
    """

    def __init__(
        self,
        settings: PutschObsSettings | None = None,
        *,
        audit: AuditLogger | None = None,
    ) -> None:
        self._settings = settings or get_settings()
        key = self._settings.vault_encryption_key.get_secret_value()
        if not key:
            raise VaultError(
                "PUTSCH_OBS_VAULT_ENCRYPTION_KEY is empty — vault refuses to start"
            )
        try:
            self._fernet = Fernet(key.encode("ascii"))
        except (ValueError, TypeError) as exc:
            raise VaultError(f"vault encryption key is not a valid Fernet key: {exc}") from exc
        self._audit = audit or AuditLogger(settings=self._settings)
        self._dsn = str(self._settings.vault_dsn)

    # ── connections ──────────────────────────────────────────────────────

    @contextlib.contextmanager
    def _conn(self) -> Iterator[psycopg.Connection]:
        import psycopg  # local import: optional dep when running w/o vault

        with psycopg.connect(self._dsn, autocommit=False) as conn:
            yield conn

    # ── VaultProtocol impl ───────────────────────────────────────────────

    def store(
        self,
        token: str,
        category: PIICategory,
        original: str,
        *,
        context_hint: str | None = None,
    ) -> None:
        ct = self._fernet.encrypt(original.encode("utf-8"))
        try:
            with self._conn() as conn, conn.cursor() as cur:
                cur.execute(
                    """
                    INSERT INTO putsch_vault.tokens (token, category, ciphertext, context_hint)
                    VALUES (%s, %s, %s, %s)
                    ON CONFLICT (token) DO NOTHING
                    """,
                    (token, category.value, ct, context_hint),
                )
                conn.commit()
        except Exception as exc:
            # On the trace hot path: warn and continue. The token is already
            # in the exported payload, but the audit chain will simply lack
            # the mapping. An auditor seeing an orphan token can flag it.
            log.warning(
                "vault.store_failed",
                token=token,
                category=category.value,
                err=str(exc),
                err_type=type(exc).__name__,
            )

    async def store_async(
        self,
        token: str,
        category: PIICategory,
        original: str,
        *,
        context_hint: str | None = None,
    ) -> None:
        import anyio

        await anyio.to_thread.run_sync(
            self.store, token, category, original
        )

    # ── un-redact (audited) ──────────────────────────────────────────────

    def unredact(
        self,
        token: str,
        *,
        actor: str,
        reason: str,
        ticket: str | None = None,
    ) -> str:
        """Resolve a token back to its original. Audited unconditionally."""
        if not actor or not reason:
            raise VaultError("un-redaction requires both actor and reason")
        with self._conn() as conn, conn.cursor() as cur:
            cur.execute(
                "SELECT category, ciphertext FROM putsch_vault.tokens WHERE token = %s",
                (token,),
            )
            row = cur.fetchone()
            if row is None:
                self._audit.write(
                    conn,
                    AuditEvent(
                        actor=actor,
                        reason=reason,
                        ticket=ticket,
                        token=token,
                        category="unknown",
                        outcome="not_found",
                    ),
                )
                conn.commit()
                raise VaultError(f"token {token!r} not in vault")
            category, ciphertext = row
            try:
                original = self._fernet.decrypt(bytes(ciphertext)).decode("utf-8")
            except InvalidToken as exc:
                self._audit.write(
                    conn,
                    AuditEvent(
                        actor=actor,
                        reason=reason,
                        ticket=ticket,
                        token=token,
                        category=category,
                        outcome="decrypt_failed",
                    ),
                )
                conn.commit()
                raise VaultError("vault ciphertext failed integrity check") from exc
            self._audit.write(
                conn,
                AuditEvent(
                    actor=actor,
                    reason=reason,
                    ticket=ticket,
                    token=token,
                    category=category,
                    outcome="ok",
                ),
            )
            conn.commit()
            return original


__all__ = ["PostgresVault"]
