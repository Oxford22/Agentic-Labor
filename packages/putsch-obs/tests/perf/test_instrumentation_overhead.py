"""Performance budget tests.

Asserts:
* < 2ms p99 added per LLM span (full path: open, set attrs, redact, close)
* < 500µs p99 added per tool span

These run only with pytest-benchmark installed. CI gates merges on them.
"""

from __future__ import annotations

import time

import pytest

from putsch_obs import init, shutdown, span
from putsch_obs.config import get_settings


@pytest.fixture(autouse=True)
def _instr():  # type: ignore[no-untyped-def]
    init()
    yield
    shutdown()


@pytest.mark.perf
def test_llm_span_overhead_p99() -> None:
    cfg = get_settings()
    n = 1000
    durations: list[float] = []
    for _ in range(n):
        t0 = time.perf_counter()
        with span(
            "perf.llm",
            attributes={
                "gen_ai.system": "mistral",
                "gen_ai.request.model": "mistral-large-latest",
                "gen_ai.usage.input_tokens": 100,
                "gen_ai.usage.output_tokens": 30,
                "input.value": "Bitte zahlen Sie an DE89370400440532013000",
                "output.value": "Erledigt.",
            },
        ):
            pass
        durations.append((time.perf_counter() - t0) * 1000.0)
    durations.sort()
    p99 = durations[int(n * 0.99) - 1]
    assert p99 < cfg.perf_budget_llm_p99_ms, (
        f"LLM span p99 = {p99:.3f} ms, budget {cfg.perf_budget_llm_p99_ms} ms"
    )


@pytest.mark.perf
def test_tool_span_overhead_p99() -> None:
    cfg = get_settings()
    n = 2000
    durations: list[float] = []
    for _ in range(n):
        t0 = time.perf_counter()
        with span("perf.tool", attributes={"tool.name": "fetch"}):
            pass
        durations.append((time.perf_counter() - t0) * 1_000_000.0)  # µs
    durations.sort()
    p99 = durations[int(n * 0.99) - 1]
    assert p99 < cfg.perf_budget_tool_p99_us, (
        f"tool span p99 = {p99:.1f} µs, budget {cfg.perf_budget_tool_p99_us} µs"
    )
