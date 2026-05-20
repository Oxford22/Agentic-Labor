"""Crew abstraction and the trust-aware NodeAdapter.

A Crew implements `run(task, context)` and returns a `CrewOutput` with a
human-readable summary plus structured data. Crews are the unit at which
the harness composes agentic work.

The NodeAdapter is the trust boundary BETWEEN crews. When crew A's output
flows into crew B, the adapter wraps A's summary in <external_content
source="..."> so B's agents see it as evidence, never as a directive.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

from trust import Source, wrap_external


@dataclass
class CrewOutput:
    """A crew's result.

    `summary` is the short, human-readable string the next crew (or a human)
    will see. `data` is structured detail for downstream consumers that
    parse rather than read.
    """

    summary: str
    data: Dict[str, Any] = field(default_factory=dict)


class Crew(ABC):
    """A unit of agentic work with a uniform interface."""

    @property
    @abstractmethod
    def name(self) -> str: ...

    @abstractmethod
    def run(self, task: str, context: Optional[Dict[str, Any]] = None) -> CrewOutput:
        """Run the crew. `context` carries wrapped output from prior crews."""


class NodeAdapter:
    """Wraps a Crew as a trust-aware node.

    On call, it builds the `context` for the wrapped crew by enveloping
    every prior crew's summary as <external_content> - so upstream outputs
    cross the trust boundary as data, never as directive material.

    The adapter is callable in two shapes:
      adapter(state: dict) -> dict        # LangGraph-style node
      adapter.run(task, prior=[...])      # direct call inside a Pipeline
    """

    def __init__(self, crew: Crew, source: Source = Source.WORKER) -> None:
        self._crew = crew
        self._source = source

    @property
    def name(self) -> str:
        return self._crew.name

    def _wrap_prior(self, prior: List[Dict[str, Any]]) -> str:
        if not prior:
            return ""
        parts = []
        for entry in prior:
            label = entry.get("name", "unknown")
            summary = entry.get("summary", "")
            parts.append(f"[{label}]\n{wrap_external(self._source, summary)}")
        return "\n\n".join(parts)

    def run(
        self,
        task: str,
        prior: Optional[List[Dict[str, Any]]] = None,
    ) -> CrewOutput:
        context = {"prior": self._wrap_prior(prior or [])}
        return self._crew.run(task=task, context=context)

    def __call__(self, state: Dict[str, Any]) -> Dict[str, Any]:
        prior = state.get("crew_outputs", [])
        output = self.run(task=state["task"], prior=prior)
        new_entry = {
            "name": self._crew.name,
            "summary": output.summary,
            "data": output.data,
        }
        return {"crew_outputs": list(prior) + [new_entry]}
