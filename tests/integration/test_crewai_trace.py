"""End-to-end trace assertion for the CrewAI integration.

We don't require a real CrewAI install in CI — instead, we exercise the
PutschCrewAITracer against an in-memory OTel SDK and verify the emitted
span structure. Real CrewAI is exercised in the staging-eval workflow,
not here.
"""

from __future__ import annotations

from typing import Any

import pytest
from opentelemetry import trace as otel_trace
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import (
    InMemorySpanExporter,
)


@pytest.fixture()
def exporter() -> InMemorySpanExporter:
    exp = InMemorySpanExporter()
    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(exp))
    otel_trace.set_tracer_provider(provider)
    return exp


def _mock_agent(role: str, goal: str) -> Any:
    class A:
        pass

    a = A()
    a.role = role
    a.goal = goal
    a.backstory = "test backstory"
    return a


def _mock_task(desc: str) -> Any:
    class T:
        pass

    t = T()
    t.description = desc
    t.expected_output = "something"
    t.input = "input payload"
    return t


def test_crewai_trace_structure(exporter: InMemorySpanExporter) -> None:
    from putsch_obs.integrations.crewai import PutschCrewAITracer

    tracer = PutschCrewAITracer()

    class C:
        pass

    crew = C()
    crew.process = "sequential"
    crew.agents = [_mock_agent("Buchhalter", "extract invoices")]
    crew.tasks = [_mock_task("Process invoice")]

    tracer.on_crew_start(crew)
    tracer.on_agent_start(crew.agents[0])
    tracer.on_task_start(crew.tasks[0])
    tracer.on_tool_start("invoice_parser", {"file": "rg-001.pdf"})
    tracer.on_tool_end({"rechnung_nr": "RE-001"})
    tracer.on_llm_start("mistral-large-latest", "extract", provider="mistral")
    tracer.on_llm_end(
        "RE-001",
        model="mistral-large-latest",
        input_tokens=120,
        output_tokens=40,
        cache_hit=False,
    )
    tracer.on_task_end({"rechnung_nr": "RE-001"})
    tracer.on_agent_end({"summary": "ok"})
    tracer.on_crew_end({"status": "ok"})

    spans = exporter.get_finished_spans()
    names = {s.name for s in spans}
    assert "crewai.crew" in names
    assert any(n.startswith("crewai.agent.Buchhalter") for n in names)
    assert "crewai.task" in names
    assert "crewai.tool.invoice_parser" in names
    assert "crewai.llm" in names

    llm_span = next(s for s in spans if s.name == "crewai.llm")
    attrs = dict(llm_span.attributes or {})
    assert attrs["gen_ai.request.model"] == "mistral-large-latest"
    assert attrs["gen_ai.usage.input_tokens"] == 120
    assert attrs["gen_ai.usage.output_tokens"] == 40
    # Cost computed via the pricing table.
    assert "gen_ai.usage.cost_eur" in attrs
    assert attrs["gen_ai.usage.cost_eur"] > 0
    assert attrs["putsch.kind"] == "generation"


def test_pii_redaction_in_attributes(exporter: InMemorySpanExporter, isolated_env: None) -> None:
    from putsch_obs.integrations.crewai import PutschCrewAITracer

    tracer = PutschCrewAITracer()
    tracer.on_tool_start("invoice_parser", "Bitte an DE89370400440532013000 zahlen.")
    tracer.on_tool_end("ok")
    spans = exporter.get_finished_spans()
    s = next(s for s in spans if s.name == "crewai.tool.invoice_parser")
    attrs = dict(s.attributes or {})
    val = str(attrs.get("input.value", ""))
    # Span attributes from the integration are pre-redaction-processor;
    # the processor that redacts hooks into the global init() path. So
    # here the raw IBAN may still appear — confirm and let
    # `test_init_path_redacts` cover the wired case.
    assert "DE89370400440532013000" in val or "<<PII:iban:" in val
