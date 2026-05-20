"""LangGraph state shape for the Magentic-One swarm.

Kept as a TypedDict so LangGraph's default merge semantics apply: each node
returns the keys it modifies, and unmentioned keys carry forward.
"""

from __future__ import annotations

from typing import List, Optional, Tuple, TypedDict

from .ledger import ProgressLedger, TaskLedger


TranscriptEntry = Tuple[str, str]
"""(speaker_name, content). Speakers are 'orchestrator' or a worker name."""


class SwarmState(TypedDict, total=False):
    task: str
    task_ledger: Optional[TaskLedger]
    progress_ledger: Optional[ProgressLedger]
    transcript: List[TranscriptEntry]
    stall_count: int
    replan_count: int
    final_answer: Optional[str]
