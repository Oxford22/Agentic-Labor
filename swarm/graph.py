"""LangGraph build for the Magentic-One swarm.

Nodes:
  init       -- build the initial Task Ledger
  progress   -- inner loop: update the Progress Ledger
  dispatch   -- invoke the chosen worker
  replan     -- outer loop on stall
  finalize   -- synthesize the final answer

Edges out of `progress` are conditional on `Orchestrator.route_after_progress`.
"""

from __future__ import annotations

from typing import Any

from .orchestrator import Orchestrator
from .state import SwarmState


def build_graph(orchestrator: Orchestrator) -> Any:
    from langgraph.graph import END, StateGraph

    graph = StateGraph(SwarmState)

    graph.add_node("init", orchestrator.init_task_ledger)
    graph.add_node("progress", orchestrator.update_progress_ledger)
    graph.add_node("dispatch", orchestrator.dispatch_worker)
    graph.add_node("replan", orchestrator.replan_task_ledger)
    graph.add_node("finalize", orchestrator.synthesize_final)

    graph.set_entry_point("init")
    graph.add_edge("init", "progress")
    graph.add_edge("dispatch", "progress")
    graph.add_edge("replan", "progress")
    graph.add_edge("finalize", END)

    graph.add_conditional_edges(
        "progress",
        orchestrator.route_after_progress,
        {"dispatch": "dispatch", "replan": "replan", "finalize": "finalize"},
    )

    return graph.compile()
