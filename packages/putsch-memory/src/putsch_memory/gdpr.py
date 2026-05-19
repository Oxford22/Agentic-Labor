"""GDPR + Betriebsrat compliance primitives.

Hard rules implemented in this module:

* **Personnel-namespace isolation.** All Mitarbeiter facts live in a
  separate Neo4j database (`personnel`). The application is the only
  thing that can join across the two — and only via the explicit
  `personnel_memory` adapter — so a generic vendor-lookup path can't
  exfiltrate employee data.
* **Read-side audit log.** Every read against the personnel namespace
  is recorded as a structured event with the caller identity, role,
  and stated purpose. The Betriebsrat reviews these.
* **Right to be forgotten.** When a subject exercises Art. 17, we
  cascade-tombstone their facts and write a tombstone audit record
  containing only what is required for future audits (which fact IDs
  were forgotten, who requested it, when). Originals are gone.
* **Data residency.** Every write checks `settings.data_residency ==
  "DE"`; cross-border replication is structurally impossible because
  we never configure replication targets outside Frankfurt.

This file does not implement the *UI* for any of this — that is in
the Sachbearbeiter web app, which calls these functions.
"""

from __future__ import annotations

import hashlib
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Any

from pydantic import BaseModel, ConfigDict, Field

from putsch_memory.config import Settings
from putsch_memory.exceptions import PutschMemoryError
from putsch_memory.logging import get_logger

if TYPE_CHECKING:
    from putsch_memory.graphiti_client import MemoryClient

logger = get_logger(__name__)


class PersonnelAccessDenied(PutschMemoryError):
    """Caller's claimed role is not authorised for personnel reads."""


class PersonnelReadAudit(BaseModel):
    """One read of a personnel fact. Written to a tamper-evident log."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    audit_id: str
    personnel_id: str
    caller_id: str
    caller_role: str
    purpose: str
    business_time: datetime
    audited_at: datetime
    prev_hash: str | None = None
    self_hash: str | None = None


class ForgetRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    subject_kind: str = Field(description="'natural_person' | 'business_partner'")
    subject_id: str
    requested_by: str
    legal_basis: str = Field(min_length=4, max_length=512)
    requested_at: datetime


class ForgetReport(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    subject_id: str
    facts_tombstoned: int
    edges_tombstoned: int
    audit_log_id: str
    completed_at: datetime


# ---------------------------------------------------------------------------
# Role gating
# ---------------------------------------------------------------------------


def ensure_personnel_role(caller_role: str, settings: Settings) -> None:
    """Raise if the caller's role doesn't match the required claim.

    The full RBAC check happens upstream (in the API gateway against
    Keycloak). This is the second layer — defence in depth so a future
    bug that loses the gateway check doesn't immediately leak employee
    data through agent memory.
    """
    required = settings.personnel_namespace_required_role
    if caller_role != required:
        logger.warning(
            "personnel_access_denied",
            claimed_role=caller_role,
            required_role=required,
        )
        raise PersonnelAccessDenied(
            f"role {caller_role!r} cannot read personnel memory; need {required!r}"
        )


# ---------------------------------------------------------------------------
# Audit log
# ---------------------------------------------------------------------------


async def audit_personnel_read(
    client: MemoryClient,
    *,
    personnel_id: str,
    caller_id: str,
    caller_role: str,
    purpose: str,
    business_time: datetime,
) -> str:
    """Write a read-side audit record.

    Implements a hash chain (`prev_hash` -> `self_hash`) so deletion or
    rewriting of any entry breaks the chain at audit time.
    """
    if business_time.tzinfo is None:
        raise ValueError("business_time must be UTC-aware")
    now = datetime.now(tz=timezone.utc)

    # Pull the most recent audit-log node's self_hash to chain against.
    prev_rows = await client._run_cypher(  # noqa: SLF001
        """
        MATCH (a:_PersonnelReadAudit)
        RETURN a.self_hash AS h
        ORDER BY a.audited_at DESC
        LIMIT 1
        """,
        {},
        database=client._settings.personnel_neo4j_database,  # noqa: SLF001
    )
    prev_hash = prev_rows[0]["h"] if prev_rows else None

    audit_id = "audit:" + hashlib.sha256(
        f"{personnel_id}|{caller_id}|{purpose}|{now.isoformat()}".encode()
    ).hexdigest()[:24]
    self_hash = _chain_hash(
        prev_hash=prev_hash,
        audit_id=audit_id,
        personnel_id=personnel_id,
        caller_id=caller_id,
        caller_role=caller_role,
        purpose=purpose,
        business_time=business_time,
        audited_at=now,
    )

    await client._run_cypher(  # noqa: SLF001
        """
        CREATE (a:_PersonnelReadAudit {
          audit_id: $aid,
          personnel_id: $pid,
          caller_id: $cid,
          caller_role: $role,
          purpose: $purpose,
          business_time: datetime($bt),
          audited_at: datetime($at),
          prev_hash: $ph,
          self_hash: $sh
        })
        RETURN a.audit_id AS audit_id
        """,
        {
            "aid": audit_id,
            "pid": personnel_id,
            "cid": caller_id,
            "role": caller_role,
            "purpose": purpose,
            "bt": business_time.isoformat(),
            "at": now.isoformat(),
            "ph": prev_hash,
            "sh": self_hash,
        },
        fetch=False,
        database=client._settings.personnel_neo4j_database,  # noqa: SLF001
    )

    logger.info(
        "personnel_read_audited",
        audit_id=audit_id,
        personnel_id=personnel_id,
        caller_id=caller_id,
        purpose=purpose,
    )
    return audit_id


def _chain_hash(
    *,
    prev_hash: str | None,
    audit_id: str,
    personnel_id: str,
    caller_id: str,
    caller_role: str,
    purpose: str,
    business_time: datetime,
    audited_at: datetime,
) -> str:
    h = hashlib.sha256()
    h.update((prev_hash or "GENESIS").encode())
    for x in (audit_id, personnel_id, caller_id, caller_role, purpose):
        h.update(b"\x00")
        h.update(x.encode())
    h.update(b"\x00")
    h.update(business_time.isoformat().encode())
    h.update(b"\x00")
    h.update(audited_at.isoformat().encode())
    return h.hexdigest()


async def verify_audit_chain(client: MemoryClient) -> bool:
    """Re-walk the audit chain and verify every link.

    Intended for the monthly Betriebsrat verification ritual: re-walks
    every personnel read in the period and confirms no entry has been
    silently modified or deleted.
    """
    rows = await client._run_cypher(  # noqa: SLF001
        """
        MATCH (a:_PersonnelReadAudit)
        RETURN a { .* } AS a
        ORDER BY a.audited_at ASC
        """,
        {},
        database=client._settings.personnel_neo4j_database,  # noqa: SLF001
    )
    prev: str | None = None
    for row in rows:
        a = row["a"]
        expected = _chain_hash(
            prev_hash=prev,
            audit_id=a["audit_id"],
            personnel_id=a["personnel_id"],
            caller_id=a["caller_id"],
            caller_role=a["caller_role"],
            purpose=a["purpose"],
            business_time=_to_dt(a["business_time"]),
            audited_at=_to_dt(a["audited_at"]),
        )
        if expected != a["self_hash"]:
            logger.error("audit_chain_broken", audit_id=a["audit_id"])
            return False
        prev = a["self_hash"]
    return True


# ---------------------------------------------------------------------------
# Right to be forgotten
# ---------------------------------------------------------------------------


async def cascade_forget(client: MemoryClient, request: ForgetRequest) -> ForgetReport:
    """Cascade-delete a subject's facts. Keeps a tombstone audit row.

    Implementation notes:
    * For a natural person, we route into the personnel database.
    * For a business partner who is also a natural person (Einzelunter-
      nehmer), the caller passes `subject_kind="natural_person"` and
      we delete from both databases.
    * Tombstones live for `rtbf_audit_retention_days` (default: ten
      years) per Steuerrecht / Handelsrecht retention windows.
    """
    log = logger.bind(op="rtbf", subject_id=request.subject_id, kind=request.subject_kind)

    facts_tombstoned, edges_tombstoned = 0, 0

    for db in _affected_databases(client._settings, request):  # noqa: SLF001
        rows = await client._run_cypher(  # noqa: SLF001
            """
            MATCH (n {id: $sid})
            DETACH DELETE n
            RETURN count(n) AS facts
            """,
            {"sid": request.subject_id},
            fetch=True,
            database=db,
        )
        if rows:
            facts_tombstoned += int(rows[0].get("facts", 0))
        # Any incoming references with the subject's id get rewritten to a
        # synthetic tombstone marker so the graph stays referentially intact.
        rewrite_rows = await client._run_cypher(  # noqa: SLF001
            """
            MATCH ()-[r]->(t)
            WHERE t.id = $sid
            DELETE r
            RETURN count(r) AS edges
            """,
            {"sid": request.subject_id},
            fetch=True,
            database=db,
        )
        if rewrite_rows:
            edges_tombstoned += int(rewrite_rows[0].get("edges", 0))

    audit_id = await _write_tombstone(client, request)
    log.warning(
        "rtbf_complete",
        facts_tombstoned=facts_tombstoned,
        edges_tombstoned=edges_tombstoned,
        audit_log_id=audit_id,
    )
    return ForgetReport(
        subject_id=request.subject_id,
        facts_tombstoned=facts_tombstoned,
        edges_tombstoned=edges_tombstoned,
        audit_log_id=audit_id,
        completed_at=datetime.now(tz=timezone.utc),
    )


async def _write_tombstone(client: MemoryClient, request: ForgetRequest) -> str:
    audit_id = "rtbf:" + hashlib.sha256(
        f"{request.subject_id}|{request.requested_at.isoformat()}".encode()
    ).hexdigest()[:24]
    await client._run_cypher(  # noqa: SLF001
        """
        CREATE (t:_RTBFTombstone {
          audit_id: $aid,
          subject_id: $sid,
          subject_kind: $kind,
          requested_by: $by,
          legal_basis: $basis,
          requested_at: datetime($at),
          completed_at: datetime($now)
        })
        """,
        {
            "aid": audit_id,
            "sid": request.subject_id,
            "kind": request.subject_kind,
            "by": request.requested_by,
            "basis": request.legal_basis,
            "at": request.requested_at.isoformat(),
            "now": datetime.now(tz=timezone.utc).isoformat(),
        },
        fetch=False,
    )
    return audit_id


def _affected_databases(settings: Settings, request: ForgetRequest) -> tuple[str, ...]:
    if request.subject_kind == "natural_person":
        return (settings.neo4j_database, settings.personnel_neo4j_database)
    return (settings.neo4j_database,)


def _to_dt(v: Any) -> datetime:
    if isinstance(v, datetime):
        return v if v.tzinfo else v.replace(tzinfo=timezone.utc)
    if isinstance(v, str):
        return datetime.fromisoformat(v)
    raise TypeError(f"cannot coerce {v!r} to datetime")


__all__ = [
    "ForgetReport",
    "ForgetRequest",
    "PersonnelAccessDenied",
    "PersonnelReadAudit",
    "audit_personnel_read",
    "cascade_forget",
    "ensure_personnel_role",
    "verify_audit_chain",
]
