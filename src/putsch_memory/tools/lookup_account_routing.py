"""Account routing lookup — who owns which Konto / customer / vendor right now.

This is a *personnel-sensitive* tool. Every call routes through the
personnel namespace and is audit-logged. Callers must claim a role; the
result is bounded to the smallest piece of information that solves the
routing task — the current owner ID — plus the prior owner only when
explicitly requested with a justification.

Mahnverfahren and AP both call this to decide who to CC; without it
they would either guess or hard-code which fails the moment someone
rotates teams.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import TYPE_CHECKING, Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from putsch_memory.gdpr import audit_personnel_read, ensure_personnel_role
from putsch_memory.logging import get_logger

if TYPE_CHECKING:
    from putsch_memory.graphiti_client import MemoryClient

logger = get_logger(__name__)


class AccountRoutingInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    subject_type: Literal["kunde", "lieferant", "konto"]
    subject_id: str = Field(min_length=1, max_length=128)
    as_of: datetime | None = None
    include_prior_owner: bool = False
    purpose: str = Field(
        min_length=8,
        max_length=512,
        description="Required justification — the Betriebsrat reviews these. "
        "'CC on dunning letter' or 'route invoice to accounts team' is fine; "
        "'just checking' is not.",
    )
    caller_role: str = Field(min_length=4)
    caller_id: str = Field(min_length=2)


class AccountRoutingResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    subject_id: str
    current_owner: dict[str, Any] | None = None
    prior_owner: dict[str, Any] | None = None
    as_of: datetime


async def lookup_account_routing(
    client: MemoryClient,
    *,
    subject_type: Literal["kunde", "lieferant", "konto"],
    subject_id: str,
    as_of: datetime | None = None,
    include_prior_owner: bool = False,
    purpose: str,
    caller_role: str,
    caller_id: str,
) -> AccountRoutingResult:
    inp = AccountRoutingInput(
        subject_type=subject_type,
        subject_id=subject_id,
        as_of=as_of,
        include_prior_owner=include_prior_owner,
        purpose=purpose,
        caller_role=caller_role,
        caller_id=caller_id,
    )
    resolved = inp.as_of or datetime.now(tz=timezone.utc)

    ensure_personnel_role(inp.caller_role, client._settings)  # noqa: SLF001

    # Find the responsible Mitarbeiter at `resolved` via the
    # RESPONSIBLE_FOR edge. The edge itself has a validity window.
    current_rows = await client._run_cypher(  # noqa: SLF001
        """
        MATCH (subject {id: $sid})<-[r:RESPONSIBLE_FOR]-(m:Mitarbeiter)
        WHERE r.business_time_from <= datetime($t)
          AND (r.business_time_to IS NULL OR r.business_time_to > datetime($t))
        RETURN m.id AS personnel_id,
               m.display_name AS display_name,
               m.role AS role,
               m.site AS site,
               r.business_time_from AS owned_since
        LIMIT 1
        """,
        {"sid": subject_id, "t": resolved.isoformat()},
        database=client._settings.personnel_neo4j_database,  # noqa: SLF001
    )
    current = current_rows[0] if current_rows else None

    if current is not None:
        await audit_personnel_read(
            client,
            personnel_id=current["personnel_id"],
            caller_id=inp.caller_id,
            caller_role=inp.caller_role,
            purpose=inp.purpose,
            business_time=resolved,
        )

    prior: dict[str, Any] | None = None
    if inp.include_prior_owner and current is not None:
        prior_rows = await client._run_cypher(  # noqa: SLF001
            """
            MATCH (subject {id: $sid})<-[r:RESPONSIBLE_FOR]-(m:Mitarbeiter)
            WHERE r.business_time_to IS NOT NULL
              AND r.business_time_to <= datetime($t)
            RETURN m.id AS personnel_id,
                   m.display_name AS display_name,
                   m.role AS role,
                   m.site AS site,
                   r.business_time_from AS owned_since,
                   r.business_time_to AS owned_until
            ORDER BY r.business_time_to DESC
            LIMIT 1
            """,
            {"sid": subject_id, "t": resolved.isoformat()},
            database=client._settings.personnel_neo4j_database,  # noqa: SLF001
        )
        prior = prior_rows[0] if prior_rows else None
        if prior is not None:
            await audit_personnel_read(
                client,
                personnel_id=prior["personnel_id"],
                caller_id=inp.caller_id,
                caller_role=inp.caller_role,
                purpose=f"{inp.purpose} [prior_owner]",
                business_time=resolved,
            )

    logger.info(
        "account_routing_resolved",
        subject_id=subject_id,
        owner=current["personnel_id"] if current else None,
        included_prior=prior is not None,
    )

    return AccountRoutingResult(
        subject_id=subject_id,
        current_owner=current,
        prior_owner=prior,
        as_of=resolved,
    )
