"""CrewAI ↔ Langfuse tracing.

CrewAI's callback surface is event-driven: agents, tasks, and tools each
emit ``on_*`` callbacks. We map each to an OTel span and to a Langfuse
observation. The mapping is:

| CrewAI event           | OTel span                       | Langfuse type  |
| ---------------------- | ------------------------------- | -------------- |
| crew kickoff           | ``crewai.crew`` (root)          | ``trace``      |
| agent step             | ``crewai.agent.{role}``         | ``span``       |
| task                   | ``crewai.task``                 | ``span``       |
| tool call              | ``crewai.tool.{name}``          | ``span``       |
| llm call               | ``crewai.llm``                  | ``generation`` |

Agent role/goal/backstory go on the agent span as attributes (redacted
through the engine like everything else). Task input/output go as
``input.value`` / ``output.value`` attributes.
"""

from __future__ import annotations

from typing import Any

from opentelemetry import trace as otel_trace
from opentelemetry.trace import Span

from putsch_obs.instrumentation import get_tracer, is_initialized
from putsch_obs.integrations._base import CostCalculator, StopWatch, safe
from putsch_obs.logging import get_logger

log = get_logger(__name__)


class PutschCrewAITracer:
    """Drop-in callback handler.

    Usage:

        from crewai import Crew
        from putsch_obs.integrations.crewai import PutschCrewAITracer

        crew = Crew(
            agents=[...], tasks=[...],
            callbacks=[PutschCrewAITracer()],
        )
    """

    def __init__(self) -> None:
        if not is_initialized():
            from putsch_obs.instrumentation import init

            init()
        self._tracer = get_tracer("putsch_obs.crewai")
        self._cost = CostCalculator()
        self._stack: list[tuple[Span, StopWatch]] = []

    # ── helpers ──────────────────────────────────────────────────────────

    def _start(self, name: str, attrs: dict[str, Any]) -> tuple[Span, StopWatch]:
        sp = self._tracer.start_span(
            name,
            kind=otel_trace.SpanKind.INTERNAL,
        )
        for k, v in attrs.items():
            sp.set_attribute(k, v)
        watch = StopWatch()
        self._stack.append((sp, watch))
        return sp, watch

    def _end(self, attrs: dict[str, Any] | None = None) -> None:
        if not self._stack:
            return
        sp, watch = self._stack.pop()
        sp.set_attribute("putsch.latency_ms", watch.elapsed_ms())
        if attrs:
            for k, v in attrs.items():
                sp.set_attribute(k, v)
        sp.end()

    # ── crew lifecycle ──────────────────────────────────────────────────

    @safe("crewai.on_crew_start")
    def on_crew_start(self, crew: Any) -> None:
        self._start(
            "crewai.crew",
            {
                "putsch.kind": "crew",
                "crewai.crew.process": str(getattr(crew, "process", "sequential")),
                "crewai.crew.agent_count": len(getattr(crew, "agents", []) or []),
                "crewai.crew.task_count": len(getattr(crew, "tasks", []) or []),
            },
        )

    @safe("crewai.on_crew_end")
    def on_crew_end(self, output: Any) -> None:
        self._end({"output.value": _stringify(output)})

    # ── agents ──────────────────────────────────────────────────────────

    @safe("crewai.on_agent_start")
    def on_agent_start(self, agent: Any) -> None:
        self._start(
            f"crewai.agent.{getattr(agent, 'role', 'unknown')}",
            {
                "putsch.kind": "agent",
                "crewai.agent.role": getattr(agent, "role", ""),
                "crewai.agent.goal": getattr(agent, "goal", ""),
                "crewai.agent.backstory": getattr(agent, "backstory", ""),
                "crewai.agent.allow_delegation": bool(
                    getattr(agent, "allow_delegation", False)
                ),
            },
        )

    @safe("crewai.on_agent_end")
    def on_agent_end(self, output: Any) -> None:
        self._end({"output.value": _stringify(output)})

    # ── tasks ───────────────────────────────────────────────────────────

    @safe("crewai.on_task_start")
    def on_task_start(self, task: Any) -> None:
        self._start(
            "crewai.task",
            {
                "putsch.kind": "task",
                "crewai.task.description": getattr(task, "description", ""),
                "crewai.task.expected_output": getattr(task, "expected_output", ""),
                "input.value": _stringify(getattr(task, "input", None)),
            },
        )

    @safe("crewai.on_task_end")
    def on_task_end(self, output: Any) -> None:
        self._end(
            {
                "output.value": _stringify(output),
                "crewai.task.output_format": type(output).__name__,
            }
        )

    # ── tools ───────────────────────────────────────────────────────────

    @safe("crewai.on_tool_start")
    def on_tool_start(self, tool_name: str, args: Any) -> None:
        self._start(
            f"crewai.tool.{tool_name}",
            {
                "putsch.kind": "tool",
                "tool.name": tool_name,
                "input.value": _stringify(args),
            },
        )

    @safe("crewai.on_tool_end")
    def on_tool_end(self, output: Any, error: BaseException | None = None) -> None:
        attrs: dict[str, Any] = {"output.value": _stringify(output)}
        if error is not None:
            attrs["error"] = True
            attrs["error.type"] = type(error).__name__
            attrs["error.message"] = str(error)
        self._end(attrs)

    # ── LLM (generation) ────────────────────────────────────────────────

    @safe("crewai.on_llm_start")
    def on_llm_start(
        self,
        model: str,
        prompt: Any,
        *,
        provider: str | None = None,
    ) -> None:
        self._start(
            "crewai.llm",
            {
                "putsch.kind": "generation",
                "gen_ai.system": provider or "litellm",
                "gen_ai.request.model": model,
                "input.value": _stringify(prompt),
            },
        )

    @safe("crewai.on_llm_end")
    def on_llm_end(
        self,
        response: Any,
        *,
        model: str | None = None,
        input_tokens: int = 0,
        output_tokens: int = 0,
        cache_hit: bool = False,
    ) -> None:
        cost = self._cost.eur(
            model or "",
            input_tokens=input_tokens,
            output_tokens=output_tokens,
        )
        attrs: dict[str, Any] = {
            "output.value": _stringify(response),
            "gen_ai.usage.input_tokens": input_tokens,
            "gen_ai.usage.output_tokens": output_tokens,
            "gen_ai.response.model": model or "",
            "putsch.cache_hit": cache_hit,
        }
        if cost is not None:
            attrs["gen_ai.usage.cost_eur"] = cost
        else:
            attrs["putsch.cost_unknown"] = True
        self._end(attrs)


def _stringify(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, str):
        return value
    try:
        return str(value)
    except Exception:
        return f"<unstringifiable {type(value).__name__}>"


__all__ = ["PutschCrewAITracer"]
