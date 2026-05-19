"""Worker specialists invoked by the Orchestrator.

A Worker wraps a chat model with a domain-specific system prompt. Workers
are stateless: the Orchestrator owns conversation history and embeds any
context the worker needs directly in the instruction.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Dict, List

from .models import ModelRouter


@dataclass
class Worker:
    name: str
    description: str
    system_prompt: str
    role: str = ""

    def __post_init__(self) -> None:
        if not self.role:
            self.role = self.name

    def invoke(self, router: ModelRouter, instruction: str) -> str:
        model = router.for_role(self.role)
        messages = [
            {"role": "system", "content": self.system_prompt},
            {"role": "user", "content": instruction},
        ]
        return model.invoke(messages)


@dataclass
class WorkerRegistry:
    workers: Dict[str, Worker] = field(default_factory=dict)

    def register(self, worker: Worker) -> None:
        self.workers[worker.name] = worker

    def get(self, name: str) -> Worker:
        if name not in self.workers:
            raise KeyError(f"Unknown worker: {name}")
        return self.workers[name]

    def has(self, name: str) -> bool:
        return name in self.workers

    def names(self) -> List[str]:
        return list(self.workers.keys())

    def manifest(self) -> str:
        return "\n".join(
            f"- {w.name}: {w.description}" for w in self.workers.values()
        )
