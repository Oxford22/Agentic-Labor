"""Zep + Graphiti memory layer instrumentation.

Zep is the agentic memory store (sessions, messages); Graphiti is the
temporal knowledge graph (entities, edges with validity windows).

We instrument both client surfaces:

* ``zep_cloud.Zep.memory.add / get / search`` → ``memory.write`` /
  ``memory.read`` spans, with ``zep.episode_count`` attribute
* ``graphiti_core.Graphiti.search`` / ``add_episode`` → graph spans with
  ``graphiti.traversal_depth``, ``graphiti.edge_count``,
  ``graphiti.temporal.valid_from``, ``graphiti.temporal.valid_to``

If neither library is installed, ``install()`` is a no-op.
"""

from __future__ import annotations

import contextlib
import threading
from typing import Any, Callable

from opentelemetry import trace as otel_trace

from putsch_obs.instrumentation import get_tracer, is_initialized
from putsch_obs.integrations._base import StopWatch, safe
from putsch_obs.logging import get_logger

log = get_logger(__name__)

_INSTALLED = False
_LOCK = threading.Lock()


def install() -> None:
    """Monkey-patch the Zep + Graphiti client surfaces. Idempotent."""
    global _INSTALLED
    with _LOCK:
        if _INSTALLED:
            return
        if not is_initialized():
            from putsch_obs.instrumentation import init

            init()
        _install_zep()
        _install_graphiti()
        _INSTALLED = True
        log.info("zep_graphiti.instrumentation_installed")


def _install_zep() -> None:
    try:
        from zep_cloud.client import AsyncMemory, Memory  # type: ignore[import-not-found]
    except ImportError:
        log.info("zep.skipped", reason="zep_cloud not installed")
        return

    tracer = get_tracer("putsch_obs.zep")

    def wrap(cls: type, attr: str, op: str) -> None:
        if not hasattr(cls, attr):
            return
        original: Callable[..., Any] = getattr(cls, attr)

        @safe(f"zep.{attr}")
        def traced(self: Any, *args: Any, **kwargs: Any) -> Any:
            with tracer.start_as_current_span(
                f"zep.{op}", kind=otel_trace.SpanKind.CLIENT
            ) as sp:
                sp.set_attribute("putsch.kind", "memory")
                sp.set_attribute("zep.op", op)
                watch = StopWatch()
                try:
                    result = original(self, *args, **kwargs)
                except Exception as exc:
                    sp.record_exception(exc)
                    sp.set_status(otel_trace.Status(otel_trace.StatusCode.ERROR, str(exc)))
                    sp.set_attribute("putsch.latency_ms", watch.elapsed_ms())
                    raise
                sp.set_attribute("putsch.latency_ms", watch.elapsed_ms())
                with contextlib.suppress(Exception):
                    sp.set_attribute("zep.episode_count", _episode_count(result))
                return result

        setattr(cls, attr, traced)

    for cls in (Memory, AsyncMemory):
        for method_name, op in (("add", "write"), ("get", "read"), ("search", "search")):
            wrap(cls, method_name, op)


def _install_graphiti() -> None:
    try:
        from graphiti_core import Graphiti  # type: ignore[import-not-found]
    except ImportError:
        log.info("graphiti.skipped", reason="graphiti_core not installed")
        return

    tracer = get_tracer("putsch_obs.graphiti")

    def wrap(method_name: str, op: str) -> None:
        if not hasattr(Graphiti, method_name):
            return
        original: Callable[..., Any] = getattr(Graphiti, method_name)

        @safe(f"graphiti.{method_name}")
        async def traced(self: Any, *args: Any, **kwargs: Any) -> Any:
            with tracer.start_as_current_span(
                f"graphiti.{op}", kind=otel_trace.SpanKind.CLIENT
            ) as sp:
                sp.set_attribute("putsch.kind", "memory")
                sp.set_attribute("graphiti.op", op)
                watch = StopWatch()
                try:
                    result = await original(self, *args, **kwargs)
                except Exception as exc:
                    sp.record_exception(exc)
                    sp.set_status(otel_trace.Status(otel_trace.StatusCode.ERROR, str(exc)))
                    sp.set_attribute("putsch.latency_ms", watch.elapsed_ms())
                    raise
                sp.set_attribute("putsch.latency_ms", watch.elapsed_ms())
                _annotate_graph(sp, result)
                return result

        setattr(Graphiti, method_name, traced)

    wrap("search", "search")
    wrap("add_episode", "add_episode")
    wrap("get_nodes_by_query", "node_query")


def _episode_count(result: Any) -> int:
    if result is None:
        return 0
    if hasattr(result, "messages"):
        return len(getattr(result, "messages", []) or [])
    if isinstance(result, dict) and "messages" in result:
        return len(result["messages"])
    if isinstance(result, list):
        return len(result)
    return 0


def _annotate_graph(sp: Any, result: Any) -> None:
    with contextlib.suppress(Exception):
        edges = getattr(result, "edges", None)
        if edges is not None:
            sp.set_attribute("graphiti.edge_count", len(edges))
        nodes = getattr(result, "nodes", None)
        if nodes is not None:
            sp.set_attribute("graphiti.node_count", len(nodes))
        depth = getattr(result, "traversal_depth", None)
        if depth is not None:
            sp.set_attribute("graphiti.traversal_depth", int(depth))
        valid_from = getattr(result, "valid_from", None)
        valid_to = getattr(result, "valid_to", None)
        if valid_from is not None:
            sp.set_attribute("graphiti.temporal.valid_from", str(valid_from))
        if valid_to is not None:
            sp.set_attribute("graphiti.temporal.valid_to", str(valid_to))


__all__ = ["install"]
