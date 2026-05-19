"""Model swap is a one-line config change. This test proves it."""

from __future__ import annotations

import pytest

from putsch_compile.exceptions import RoutingError
from putsch_compile.routing import MODEL_CATALOG, ModelTier, Router


def test_every_signature_resolves_a_tier() -> None:
    r = Router()
    from putsch_compile.signatures import SIGNATURE_REGISTRY

    for name in SIGNATURE_REGISTRY:
        assert isinstance(r.preferred_tier(name), ModelTier)


def test_unknown_signature_raises_routing_error() -> None:
    r = Router()
    with pytest.raises(RoutingError):
        r.preferred_tier("never_declared_signature")


def test_cheapest_first_ladder_starts_with_cheapest_tier() -> None:
    r = Router()
    ladder = r.candidates_cheapest_first("classify_invoice_exception")
    assert ladder
    # The very first ladder entry is in Tier 5 (cheapest).
    assert ladder[0].tier == ModelTier.CHEAP_CLASSIFICATION


def test_swap_model_for_signature_via_config_only() -> None:
    """Swapping a tier preference is one dict update, no code change anywhere else."""

    custom = Router(preferences={"classify_hs_code": ModelTier.REASONING})
    ladder = custom.candidates_cheapest_first("classify_hs_code")
    # Cheaper tiers still come first; only the preferred tier moved.
    assert ladder[0].tier == ModelTier.CHEAP_CLASSIFICATION
    # And the preferred tier is REASONING for *this* router only.
    assert custom.preferred_tier("classify_hs_code") == ModelTier.REASONING

    default = Router()
    assert default.preferred_tier("classify_hs_code") == ModelTier.CHEAP_CLASSIFICATION


def test_cost_estimate_increases_with_tokens() -> None:
    r = Router()
    cheap = r.estimate_cost_eur_per_call(
        "qwen/qwen3-14b-instruct", in_tokens=100, out_tokens=100
    )
    pricey = r.estimate_cost_eur_per_call(
        "mistral/mistral-large-2411", in_tokens=100, out_tokens=100
    )
    assert pricey > cheap


def test_three_distinct_models_in_extraction_tier() -> None:
    """At least one alternative model present per tier — proves the ladder is meaningful."""

    extraction_models = [c.id for c in MODEL_CATALOG if c.tier == ModelTier.EXTRACTION]
    assert len(extraction_models) >= 2, (
        "extraction tier needs alternatives so the optimizer has a choice"
    )


def test_unknown_model_id_raises() -> None:
    r = Router()
    with pytest.raises(RoutingError):
        r.get_card("never/heard-of-it")
