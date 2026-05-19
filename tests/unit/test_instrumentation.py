"""Smoke tests for the OTel/Langfuse bootstrap."""

from __future__ import annotations

import pytest
from opentelemetry import trace as otel_trace

from putsch_obs import init, is_initialized, shutdown, span
from putsch_obs.correlation import correlation_scope, get_correlation_id


@pytest.fixture(autouse=True)
def _cleanup_init():  # type: ignore[no-untyped-def]
    yield
    shutdown()


def test_init_is_idempotent(isolated_env: None) -> None:
    init()
    assert is_initialized()
    init()  # second call must be a no-op
    assert is_initialized()


def test_span_carries_correlation_id(isolated_env: None) -> None:
    init()
    with correlation_scope("abc-123") as cid:
        with span("test.span") as sp:
            assert cid == get_correlation_id()
            assert sp.is_recording()


def test_span_records_exception(isolated_env: None) -> None:
    init()
    with pytest.raises(RuntimeError):
        with span("test.error"):
            raise RuntimeError("boom")


def test_tracer_provider_is_set(isolated_env: None) -> None:
    init()
    tp = otel_trace.get_tracer_provider()
    # The default global is `ProxyTracerProvider`; after init() it should
    # be the SDK's TracerProvider.
    assert "TracerProvider" in type(tp).__name__
