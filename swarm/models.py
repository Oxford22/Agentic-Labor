"""Per-role model routing.

Magentic-One's design separates orchestrator reasoning (GPT-4o-class) from
worker execution (smaller, specialised models). This module owns the
mapping between role names and model identifiers, with a pluggable factory
so the Prompt-1 orchestration module can inject its own model gateway
without this module taking a hard dependency on it.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Callable, Dict, List, Protocol


class ChatModel(Protocol):
    """Minimal chat-model interface the orchestrator and workers depend on.

    `messages` is a list of `{"role": "system"|"user"|"assistant", "content": str}`
    dicts. Implementations are responsible for converting to whatever shape
    their underlying SDK expects.
    """

    def invoke(self, messages: List[dict]) -> str: ...


ModelFactory = Callable[[str], ChatModel]


@dataclass
class ModelRouter:
    """Maps roles to model identifiers; instantiates via a pluggable factory."""

    factory: ModelFactory
    default_model: str
    role_models: Dict[str, str] = field(default_factory=dict)
    _cache: Dict[str, ChatModel] = field(default_factory=dict, init=False, repr=False)

    def register(self, role: str, model_id: str) -> None:
        self.role_models[role] = model_id

    def for_role(self, role: str) -> ChatModel:
        model_id = self.role_models.get(role, self.default_model)
        if model_id not in self._cache:
            self._cache[model_id] = self.factory(model_id)
        return self._cache[model_id]


def putsch_routing(factory: ModelFactory) -> ModelRouter:
    """Model assignment for the Putsch deployment.

    Orchestrator gets a large reasoning model; functional specialists get a
    small general model; execution specialists get the model best suited to
    their narrow task (code, document extraction, DATEV booking codes).
    """

    router = ModelRouter(factory=factory, default_model="mistral-large-2")
    router.register("orchestrator", "mistral-large-2")
    router.register("procurement", "mistral-small")
    router.register("finance", "mistral-small")
    router.register("logistics", "mistral-small")
    router.register("master_data", "mistral-small")
    router.register("sap_coder", "qwen2.5-coder-32b")
    router.register("docling", "granite-docling-3b")
    router.register("datev", "putsch-datev-finetune")
    return router
