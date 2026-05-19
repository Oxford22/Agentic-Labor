"""DSPy instrumentation.

DSPy ``Predict`` / ``ChainOfThought`` / ``ReAct`` modules are the compiled
prompt artefacts in the Putsch stack. We need to record:

* The **signature hash** — a stable identifier of the input/output schema
* The **optimizer version** — the GEPA / MIPROv2 / BootstrapFewShot run
  that produced the compiled artefact (read from the artefact's
  ``compile_version`` attribute, populated by the compiler in `models.py`)
* The **compiled prompt hash** — sha256 of the rendered demos + instruction
* **Per-call score** — populated by the eval harness when running against
  a dataset; absent on production traces

The instrumentation is a monkey-patch of ``dspy.Predict.__call__``. We do
it at ``install()`` time, not on import, so import order doesn't matter.
"""

from __future__ import annotations

import contextlib
import hashlib
import json
import threading
from typing import Any, Callable

from putsch_obs.instrumentation import get_tracer, is_initialized
from putsch_obs.integrations._base import StopWatch, safe
from putsch_obs.logging import get_logger

log = get_logger(__name__)


_INSTALLED = False
_LOCK = threading.Lock()


def install() -> None:
    """Monkey-patch DSPy. Idempotent."""
    global _INSTALLED
    with _LOCK:
        if _INSTALLED:
            return
        try:
            import dspy  # type: ignore[import-not-found]
        except ImportError as exc:
            raise ImportError(
                "dspy-ai is not installed; pip install putsch-obs[integrations]"
            ) from exc

        if not is_initialized():
            from putsch_obs.instrumentation import init

            init()

        original_call: Callable[..., Any] = dspy.Predict.__call__

        @safe("dspy.Predict.__call__")
        def _traced_call(self: Any, *args: Any, **kwargs: Any) -> Any:
            tracer = get_tracer("putsch_obs.dspy")
            sig_repr = _signature_repr(getattr(self, "signature", None))
            sig_hash = hashlib.sha256(sig_repr.encode("utf-8")).hexdigest()[:16]
            compile_version = getattr(self, "compile_version", None)
            prompt_hash = _compiled_prompt_hash(self)

            with tracer.start_as_current_span("dspy.predict") as sp:
                sp.set_attribute("putsch.kind", "generation")
                sp.set_attribute("dspy.module", type(self).__name__)
                sp.set_attribute("dspy.signature", sig_repr)
                sp.set_attribute("dspy.signature_hash", sig_hash)
                sp.set_attribute("dspy.compiled_prompt_hash", prompt_hash)
                if compile_version is not None:
                    sp.set_attribute("dspy.compile_version", str(compile_version))
                if args:
                    sp.set_attribute("input.value", _stringify(args[0]))
                if kwargs:
                    sp.set_attribute("input.kwargs", _stringify(kwargs))
                watch = StopWatch()
                try:
                    result = original_call(self, *args, **kwargs)
                except Exception:
                    sp.set_attribute("putsch.latency_ms", watch.elapsed_ms())
                    raise
                sp.set_attribute("putsch.latency_ms", watch.elapsed_ms())
                sp.set_attribute("output.value", _stringify(result))
                score = _attached_score(result)
                if score is not None:
                    sp.set_attribute("putsch.quality_score", float(score))
                return result

        dspy.Predict.__call__ = _traced_call  # type: ignore[method-assign]
        _INSTALLED = True
        log.info("dspy.instrumentation_installed")


def _signature_repr(signature: Any) -> str:
    if signature is None:
        return ""
    # DSPy signatures expose `signature` (the docstring/instruction) and
    # `fields` (an ordered dict).
    try:
        fields = getattr(signature, "fields", None) or {}
        return json.dumps(
            {"doc": getattr(signature, "signature", ""), "fields": list(fields.keys())},
            sort_keys=True,
        )
    except Exception:
        return str(signature)


def _compiled_prompt_hash(predictor: Any) -> str:
    parts: list[str] = []
    demos = getattr(predictor, "demos", None) or []
    for d in demos:
        parts.append(_stringify(d))
    instruction = ""
    sig = getattr(predictor, "signature", None)
    if sig is not None:
        instruction = getattr(sig, "signature", "") or ""
    blob = instruction + "\n---\n" + "\n".join(parts)
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()[:16]


def _attached_score(result: Any) -> float | None:
    # Eval runners stash a per-call score under `_putsch_score`. Production
    # traces don't have one and this returns None — that's correct.
    with contextlib.suppress(Exception):
        score = getattr(result, "_putsch_score", None)
        if score is not None:
            return float(score)
    return None


def _stringify(v: Any) -> str:
    if v is None:
        return ""
    if isinstance(v, str):
        return v
    with contextlib.suppress(Exception):
        return json.dumps(v, default=str, ensure_ascii=False)
    return str(v)


__all__ = ["install"]
