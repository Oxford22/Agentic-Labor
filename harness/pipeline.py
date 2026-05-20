"""Sequential composition of crews.

Each crew receives the original task plus a wrapped view of every prior
crew's output. The wrapping happens inside NodeAdapter, so the Pipeline
itself only sequences calls.

For a non-sequential graph (branching, parallel fan-out, joins), wrap each
crew with NodeAdapter manually and assemble the LangGraph directly. The
adapter's `__call__(state)` shape matches a LangGraph node.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, List

from crews.base import Crew, NodeAdapter


@dataclass
class PipelineResult:
    task: str
    crew_outputs: List[Dict[str, Any]] = field(default_factory=list)

    @property
    def final_summary(self) -> str:
        if not self.crew_outputs:
            return "(no crews ran)"
        return self.crew_outputs[-1]["summary"]


class Pipeline:
    """A linear chain of crews. Each step sees prior outputs as data."""

    def __init__(self, crews: List[Crew]) -> None:
        if not crews:
            raise ValueError("Pipeline requires at least one crew.")
        self._adapters = [NodeAdapter(c) for c in crews]

    @property
    def crew_names(self) -> List[str]:
        return [a.name for a in self._adapters]

    def run(self, task: str) -> PipelineResult:
        result = PipelineResult(task=task)
        for adapter in self._adapters:
            update = adapter({"task": task, "crew_outputs": result.crew_outputs})
            result.crew_outputs = update["crew_outputs"]
        return result
