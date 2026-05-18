"""DSPy adapter wiring. ``BAMLAdapter`` is the default for every Putsch signature.

Why BAML rather than the default JSON adapter: on small / cheap models (Qwen2.5-72B and below),
BAML's structured-output representation halves the parse-error rate compared to JSON schemas. That
is what lets Putsch run 70B-class models on bounded extraction tasks and still match 200B-class
frontier performance for them. The half-the-error rate is the deal — without it, the cheapest-model
ladder would collapse to the second cheapest.

Where the adapter cannot bind the model output to the signature, we raise ``AdapterError`` with the
raw output attached as context — never silently swallow.
"""

from __future__ import annotations

from typing import Any

import dspy

from putsch_compile.config import get_settings
from putsch_compile.exceptions import AdapterError
from putsch_compile.logging import get_logger

_log = get_logger(__name__)

_baml_adapter_cls: type[dspy.Adapter] | None = None


def _baml_adapter() -> type[dspy.Adapter]:
    """Import lazily — DSPy moves the adapter location between minor versions."""

    global _baml_adapter_cls
    if _baml_adapter_cls is not None:
        return _baml_adapter_cls
    try:
        from dspy.adapters.baml_adapter import BAMLAdapter  # type: ignore[import-not-found]
    except ImportError:
        try:
            from dspy.adapters import BAMLAdapter  # type: ignore[import-not-found,no-redef]
        except ImportError as exc:  # pragma: no cover - env mismatch
            raise AdapterError(
                "BAMLAdapter not available — check dspy-ai >= 2.5.20 in pyproject.toml",
                context={"error": str(exc)},
            ) from exc
    _baml_adapter_cls = BAMLAdapter
    return BAMLAdapter


def configure_dspy(*, model: str, api_base: str | None = None, **kwargs: Any) -> None:
    """Configure DSPy with BAML as the default adapter and the given LM.

    All Putsch code paths go through this — direct ``dspy.configure(...)`` is forbidden because it
    leaves the adapter at the JSON default and silently degrades small-model accuracy.
    """

    settings = get_settings()
    base_url = api_base or settings.litellm.proxy_base_url
    api_key = settings.litellm.proxy_api_key.get_secret_value() or "sk-litellm-proxy"

    lm = dspy.LM(
        model=model,
        api_base=base_url,
        api_key=api_key,
        cache=False,  # caching is at the LiteLLM proxy + Langfuse layer, not the SDK
        timeout=settings.litellm.request_timeout_s,
        num_retries=settings.litellm.max_retries,
        **kwargs,
    )
    dspy.configure(lm=lm, adapter=_baml_adapter()())
    _log.info("dspy.configured", model=model, adapter="BAMLAdapter")


class BAMLBoundCall:
    """Helper that wraps a DSPy program call and converts BAML parse failures into ``AdapterError``.

    Use at the *call site*, not as a Module subclass — keeps the program graph clean and the error
    surface in a single place that on-call can grep for.
    """

    def __init__(self, program: dspy.Module, *, signature_name: str) -> None:
        self._program = program
        self._signature_name = signature_name

    def __call__(self, **inputs: Any) -> Any:
        try:
            return self._program(**inputs)
        except (TypeError, ValueError) as exc:
            raise AdapterError(
                f"BAML parse failure on {self._signature_name}",
                context={"error": str(exc), "signature": self._signature_name},
            ) from exc
