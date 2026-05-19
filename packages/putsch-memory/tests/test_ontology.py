"""Ontology contract tests — these are deliberately strict.

Breaking any of these tests means the constitution of the memory layer
changed; the failing test is doing its job. Do not relax these; either
revert the ontology change or update the test together with an ADR
amendment.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest
from pydantic import ValidationError

from putsch_memory.ontology import (
    BUSINESS_GRAPH,
    Lieferant,
    Provenance,
    SourceSystem,
    ValidityWindow,
    make_idempotency_key,
    now_utc,
    open_window,
)


# ---------------------------------------------------------------------------
# ValidityWindow
# ---------------------------------------------------------------------------


def test_validity_window_requires_utc() -> None:
    naive = datetime(2026, 1, 1, 0, 0, 0)
    with pytest.raises(ValidationError):
        ValidityWindow(
            business_time_from=naive,  # type: ignore[arg-type]
            system_time_from=now_utc(),
        )


def test_validity_window_rejects_inverted_times() -> None:
    t0 = now_utc()
    with pytest.raises(ValidationError):
        ValidityWindow(
            business_time_from=t0,
            business_time_to=t0 - timedelta(days=1),
            system_time_from=t0,
        )


def test_validity_window_is_active_at() -> None:
    t0 = now_utc()
    w = ValidityWindow(
        business_time_from=t0,
        business_time_to=t0 + timedelta(days=10),
        system_time_from=t0,
    )
    assert w.is_active_at(t0 + timedelta(days=5))
    assert not w.is_active_at(t0 - timedelta(days=1))
    assert not w.is_active_at(t0 + timedelta(days=20))


def test_validity_window_is_open() -> None:
    w = open_window()
    assert w.is_open()


# ---------------------------------------------------------------------------
# Provenance — required on every fact
# ---------------------------------------------------------------------------


def test_provenance_requires_all_four_fields() -> None:
    with pytest.raises(ValidationError):
        Provenance(  # type: ignore[call-arg]
            source_system=SourceSystem.SAP_HAGEN,
            source_id="L-4711",
            written_by_agent="sap_sync",
            # missing written_at_trace_id
        )


def test_provenance_confidence_bounds() -> None:
    with pytest.raises(ValidationError):
        Provenance(
            source_system=SourceSystem.AGENT_AP,
            source_id="x",
            written_by_agent="agent",
            written_at_trace_id="t",
            confidence=1.5,
        )


# ---------------------------------------------------------------------------
# Lieferant — deterministic identity
# ---------------------------------------------------------------------------


def _prov() -> Provenance:
    return Provenance(
        source_system=SourceSystem.SAP_HAGEN,
        source_id="L-4711",
        written_by_agent="test",
        written_at_trace_id="trace-001",
    )


def test_lieferant_id_must_match_deterministic_key() -> None:
    with pytest.raises(ValidationError):
        Lieferant(
            id="lieferant:WRONG",
            name="Beispiel",
            ust_id_nr="DE123456789",
            validity=open_window(),
            provenance=_prov(),
        )


def test_lieferant_id_is_derived_from_ust_id_nr() -> None:
    expected = Lieferant.make_id(ust_id_nr="DE123456789")
    assert expected == "lieferant:DE123456789"
    fact = Lieferant(
        id=expected,
        name="Beispiel",
        ust_id_nr="DE123456789",
        validity=open_window(),
        provenance=_prov(),
    )
    assert fact.id == expected


def test_ust_id_nr_normalises_whitespace_and_case() -> None:
    fact = Lieferant(
        id=Lieferant.make_id(ust_id_nr="de123456789"),
        name="Beispiel",
        ust_id_nr="de 123456789",  # type: ignore[arg-type]  # validated/normalised
        validity=open_window(),
        provenance=_prov(),
    )
    assert fact.ust_id_nr == "DE123456789"


def test_lieferant_rejects_extra_fields() -> None:
    with pytest.raises(ValidationError):
        Lieferant(
            id=Lieferant.make_id(ust_id_nr="DE123456789"),
            name="Beispiel",
            ust_id_nr="DE123456789",
            validity=open_window(),
            provenance=_prov(),
            quack="duck",  # type: ignore[call-arg]
        )


# ---------------------------------------------------------------------------
# BusinessGraph — DDL generation
# ---------------------------------------------------------------------------


def test_cypher_constraints_include_id_uniqueness_for_every_entity() -> None:
    ddl = BUSINESS_GRAPH.cypher_constraints()
    for ent in BUSINESS_GRAPH.entities:
        expected = f"{ent.__entity_label__.lower()}_id_unique"
        assert any(expected in s for s in ddl), f"missing constraint for {ent.__entity_label__}"


def test_cypher_constraints_include_validity_indexes() -> None:
    ddl = BUSINESS_GRAPH.cypher_constraints()
    assert any("ent_valid_window" in s for s in ddl)
    assert any("ent_source_system" in s for s in ddl)
    assert any("ent_idempotency" in s for s in ddl)


def test_diagram_renders_every_relationship() -> None:
    diagram = BUSINESS_GRAPH.to_diagram()
    for r in BUSINESS_GRAPH.relationships:
        assert r.name in diagram


# ---------------------------------------------------------------------------
# Idempotency key
# ---------------------------------------------------------------------------


def test_idempotency_key_is_deterministic() -> None:
    t = datetime(2026, 4, 15, 14, 23, 0, tzinfo=timezone.utc)
    a = make_idempotency_key(source_system=SourceSystem.SAP_HAGEN, source_id="L-4711", event_time=t)
    b = make_idempotency_key(source_system=SourceSystem.SAP_HAGEN, source_id="L-4711", event_time=t)
    assert a == b


def test_idempotency_key_changes_on_any_input_change() -> None:
    t = datetime(2026, 4, 15, 14, 23, 0, tzinfo=timezone.utc)
    base = make_idempotency_key(source_system=SourceSystem.SAP_HAGEN, source_id="L-4711", event_time=t)
    other_source = make_idempotency_key(source_system=SourceSystem.SAP_ASHEVILLE, source_id="L-4711", event_time=t)
    other_id = make_idempotency_key(source_system=SourceSystem.SAP_HAGEN, source_id="L-9999", event_time=t)
    other_time = make_idempotency_key(
        source_system=SourceSystem.SAP_HAGEN, source_id="L-4711", event_time=t + timedelta(seconds=1)
    )
    assert len({base, other_source, other_id, other_time}) == 4
