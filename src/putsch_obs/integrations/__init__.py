"""Framework integrations.

Each module here wires a single framework (CrewAI, LangGraph, DSPy,
LiteLLM, Docling, Zep+Graphiti) into the OTel + Langfuse pipeline. The
SDK calls ``init()`` once; calling ``install()`` on each integration after
that turns the per-framework tracing on.

All integrations are best-effort: if the framework is not installed, the
import is a no-op and ``install()`` raises a clear ``ImportError`` that the
operator can resolve.
"""

from __future__ import annotations

__all__ = [
    "crewai",
    "docling",
    "dspy",
    "langgraph",
    "litellm",
    "zep_graphiti",
]
