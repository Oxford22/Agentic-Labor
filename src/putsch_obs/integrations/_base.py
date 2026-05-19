"""Shared utilities used by every integration.

Two things live here:

* :func:`safe` — decorator that traps every exception and logs a WARN so
  instrumentation failures don't propagate. Every callback that crosses
  the integration boundary uses it.
* :class:`CostCalculator` — token → EUR conversion driven by the pricing
  block in settings.
"""

from __future__ import annotations

import functools
import time
from collections.abc import Callable
from typing import Any, ParamSpec, TypeVar

from putsch_obs.config import PricingPerMillionTokens, get_settings
from putsch_obs.logging import get_logger

log = get_logger(__name__)

P = ParamSpec("P")
R = TypeVar("R")


def safe(name: str) -> Callable[[Callable[P, R]], Callable[P, R | None]]:
    """Wrap a callback so any exception is logged at WARN, never raised."""

    def deco(fn: Callable[P, R]) -> Callable[P, R | None]:
        @functools.wraps(fn)
        def wrapper(*args: P.args, **kwargs: P.kwargs) -> R | None:
            try:
                return fn(*args, **kwargs)
            except Exception as exc:
                log.warning(
                    "integration.callback_failed",
                    callback=name,
                    err=str(exc),
                    err_type=type(exc).__name__,
                )
                return None

        return wrapper

    return deco


class CostCalculator:
    """Compute EUR cost for an LLM call.

    The model-id → pricing-attribute mapping is intentionally explicit:
    "mistral-large-2407" should never silently fall through to a zero
    price. If the model is unknown, returns ``None`` and the caller
    records ``putsch.cost_unknown=true`` on the span — that surfaces in
    the routing dashboard.
    """

    _MAP: dict[str, tuple[str, str]] = {
        # model id              (input attr,                   output attr)
        "mistral-small-latest": ("mistral_small_input",         "mistral_small_output"),
        "mistral-small-2409":   ("mistral_small_input",         "mistral_small_output"),
        "mistral-large-latest": ("mistral_large_input",         "mistral_large_output"),
        "mistral-large-2407":   ("mistral_large_input",         "mistral_large_output"),
        "codestral-latest":     ("codestral_input",             "codestral_output"),
        "deepseek-chat":        ("deepseek_v3_input",           "deepseek_v3_output"),
        "deepseek-v3":          ("deepseek_v3_input",           "deepseek_v3_output"),
        "qwen3-14b":            ("qwen3_14b_local_input",       "qwen3_14b_local_output"),
        "qwen3-14b-redactor":   ("qwen3_14b_local_input",       "qwen3_14b_local_output"),
        "mistral-embed":        ("embed_mistral_input",         "embed_mistral_input"),
    }

    def __init__(self, pricing: PricingPerMillionTokens | None = None) -> None:
        self._pricing = pricing or get_settings().pricing

    def eur(
        self,
        model: str,
        *,
        input_tokens: int,
        output_tokens: int,
    ) -> float | None:
        key = model.lower()
        if key not in self._MAP:
            return None
        in_attr, out_attr = self._MAP[key]
        in_price = getattr(self._pricing, in_attr)
        out_price = getattr(self._pricing, out_attr)
        cost = (input_tokens * in_price + output_tokens * out_price) / 1_000_000.0
        return round(cost, 6)


class StopWatch:
    """Monotonic timer that returns elapsed milliseconds.

    Used in integrations because OTel ``span.end_time - start_time`` is only
    available post-end and we want to set ``putsch.latency_ms`` *as* an
    attribute on the live span.
    """

    __slots__ = ("_start",)

    def __init__(self) -> None:
        self._start = time.perf_counter()

    def elapsed_ms(self) -> float:
        return (time.perf_counter() - self._start) * 1000.0


__all__ = ["CostCalculator", "StopWatch", "safe"]
