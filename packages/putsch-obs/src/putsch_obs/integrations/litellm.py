"""LiteLLM proxy/SDK instrumentation.

LiteLLM is the single LLM gateway in the Putsch stack — every model call,
whether to Mistral La Plateforme or to a local Qwen, goes through it.
Capturing here gives us a chokepoint for cost and latency.

The hook is registered via LiteLLM's ``success_callback`` / ``failure_callback``
mechanism. Both are stack-pushed (LiteLLM iterates them), so we add a
single class instance with ``log_success_event`` / ``log_failure_event``
methods.
"""

from __future__ import annotations

import threading
from typing import Any

from opentelemetry import trace as otel_trace

from putsch_obs.instrumentation import get_tracer, is_initialized
from putsch_obs.integrations._base import CostCalculator, safe
from putsch_obs.logging import get_logger

log = get_logger(__name__)

_INSTALLED = False
_LOCK = threading.Lock()


class PutschLiteLLMCallback:
    """Implements LiteLLM's CustomLogger protocol."""

    def __init__(self) -> None:
        if not is_initialized():
            from putsch_obs.instrumentation import init

            init()
        self._tracer = get_tracer("putsch_obs.litellm")
        self._cost = CostCalculator()

    @safe("litellm.success")
    def log_success_event(
        self,
        kwargs: dict[str, Any],
        response_obj: Any,
        start_time: float,
        end_time: float,
    ) -> None:
        self._emit("ok", kwargs, response_obj, start_time, end_time, error=None)

    @safe("litellm.failure")
    def log_failure_event(
        self,
        kwargs: dict[str, Any],
        response_obj: Any,
        start_time: float,
        end_time: float,
    ) -> None:
        err = kwargs.get("exception") or kwargs.get("error")
        self._emit("error", kwargs, response_obj, start_time, end_time, error=err)

    # async variants
    @safe("litellm.async_success")
    async def async_log_success_event(
        self,
        kwargs: dict[str, Any],
        response_obj: Any,
        start_time: float,
        end_time: float,
    ) -> None:
        self.log_success_event(kwargs, response_obj, start_time, end_time)

    @safe("litellm.async_failure")
    async def async_log_failure_event(
        self,
        kwargs: dict[str, Any],
        response_obj: Any,
        start_time: float,
        end_time: float,
    ) -> None:
        self.log_failure_event(kwargs, response_obj, start_time, end_time)

    # ── implementation ──────────────────────────────────────────────────

    def _emit(
        self,
        status: str,
        kwargs: dict[str, Any],
        response_obj: Any,
        start_time: float,
        end_time: float,
        *,
        error: Any,
    ) -> None:
        model = (
            kwargs.get("model")
            or (getattr(response_obj, "model", None))
            or ""
        )
        provider = _provider_from_model(str(model))
        usage = _usage(response_obj)
        in_toks = int(usage.get("prompt_tokens", 0))
        out_toks = int(usage.get("completion_tokens", 0))
        cache_hit = bool(
            (kwargs.get("cache_hit"))
            or (
                isinstance(response_obj, dict)
                and response_obj.get("cache_hit")
            )
        )
        cost = self._cost.eur(
            str(model),
            input_tokens=in_toks,
            output_tokens=out_toks,
        )
        latency_ms = max(0.0, (end_time - start_time) * 1000.0)

        # We can't reattach to the live span because LiteLLM callbacks fire
        # after the call returns, so we open and close a generation span
        # here, with the current OTel context as the parent (which is the
        # caller's span — exactly what we want).
        sp = self._tracer.start_span(
            "litellm.completion",
            kind=otel_trace.SpanKind.CLIENT,
        )
        try:
            sp.set_attribute("putsch.kind", "generation")
            sp.set_attribute("gen_ai.system", provider)
            sp.set_attribute("gen_ai.request.model", str(model))
            sp.set_attribute("gen_ai.response.model", str(model))
            sp.set_attribute("gen_ai.usage.input_tokens", in_toks)
            sp.set_attribute("gen_ai.usage.output_tokens", out_toks)
            sp.set_attribute("putsch.latency_ms", latency_ms)
            sp.set_attribute("putsch.cache_hit", cache_hit)
            if cost is not None:
                sp.set_attribute("gen_ai.usage.cost_eur", cost)
            else:
                sp.set_attribute("putsch.cost_unknown", True)
            messages = kwargs.get("messages")
            if messages is not None:
                sp.set_attribute("input.value", _stringify(messages))
            if status == "ok":
                sp.set_attribute("output.value", _extract_output(response_obj))
            else:
                if error is not None:
                    sp.set_attribute("error", True)
                    sp.set_attribute("error.type", type(error).__name__)
                    sp.set_attribute("error.message", str(error))
                    sp.set_status(
                        otel_trace.Status(
                            otel_trace.StatusCode.ERROR, str(error)
                        )
                    )
        finally:
            sp.end()


def install() -> None:
    """Register the callback with LiteLLM. Idempotent."""
    global _INSTALLED
    with _LOCK:
        if _INSTALLED:
            return
        try:
            import litellm  # type: ignore[import-not-found]
        except ImportError as exc:
            raise ImportError(
                "litellm is not installed; pip install putsch-obs[integrations]"
            ) from exc

        cb = PutschLiteLLMCallback()
        # LiteLLM dedupes by identity; this is safe to call repeatedly.
        if not any(
            isinstance(c, PutschLiteLLMCallback)
            for c in (getattr(litellm, "success_callback", None) or [])
        ):
            litellm.success_callback = (
                list(getattr(litellm, "success_callback", []) or []) + [cb]
            )
        if not any(
            isinstance(c, PutschLiteLLMCallback)
            for c in (getattr(litellm, "failure_callback", None) or [])
        ):
            litellm.failure_callback = (
                list(getattr(litellm, "failure_callback", []) or []) + [cb]
            )
        _INSTALLED = True
        log.info("litellm.instrumentation_installed")


def _provider_from_model(model: str) -> str:
    m = model.lower()
    if m.startswith("mistral") or m.startswith("codestral") or m.startswith("magistral"):
        return "mistral"
    if m.startswith("deepseek"):
        return "deepseek"
    if m.startswith("qwen"):
        return "qwen"
    if "/" in m:
        return m.split("/", 1)[0]
    return "unknown"


def _usage(response_obj: Any) -> dict[str, Any]:
    if response_obj is None:
        return {}
    if isinstance(response_obj, dict):
        u = response_obj.get("usage") or {}
    else:
        u = getattr(response_obj, "usage", None) or {}
        if hasattr(u, "model_dump"):
            u = u.model_dump()
        elif hasattr(u, "dict"):
            u = u.dict()
    return u if isinstance(u, dict) else {}


def _extract_output(response_obj: Any) -> str:
    try:
        if isinstance(response_obj, dict):
            choices = response_obj.get("choices") or []
        else:
            choices = getattr(response_obj, "choices", None) or []
        if not choices:
            return ""
        first = choices[0]
        msg = first.get("message") if isinstance(first, dict) else getattr(first, "message", None)
        if msg is None:
            return ""
        content = msg.get("content") if isinstance(msg, dict) else getattr(msg, "content", None)
        return str(content or "")
    except Exception:
        return ""


def _stringify(v: Any) -> str:
    if isinstance(v, str):
        return v
    try:
        import json

        return json.dumps(v, default=str, ensure_ascii=False)
    except Exception:
        return str(v)


__all__ = ["PutschLiteLLMCallback", "install"]
