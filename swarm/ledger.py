"""Magentic-One Task and Progress ledger types.

The Orchestrator runs two loops.

The outer loop owns the Task Ledger: facts the orchestrator has verified,
guesses it has not, and the ordered plan that turns the request into work
for the specialists. The ledger is rebuilt (a "replan") whenever the inner
loop reports the swarm is stalled.

The inner loop owns the Progress Ledger: a per-step self-reflection that
decides whether the request is satisfied, whether the last step actually
moved the plan forward, who to dispatch next, and what to ask them.
"""

from __future__ import annotations

from typing import List, Optional

from pydantic import BaseModel, Field


class TaskLedger(BaseModel):
    """Outer-loop ledger.

    `revision` tracks how many times the orchestrator has rebuilt the plan;
    the graph uses it to stop replanning past a configured ceiling.
    """

    task: str = Field(..., description="The original user request, restated.")
    facts: List[str] = Field(
        default_factory=list,
        description="Information the orchestrator has verified.",
    )
    guesses: List[str] = Field(
        default_factory=list,
        description="Plausible assumptions the orchestrator has not yet verified.",
    )
    plan: List[str] = Field(
        default_factory=list,
        description="Ordered steps toward completing the task; each names a worker.",
    )
    revision: int = Field(
        0, description="Number of times the ledger has been replanned."
    )


class ProgressLedger(BaseModel):
    """Inner-loop ledger.

    A worker is dispatched when `is_request_satisfied` is False and a
    `next_speaker` plus `instruction_or_question` are populated. When
    satisfied, `final_answer` carries the answer for the user.
    """

    is_request_satisfied: bool = Field(
        ..., description="True if the task is fully addressed."
    )
    is_in_loop: bool = Field(
        ..., description="True if the last steps are repeating without progress."
    )
    is_progress_being_made: bool = Field(
        ..., description="True if the most recent step advanced the plan."
    )
    next_speaker: Optional[str] = Field(
        None, description="Name of the worker the orchestrator wants to dispatch."
    )
    instruction_or_question: Optional[str] = Field(
        None,
        description=(
            "Complete message to send to next_speaker. The worker does not see "
            "prior conversation, so this string must carry any needed context."
        ),
    )
    final_answer: Optional[str] = Field(
        None, description="Answer to return to the user when satisfied."
    )
    reasoning: str = Field(
        "", description="One-paragraph rationale supporting the above fields."
    )
