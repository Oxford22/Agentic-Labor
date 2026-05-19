"""Reversible-tokenization vault.

The vault is a separate Postgres database that holds the (token → original)
mapping for every PII value the redactor produced. Two reasons it is
separate:

* Blast radius. A Langfuse compromise must not yield un-redacted PII.
* Compliance. Access to the vault is logged in a WORM-style audit table;
  Langfuse access is not, by design.

Public surface:

    from putsch_obs.vault import PostgresVault, AuditLogger
"""

from __future__ import annotations

from putsch_obs.vault.audit import AuditEvent, AuditLogger
from putsch_obs.vault.client import PostgresVault

__all__ = ["AuditEvent", "AuditLogger", "PostgresVault"]
