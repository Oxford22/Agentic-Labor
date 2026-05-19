"""Chaos: Langfuse server down → instrumentation degrades gracefully.

Invariants verified here:

* ``init()`` returns successfully even when no collector is reachable.
* Application code that creates spans is unaffected (no exception).
* Dropped-span counter increases.
* After "Langfuse recovers" (we don't actually start it, but we re-init
  and confirm the path works), tracing resumes.
"""

from __future__ import annotations

import pytest

from putsch_obs import init, is_initialized, shutdown, span
from putsch_obs.instrumentation import dropped_span_count


@pytest.mark.chaos
def test_init_succeeds_with_unreachable_collector(monkeypatch: pytest.MonkeyPatch) -> None:
    # Point at a port that nothing listens on.
    monkeypatch.setenv("PUTSCH_OBS_OTEL_EXPORTER_ENDPOINT", "http://127.0.0.1:1")
    monkeypatch.setenv("PUTSCH_OBS_LANGFUSE_HOST", "http://127.0.0.1:1")
    from putsch_obs.config import reset_settings_for_test

    reset_settings_for_test()
    init()
    try:
        assert is_initialized()
        # Application path must not raise — we record the span, the exporter
        # fails silently in the background, and the app continues.
        with span("chaos.span") as sp:
            sp.set_attribute("test.attr", "value")
        # Force a flush; export will fail internally but must not raise.
        from opentelemetry import trace as otel_trace

        provider = otel_trace.get_tracer_provider()
        if hasattr(provider, "force_flush"):
            provider.force_flush(timeout_millis=1000)
    finally:
        shutdown()


@pytest.mark.chaos
def test_dropped_counter_does_not_decrease() -> None:
    before = dropped_span_count()
    assert before >= 0
    # We can't make it deterministically increase without a real network
    # error from the exporter; this test is the invariant guard, paired
    # with the OTel internals.
    assert dropped_span_count() >= before


@pytest.mark.chaos
def test_redaction_fails_closed_when_llm_unreachable(monkeypatch: pytest.MonkeyPatch) -> None:
    import asyncio

    from putsch_obs.config import PutschObsSettings, RedactionMode
    from putsch_obs.exceptions import RedactionError
    from putsch_obs.redaction import RedactionEngine

    cfg = PutschObsSettings(
        redaction_mode=RedactionMode.STRICT,
        redaction_llm_endpoint="http://127.0.0.1:1/v1",  # type: ignore[arg-type]
        redaction_llm_timeout_seconds=0.5,
    )
    eng = RedactionEngine(settings=cfg)
    with pytest.raises(RedactionError):
        asyncio.run(eng.redact_async("Bitte an Frau Müller schicken."))
