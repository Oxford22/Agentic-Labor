"""Conflict-detection contract tests."""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from putsch_memory.conflicts import _canonical, _conflict_id, resolve_conflict


def test_conflict_id_is_symmetric_in_source_pair() -> None:
    # Order of source systems shouldn't matter — we want one canonical id.
    a = _conflict_id("lieferant:DE111", "payment_terms_days", "sap:hagen", "sap:poggibonsi")
    b = _conflict_id("lieferant:DE111", "payment_terms_days", "sap:poggibonsi", "sap:hagen")
    assert a == b


def test_conflict_id_changes_with_attribute() -> None:
    a = _conflict_id("lieferant:DE111", "payment_terms_days", "sap:hagen", "sap:poggibonsi")
    b = _conflict_id("lieferant:DE111", "bank_iban", "sap:hagen", "sap:poggibonsi")
    assert a != b


def test_canonical_normalises_whitespace_and_case() -> None:
    assert _canonical("  Müller GmbH  ") == _canonical("müller gmbh")
    assert _canonical("Müller GmbH") != _canonical("Müller AG")


async def test_resolve_conflict_requires_substantive_justification(memory_client) -> None:
    with pytest.raises(ValueError):
        await resolve_conflict(
            memory_client,
            conflict_id="conflict:abc",
            winning_source="sap:hagen",
            winning_value=30,
            resolved_by="M-100",
            justification="x",
        )


async def test_resolve_conflict_writes_audit_fields(memory_client, fake_graph) -> None:
    await resolve_conflict(
        memory_client,
        conflict_id="conflict:abc",
        winning_source="sap:hagen",
        winning_value=30,
        resolved_by="M-100",
        justification="Hagen is source of truth for payment terms as of Q1 reorg.",
    )
    update_calls = [c for c, _ in fake_graph.cypher_log if "SET c.status = 'resolved'" in c]
    assert update_calls
