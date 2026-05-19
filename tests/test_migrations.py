"""Schema-evolution tests.

We promise: when the ontology changes (adds a new entity, adds a new
attribute), historical facts are not lost. The migration runner is
*additive* — it only CREATEs constraints/indexes. Destructive
migrations are out of scope here on purpose.
"""

from __future__ import annotations

from datetime import datetime, timezone

from putsch_memory.migrations import _fingerprint
from putsch_memory.ontology import BUSINESS_GRAPH


def test_fingerprint_is_stable_across_runs() -> None:
    c1 = BUSINESS_GRAPH.cypher_constraints()
    c2 = BUSINESS_GRAPH.cypher_constraints()
    assert _fingerprint(c1) == _fingerprint(c2)


def test_constraints_are_idempotent_create_if_not_exists() -> None:
    for cypher in BUSINESS_GRAPH.cypher_constraints():
        assert "IF NOT EXISTS" in cypher, f"non-idempotent migration: {cypher}"


def test_every_entity_has_id_uniqueness() -> None:
    constraints = BUSINESS_GRAPH.cypher_constraints()
    for ent in BUSINESS_GRAPH.entities:
        label = ent.__entity_label__.lower()
        assert any(
            f"{label}_id_unique" in c and "REQUIRE n.id IS UNIQUE" in c for c in constraints
        ), f"missing id-uniqueness constraint for {ent.__entity_label__}"


def test_no_destructive_constraints_emitted() -> None:
    for cypher in BUSINESS_GRAPH.cypher_constraints():
        up = cypher.upper()
        # The migration runner is additive: no DROPs or DELETEs.
        assert "DROP " not in up, f"destructive migration: {cypher}"
        assert "DELETE " not in up, f"destructive migration: {cypher}"
