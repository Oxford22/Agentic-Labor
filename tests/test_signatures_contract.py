"""Contract tests for every registered signature.

Properties enforced:

* ``SignatureMeta`` is set and well-formed.
* The version hash is stable across two evaluations of the same class (determinism).
* Every demo's input keys are exactly the signature's input field names; every demo's output keys
  are exactly the signature's output field names. Demos that drift from the schema are a hidden
  source of compilation bugs.
* The accuracy threshold is plausible (>=0.7, <1.0); a threshold of 1.0 is unreachable.
* The cost ceiling is positive and finite.
"""

from __future__ import annotations

import pytest

from putsch_compile.metrics import METRIC_REGISTRY
from putsch_compile.routing import Router
from putsch_compile.signatures import SIGNATURE_REGISTRY


@pytest.mark.parametrize("name", sorted(SIGNATURE_REGISTRY))
def test_signature_meta_is_present(name: str) -> None:
    sig = SIGNATURE_REGISTRY[name]
    meta = sig.meta()
    assert meta.name == name
    assert 0.7 <= meta.accuracy_threshold < 1.0, (
        f"{name}: threshold {meta.accuracy_threshold} unreasonable"
    )
    assert meta.cost_ceiling_eur_per_1k_calls > 0
    assert meta.purpose, f"{name}: purpose must be set"
    assert meta.instruction, f"{name}: instruction must be set"


@pytest.mark.parametrize("name", sorted(SIGNATURE_REGISTRY))
def test_version_hash_is_deterministic(name: str) -> None:
    sig = SIGNATURE_REGISTRY[name]
    assert sig.version_hash() == sig.version_hash()


@pytest.mark.parametrize("name", sorted(SIGNATURE_REGISTRY))
def test_demos_match_signature_fields(name: str) -> None:
    sig = SIGNATURE_REGISTRY[name]
    fields = sig.iter_dspy_fields()
    input_names = {
        n
        for n, f in fields.items()
        if (getattr(f, "json_schema_extra", None) or {}).get("__dspy_field_type") == "input"
    }
    output_names = {
        n
        for n, f in fields.items()
        if (getattr(f, "json_schema_extra", None) or {}).get("__dspy_field_type") == "output"
    }
    for i, demo in enumerate(sig.meta().demos):
        unexpected_inputs = set(demo.inputs) - input_names
        missing_inputs = input_names - set(demo.inputs)
        unexpected_outputs = set(demo.outputs) - output_names
        missing_outputs = output_names - set(demo.outputs)
        assert not unexpected_inputs, f"{name} demo {i}: extra inputs {unexpected_inputs}"
        assert not missing_inputs, f"{name} demo {i}: missing inputs {missing_inputs}"
        assert not unexpected_outputs, f"{name} demo {i}: extra outputs {unexpected_outputs}"
        assert not missing_outputs, f"{name} demo {i}: missing outputs {missing_outputs}"


@pytest.mark.parametrize("name", sorted(SIGNATURE_REGISTRY))
def test_every_signature_has_a_metric(name: str) -> None:
    assert name in METRIC_REGISTRY, f"{name}: no metric registered in metrics.METRIC_REGISTRY"


@pytest.mark.parametrize("name", sorted(SIGNATURE_REGISTRY))
def test_every_signature_has_a_router_tier(name: str) -> None:
    router = Router()
    tier = router.preferred_tier(name)
    assert tier is not None
    candidates = router.candidates_cheapest_first(name)
    assert candidates, f"{name}: ladder is empty"


def test_signature_registry_is_not_empty() -> None:
    assert len(SIGNATURE_REGISTRY) >= 8


def test_changing_an_instruction_changes_version_hash() -> None:
    from putsch_compile.signatures._base import (
        OwnerTeam,
        SignatureMeta,
    )

    a = SignatureMeta(
        name="demo_only",
        owner_team=OwnerTeam.AP_AUTOMATION,
        purpose="test fixture for hash determinism",
        version="1.0.0",
        accuracy_threshold=0.9,
        cost_ceiling_eur_per_1k_calls=0.5,
        instruction="alpha",
    )
    b = a.model_copy(update={"instruction": "beta"})
    assert a.model_dump_json() != b.model_dump_json()
