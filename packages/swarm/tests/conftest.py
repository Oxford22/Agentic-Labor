"""Shared test fixtures.

`StubModel` lets us drive the orchestrator with canned JSON responses so
every routing decision is deterministic and the LLM round-trip is
sidestepped.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Callable, Dict, List

import pytest

from swarm.models import ModelRouter
from swarm.orchestrator import Orchestrator
from swarm.workers import Worker, WorkerRegistry


@dataclass
class StubModel:
    """Records calls; replies from a list of canned strings."""

    responses: List[str] = field(default_factory=list)
    received: List[List[dict]] = field(default_factory=list)

    def invoke(self, messages):
        self.received.append(messages)
        if not self.responses:
            raise RuntimeError(
                f"StubModel out of canned responses (received {len(self.received)} calls)"
            )
        return self.responses.pop(0)


def make_stub_router(scripts: Dict[str, List[str]]) -> ModelRouter:
    """Build a ModelRouter whose factory hands out per-model StubModels."""

    cache: Dict[str, StubModel] = {}

    def factory(model_id: str) -> StubModel:
        if model_id not in cache:
            cache[model_id] = StubModel(responses=list(scripts.get(model_id, [])))
        return cache[model_id]

    router = ModelRouter(factory=factory, default_model="orch")
    router.register("orchestrator", "orch")
    router.register("procurement", "proc")
    router.register("finance", "fin")
    return router


def make_registry() -> WorkerRegistry:
    registry = WorkerRegistry()
    registry.register(Worker(
        name="procurement",
        description="reads POs",
        system_prompt="You are procurement.",
    ))
    registry.register(Worker(
        name="finance",
        description="reads AP",
        system_prompt="You are finance.",
    ))
    return registry


@pytest.fixture
def stub_factory():
    return make_stub_router


@pytest.fixture
def registry():
    return make_registry()


@pytest.fixture
def orchestrator_factory(registry):
    def _build(scripts: Dict[str, List[str]], **kwargs) -> Orchestrator:
        router = make_stub_router(scripts)
        return Orchestrator(router=router, workers=registry, **kwargs)

    return _build
