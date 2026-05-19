"""Magentic-One pattern, implemented on LangGraph.

Swarm Coordination for the Putsch AI deployment (Prompt 3 of 6).

Public surface:
  - TaskLedger, ProgressLedger : typed state for the outer/inner loops
  - Worker, WorkerRegistry     : pluggable specialist agents
  - ChatModel, ModelRouter     : per-role model assignment
  - putsch_routing             : the model assignment for the Putsch deployment
  - Orchestrator               : the supervisor running both loops
  - build_putsch_registry      : the seven specialists for the Putsch deployment
  - build_graph                : LangGraph wiring (requires `langgraph` installed)
  - SwarmState                 : the LangGraph state shape
"""

from .ledger import ProgressLedger, TaskLedger
from .models import ChatModel, ModelRouter, putsch_routing
from .orchestrator import Orchestrator
from .specialists import build_putsch_registry
from .state import SwarmState
from .workers import Worker, WorkerRegistry

try:
    from .graph import build_graph
except ImportError:  # langgraph is an optional install for non-graph use
    build_graph = None  # type: ignore[assignment]

__all__ = [
    "TaskLedger",
    "ProgressLedger",
    "Worker",
    "WorkerRegistry",
    "ChatModel",
    "ModelRouter",
    "putsch_routing",
    "Orchestrator",
    "SwarmState",
    "build_putsch_registry",
    "build_graph",
]
