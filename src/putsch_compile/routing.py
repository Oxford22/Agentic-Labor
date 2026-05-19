"""Per-signature model routing through LiteLLM.

The router never references a model SDK directly. It returns a LiteLLM model identifier; the call
site hands that to ``configure_dspy``. Swapping providers is a config change to the tier table
below, never a code change.

Tier definitions are declared once here and consumed by:

* The optimizer (``optimize.py``) — to walk the cheapest-first ladder.
* The production agents — to resolve which model to call for a given signature.
* The registry — to record (signature, model, version) tuples.

A signature without a tier preference is a routing bug — we refuse to fall through to a default
model. The signature owner must declare intent.
"""

from __future__ import annotations

from enum import IntEnum
from typing import Final

from pydantic import BaseModel, ConfigDict, Field

from putsch_compile.exceptions import RoutingError


class ModelTier(IntEnum):
    """Ranked tiers, cheapest = highest number. Walked bottom-up by the optimizer."""

    REASONING = 1
    GERMAN_PROSE = 2
    EXTRACTION = 3
    CODE = 4
    CHEAP_CLASSIFICATION = 5


class ModelCard(BaseModel):
    """A concrete model the router can dispatch to.

    ``id`` is the LiteLLM identifier — the only string the call site sees. ``eur_per_1k_input`` and
    ``eur_per_1k_output`` are the realised, Frankfurt-region prices observed in the LiteLLM proxy.
    They drive the cost-per-call estimate in the optimizer; they are not contractual but they are
    audited monthly.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    id: str = Field(..., min_length=3)
    tier: ModelTier
    eur_per_1k_input: float = Field(..., ge=0.0)
    eur_per_1k_output: float = Field(..., ge=0.0)
    context_window: int = Field(..., ge=4_096)
    locale_strength: str = Field(
        ..., description="Coarse note: 'de-DE', 'en-US', 'multilingual'."
    )
    supports_structured_output: bool = True


# The catalog. Keep alphabetical inside each tier so PR diffs are clean.
MODEL_CATALOG: Final[tuple[ModelCard, ...]] = (
    # ---- Tier 1: reasoning (expensive, slow) ----
    ModelCard(
        id="mistral/mistral-large-2411",
        tier=ModelTier.REASONING,
        eur_per_1k_input=0.0020,
        eur_per_1k_output=0.0060,
        context_window=128_000,
        locale_strength="multilingual",
    ),
    ModelCard(
        id="deepseek/deepseek-r1",
        tier=ModelTier.REASONING,
        eur_per_1k_input=0.0014,
        eur_per_1k_output=0.0028,
        context_window=64_000,
        locale_strength="multilingual",
    ),
    # ---- Tier 2: German prose ----
    ModelCard(
        id="mistral/mistral-medium-2412",
        tier=ModelTier.GERMAN_PROSE,
        eur_per_1k_input=0.0008,
        eur_per_1k_output=0.0024,
        context_window=128_000,
        locale_strength="multilingual",
    ),
    # ---- Tier 3: extraction ----
    ModelCard(
        id="mistral/open-mistral-nemo",
        tier=ModelTier.EXTRACTION,
        eur_per_1k_input=0.00015,
        eur_per_1k_output=0.00015,
        context_window=128_000,
        locale_strength="multilingual",
    ),
    ModelCard(
        id="qwen/qwen2.5-72b-instruct",
        tier=ModelTier.EXTRACTION,
        eur_per_1k_input=0.00040,
        eur_per_1k_output=0.00060,
        context_window=128_000,
        locale_strength="multilingual",
    ),
    # ---- Tier 4: code ----
    ModelCard(
        id="qwen/qwen2.5-coder-32b-instruct",
        tier=ModelTier.CODE,
        eur_per_1k_input=0.00018,
        eur_per_1k_output=0.00036,
        context_window=128_000,
        locale_strength="en-US",
    ),
    # ---- Tier 5: cheap classification ----
    ModelCard(
        id="qwen/qwen3-14b-instruct",
        tier=ModelTier.CHEAP_CLASSIFICATION,
        eur_per_1k_input=0.00006,
        eur_per_1k_output=0.00012,
        context_window=64_000,
        locale_strength="multilingual",
    ),
)


# Per-signature *preferred* tier. The optimizer may end up choosing a cheaper tier for a given
# signature if the cheaper tier proves to meet the accuracy threshold on holdout — that is the
# entire point of the cheapest-model-first ladder.
_SIGNATURE_TIER: Final[dict[str, ModelTier]] = {
    "classify_invoice_exception": ModelTier.REASONING,
    "reconcile_master_data": ModelTier.REASONING,
    "draft_mahnung_letter": ModelTier.GERMAN_PROSE,
    "draft_customer_email": ModelTier.GERMAN_PROSE,
    "summarize_audit_trail": ModelTier.GERMAN_PROSE,
    "extract_invoice_fields": ModelTier.EXTRACTION,
    "generate_datev_booking_code": ModelTier.CODE,
    "classify_hs_code": ModelTier.CHEAP_CLASSIFICATION,
}


class Router:
    """Single source of truth for "which model for which signature, today?".

    Stateless. Construct once at process boot, reuse forever. The catalog and the per-signature
    preferences are class-level constants; tests can monkey-patch via ``with_catalog()``.
    """

    def __init__(
        self,
        catalog: tuple[ModelCard, ...] = MODEL_CATALOG,
        preferences: dict[str, ModelTier] | None = None,
    ) -> None:
        self._catalog = catalog
        self._preferences: dict[str, ModelTier] = (
            dict(preferences) if preferences is not None else dict(_SIGNATURE_TIER)
        )
        # Pre-index by tier for O(1) lookup.
        self._by_tier: dict[ModelTier, list[ModelCard]] = {}
        for card in catalog:
            self._by_tier.setdefault(card.tier, []).append(card)
        for cards in self._by_tier.values():
            cards.sort(key=lambda c: c.eur_per_1k_input + c.eur_per_1k_output)

    def preferred_tier(self, signature_name: str) -> ModelTier:
        try:
            return self._preferences[signature_name]
        except KeyError as exc:
            raise RoutingError(
                f"signature {signature_name!r} has no tier preference declared",
                context={"signature": signature_name},
            ) from exc

    def candidates_cheapest_first(self, signature_name: str) -> list[ModelCard]:
        """Walking order for the optimizer: cheapest valid tier first, then climb.

        We *always* include cheaper tiers than the declared preference — the whole point of the
        ladder is that the optimizer may prove a cheaper tier suffices.
        """

        preferred = self.preferred_tier(signature_name)
        out: list[ModelCard] = []
        for tier in sorted(ModelTier, key=int, reverse=True):  # 5 → 4 → 3 → 2 → 1
            if tier.value < preferred.value:
                # tier number is *smaller* than preferred; this is a *more expensive* tier.
                # We allow climbing into it as a fallback.
                pass
            cards = self._by_tier.get(tier, [])
            out.extend(cards)
        return out

    def cheapest_in_tier(self, tier: ModelTier) -> ModelCard:
        cards = self._by_tier.get(tier)
        if not cards:
            raise RoutingError(f"no models in tier {tier.name}", context={"tier": tier.name})
        return cards[0]

    def get_card(self, model_id: str) -> ModelCard:
        for card in self._catalog:
            if card.id == model_id:
                return card
        raise RoutingError(f"model {model_id!r} not in catalog", context={"model": model_id})

    def estimate_cost_eur_per_call(
        self, model_id: str, *, in_tokens: int, out_tokens: int
    ) -> float:
        card = self.get_card(model_id)
        return (
            (in_tokens / 1000.0) * card.eur_per_1k_input
            + (out_tokens / 1000.0) * card.eur_per_1k_output
        )
