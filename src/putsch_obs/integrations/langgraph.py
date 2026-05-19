"""LangGraph instrumentation.

The LangGraph swarm (Magentic-One pattern) is the orchestrator for the
multi-agent flows. We need visibility into:

* **Node entry/exit** — each node is a span with the node name
* **State mutations** — recorded as a diff payload per node exit
* **``interrupt()`` points** — recorded as a span event, so we can compute
  Mahnverfahren-style stall metrics
* **Checkpointer activity** — every checkpoint write is an event, with the
  thread id and step number
* **Edge decisions** — conditional edges record the chosen next-node as a
  ``putsch.routing.decision`` attribute on the source node's span

LangGraph exposes these via a ``CallbackManager`` or directly through
``Runnable.astream(... config={"callbacks": [...]})``. We provide a callback
handler compatible with ``langchain_core.callbacks.BaseCallbackHandler``.
"""

from __future__ import annotations

from typing import Any
from uuid import UUID

from opentelemetry import trace as otel_trace
from opentelemetry.trace import Span

from putsch_obs.instrumentation import get_tracer, is_initialized
from putsch_obs.integrations._base import StopWatch, safe
from putsch_obs.logging import get_logger

log = get_logger(__name__)


class PutschLangGraphTracer:
    """LangChain-compatible callback handler for LangGraph runtimes.

    Use:

        from langgraph.graph import StateGraph
        from putsch_obs.integrations.langgraph import PutschLangGraphTracer

        graph = StateGraph(...).compile()
        result = await graph.ainvoke(
            input,
            config={"callbacks": [PutschLangGraphTracer()]},
        )
    """

    name = "putsch_langgraph_tracer"

    def __init__(self) -> None:
        if not is_initialized():
            from putsch_obs.instrumentation import init

            init()
        self._tracer = get_tracer("putsch_obs.langgraph")
        # Map run_id (UUID) → (span, stopwatch, last_state)
        self._runs: dict[UUID, tuple[Span, StopWatch, dict[str, Any]]] = {}

    # ── lifecycle helpers ───────────────────────────────────────────────

    def _open(
        self,
        run_id: UUID,
        name: str,
        attrs: dict[str, Any],
        last_state: dict[str, Any] | None = None,
    ) -> None:
        sp = self._tracer.start_span(name, kind=otel_trace.SpanKind.INTERNAL)
        for k, v in attrs.items():
            sp.set_attribute(k, _attrify(v))
        self._runs[run_id] = (sp, StopWatch(), dict(last_state or {}))

    def _close(self, run_id: UUID, attrs: dict[str, Any] | None = None) -> None:
        rec = self._runs.pop(run_id, None)
        if rec is None:
            return
        sp, watch, _ = rec
        sp.set_attribute("putsch.latency_ms", watch.elapsed_ms())
        if attrs:
            for k, v in attrs.items():
                sp.set_attribute(k, _attrify(v))
        sp.end()

    # ── chain (= node) hooks ────────────────────────────────────────────

    @safe("langgraph.on_chain_start")
    def on_chain_start(
        self,
        serialized: dict[str, Any] | None,
        inputs: dict[str, Any],
        *,
        run_id: UUID,
        parent_run_id: UUID | None = None,
        name: str | None = None,
        tags: list[str] | None = None,
        metadata: dict[str, Any] | None = None,
        **_: Any,
    ) -> None:
        node_name = name or (serialized or {}).get("name") or "node"
        attrs: dict[str, Any] = {
            "putsch.kind": "node",
            "langgraph.node.name": node_name,
            "langgraph.node.tags": ",".join(tags or []),
            "input.value": _stringify(inputs),
        }
        if metadata:
            thread = metadata.get("thread_id")
            checkpoint = metadata.get("checkpoint_ns")
            step = metadata.get("step")
            if thread is not None:
                attrs["langgraph.thread_id"] = str(thread)
            if checkpoint is not None:
                attrs["langgraph.checkpoint_ns"] = str(checkpoint)
            if step is not None:
                attrs["langgraph.step"] = int(step) if str(step).isdigit() else str(step)
        self._open(run_id, f"langgraph.node.{node_name}", attrs, last_state=inputs)

    @safe("langgraph.on_chain_end")
    def on_chain_end(
        self,
        outputs: dict[str, Any],
        *,
        run_id: UUID,
        **_: Any,
    ) -> None:
        # Compute the state diff vs. on_chain_start's inputs.
        rec = self._runs.get(run_id)
        diff: dict[str, Any] = {}
        if rec is not None:
            _, _, prev = rec
            for k, v in (outputs or {}).items():
                if prev.get(k) != v:
                    diff[k] = {"before": _stringify(prev.get(k)), "after": _stringify(v)}
        self._close(
            run_id,
            {
                "output.value": _stringify(outputs),
                "langgraph.state.diff": _stringify(diff) if diff else "",
                "langgraph.state.changed_keys": ",".join(diff.keys()),
            },
        )

    @safe("langgraph.on_chain_error")
    def on_chain_error(
        self,
        error: BaseException,
        *,
        run_id: UUID,
        **_: Any,
    ) -> None:
        rec = self._runs.get(run_id)
        if rec is not None:
            sp, _, _ = rec
            sp.record_exception(error)
            sp.set_status(otel_trace.Status(otel_trace.StatusCode.ERROR, str(error)))
        self._close(
            run_id,
            {
                "error": True,
                "error.type": type(error).__name__,
                "error.message": str(error),
            },
        )

    # ── interrupt / checkpoint events ───────────────────────────────────

    @safe("langgraph.on_interrupt")
    def on_interrupt(
        self,
        run_id: UUID,
        *,
        reason: str,
        payload: Any = None,
    ) -> None:
        """Call from your node when you invoke ``interrupt()``.

        LangGraph doesn't currently surface interrupts to the callback API
        in a stable form, so the swarm-orchestrator wrapper invokes this
        explicitly.
        """
        rec = self._runs.get(run_id)
        if rec is None:
            return
        sp, _, _ = rec
        sp.add_event(
            "langgraph.interrupt",
            attributes={
                "interrupt.reason": reason,
                "interrupt.payload": _stringify(payload),
            },
        )
        sp.set_attribute("langgraph.interrupted", True)

    @safe("langgraph.on_checkpoint")
    def on_checkpoint(
        self,
        run_id: UUID,
        *,
        thread_id: str,
        checkpoint_id: str,
        step: int,
    ) -> None:
        rec = self._runs.get(run_id)
        if rec is None:
            return
        sp, _, _ = rec
        sp.add_event(
            "langgraph.checkpoint",
            attributes={
                "checkpoint.id": checkpoint_id,
                "checkpoint.thread_id": thread_id,
                "checkpoint.step": step,
            },
        )

    @safe("langgraph.on_edge_decision")
    def on_edge_decision(
        self,
        run_id: UUID,
        *,
        next_node: str,
        justification: str = "",
    ) -> None:
        rec = self._runs.get(run_id)
        if rec is None:
            return
        sp, _, _ = rec
        sp.set_attribute("putsch.routing.decision", next_node)
        if justification:
            sp.set_attribute("putsch.routing.justification", justification)

    # ── tool / llm passthroughs (matched to LangChain API) ──────────────

    @safe("langgraph.on_tool_start")
    def on_tool_start(
        self,
        serialized: dict[str, Any] | None,
        input_str: str,
        *,
        run_id: UUID,
        name: str | None = None,
        **_: Any,
    ) -> None:
        tool_name = name or (serialized or {}).get("name") or "tool"
        self._open(
            run_id,
            f"langgraph.tool.{tool_name}",
            {
                "putsch.kind": "tool",
                "tool.name": tool_name,
                "input.value": _stringify(input_str),
            },
        )

    @safe("langgraph.on_tool_end")
    def on_tool_end(self, output: str, *, run_id: UUID, **_: Any) -> None:
        self._close(run_id, {"output.value": _stringify(output)})


def _attrify(v: Any) -> Any:
    if isinstance(v, (str, int, float, bool)) or v is None:
        return v
    return _stringify(v)


def _stringify(v: Any) -> str:
    if v is None:
        return ""
    if isinstance(v, str):
        return v
    try:
        import json

        return json.dumps(v, default=str, ensure_ascii=False)
    except Exception:
        return str(v)


__all__ = ["PutschLangGraphTracer"]
