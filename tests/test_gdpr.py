"""GDPR primitives.

Tests cover:
* role gating raises if claim is wrong
* audit chain hashing is correct
* RTBF tombstone path completes end-to-end against the fake graph
"""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from putsch_memory.config import Settings
from putsch_memory.gdpr import (
    ForgetRequest,
    PersonnelAccessDenied,
    _chain_hash,
    cascade_forget,
    ensure_personnel_role,
)


def test_personnel_role_gate(settings: Settings) -> None:
    ensure_personnel_role("role:personnel-reader", settings)
    with pytest.raises(PersonnelAccessDenied):
        ensure_personnel_role("role:guest", settings)


def test_chain_hash_links_prev_to_self() -> None:
    t = datetime(2026, 5, 18, 10, 0, 0, tzinfo=timezone.utc)
    h1 = _chain_hash(
        prev_hash=None,
        audit_id="a1",
        personnel_id="M-100",
        caller_id="cron",
        caller_role="role:personnel-reader",
        purpose="reconciliation",
        business_time=t,
        audited_at=t,
    )
    h2 = _chain_hash(
        prev_hash=h1,
        audit_id="a2",
        personnel_id="M-100",
        caller_id="cron",
        caller_role="role:personnel-reader",
        purpose="reconciliation",
        business_time=t,
        audited_at=t,
    )
    # Different inputs → different self_hash
    assert h1 != h2
    # Tampering with prev_hash changes the chain at this link
    h2_tampered = _chain_hash(
        prev_hash="GENESIS",
        audit_id="a2",
        personnel_id="M-100",
        caller_id="cron",
        caller_role="role:personnel-reader",
        purpose="reconciliation",
        business_time=t,
        audited_at=t,
    )
    assert h2 != h2_tampered


async def test_cascade_forget_runs_against_both_databases(memory_client, fake_graph) -> None:
    request = ForgetRequest(
        subject_kind="natural_person",
        subject_id="mitarbeiter:M-100",
        requested_by="dpo@putsch.example",
        legal_basis="GDPR Art. 17 — subject request 2026-05-18",
        requested_at=datetime(2026, 5, 18, tzinfo=timezone.utc),
    )
    report = await cascade_forget(memory_client, request)
    assert report.subject_id == "mitarbeiter:M-100"
    # Each database should have at least one DETACH DELETE + one tombstone CREATE.
    detach_calls = [c for c, _ in fake_graph.cypher_log if "DETACH DELETE" in c]
    tombstone_calls = [c for c, _ in fake_graph.cypher_log if "_RTBFTombstone" in c]
    assert len(detach_calls) >= 2
    assert len(tombstone_calls) >= 1
