"""Magentic-One Orchestrator, implemented as LangGraph nodes.

The Orchestrator runs two loops:

  outer loop  -- maintain the Task Ledger (facts, guesses, plan)
  inner loop  -- maintain the Progress Ledger, then dispatch a worker
                 or finish.

`init_task_ledger` and `replan_task_ledger` are the outer-loop entry
points. `update_progress_ledger` is the inner-loop step. `dispatch_worker`
invokes the chosen specialist and appends the result to the transcript.
`route_after_progress` is the conditional edge the graph evaluates after
each inner-loop step.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import Iterable

from trust import ORCHESTRATOR_HEADER, Source, wrap_external

from .ledger import ProgressLedger, TaskLedger
from .models import ModelRouter
from .prompts import (
    FINAL_ANSWER_PROMPT,
    PROGRESS_LEDGER_PROMPT,
    TASK_LEDGER_INIT_PROMPT,
    TASK_LEDGER_REPLAN_PROMPT,
)
from .state import SwarmState, TranscriptEntry
from .workers import WorkerRegistry


@dataclass
class Orchestrator:
    """The supervisor agent.

    `max_stalls_before_replan` controls how many consecutive non-progress
    inner-loop steps trigger an outer-loop replan. `max_replans` caps how
    many times the outer loop will rebuild the ledger before giving up.
    """

    router: ModelRouter
    workers: WorkerRegistry
    role: str = "orchestrator"
    max_stalls_before_replan: int = 2
    max_replans: int = 2

    def _model(self):
        return self.router.for_role(self.role)

    def _ask_json(self, prompt: str) -> dict:
        raw = self._model().invoke([
            {"role": "system", "content": ORCHESTRATOR_HEADER},
            {"role": "user", "content": prompt},
        ])
        return _coerce_json(raw)

    # ----- outer loop -----

    def init_task_ledger(self, state: SwarmState) -> dict:
        prompt = TASK_LEDGER_INIT_PROMPT.format(
            task=state["task"],
            worker_manifest=self.workers.manifest(),
        )
        data = self._ask_json(prompt)
        ledger = TaskLedger(task=state["task"], **data)
        return {
            "task_ledger": ledger,
            "stall_count": 0,
            "replan_count": 0,
            "transcript": list(state.get("transcript", [])),
        }

    def replan_task_ledger(self, state: SwarmState) -> dict:
        current = state.get("task_ledger")
        prompt = TASK_LEDGER_REPLAN_PROMPT.format(
            task=state["task"],
            worker_manifest=self.workers.manifest(),
            current_facts="\n".join(current.facts) if current else "(none)",
            current_plan="\n".join(current.plan) if current else "(none)",
            transcript=_format_transcript(state.get("transcript", [])),
        )
        data = self._ask_json(prompt)
        revision = (current.revision + 1) if current else 1
        ledger = TaskLedger(task=state["task"], revision=revision, **data)
        return {
            "task_ledger": ledger,
            "stall_count": 0,
            "replan_count": state.get("replan_count", 0) + 1,
        }

    # ----- inner loop -----

    def update_progress_ledger(self, state: SwarmState) -> dict:
        ledger = state["task_ledger"]
        prompt = PROGRESS_LEDGER_PROMPT.format(
            task=state["task"],
            worker_manifest=self.workers.manifest(),
            plan="\n".join(ledger.plan),
            transcript=_format_transcript(state.get("transcript", [])),
        )
        data = self._ask_json(prompt)
        progress = ProgressLedger(**data)

        stall = state.get("stall_count", 0)
        if progress.is_in_loop or not progress.is_progress_being_made:
            stall += 1
        else:
            stall = 0
        return {"progress_ledger": progress, "stall_count": stall}

    # ----- dispatch -----

    def dispatch_worker(self, state: SwarmState) -> dict:
        progress = state["progress_ledger"]
        if not progress.next_speaker or not progress.instruction_or_question:
            return {}
        worker = self.workers.get(progress.next_speaker)
        reply = worker.invoke(self.router, progress.instruction_or_question)
        transcript = list(state.get("transcript", []))
        transcript.append((
            "orchestrator",
            f"-> {worker.name}: {progress.instruction_or_question}",
        ))
        transcript.append((worker.name, reply))
        return {"transcript": transcript}

    # ----- finalize -----

    def synthesize_final(self, state: SwarmState) -> dict:
        progress = state.get("progress_ledger")
        if progress and progress.final_answer:
            return {"final_answer": progress.final_answer}
        prompt = FINAL_ANSWER_PROMPT.format(
            task=state["task"],
            transcript=_format_transcript(state.get("transcript", [])),
        )
        answer = self._model().invoke([
            {"role": "system", "content": ORCHESTRATOR_HEADER},
            {"role": "user", "content": prompt},
        ])
        return {"final_answer": answer.strip()}

    # ----- routing -----

    def route_after_progress(self, state: SwarmState) -> str:
        progress = state["progress_ledger"]
        if progress.is_request_satisfied:
            return "finalize"

        stalled = state.get("stall_count", 0) >= self.max_stalls_before_replan
        has_valid_speaker = bool(
            progress.next_speaker and self.workers.has(progress.next_speaker)
        )
        if not stalled and has_valid_speaker:
            return "dispatch"
        # Either stalled or no usable next speaker - try a replan, unless the
        # budget is exhausted, in which case synthesize what we have.
        if state.get("replan_count", 0) >= self.max_replans:
            return "finalize"
        return "replan"


def _format_transcript(transcript: Iterable[TranscriptEntry]) -> str:
    """Format transcript for inclusion in an orchestrator prompt.

    Worker replies are wrapped in <external_content source="worker"> so that
    a worker reply carrying an injected directive (e.g. an OCR'd invoice
    with adversarial text) cannot redirect the orchestrator's next inner-loop
    decision. The orchestrator's own dispatch messages are not wrapped -
    they come from a trusted source (the orchestrator itself).
    """

    entries = list(transcript)
    if not entries:
        return "(empty)"
    lines = []
    for speaker, content in entries:
        if speaker == "orchestrator":
            lines.append(f"[orchestrator] {content}")
        else:
            wrapped = wrap_external(Source.WORKER, content)
            lines.append(f"[{speaker}] {wrapped}")
    return "\n".join(lines)


_JSON_FENCE_RE = re.compile(r"^```(?:json)?\s*|\s*```$", re.MULTILINE)


def _coerce_json(raw: str) -> dict:
    text = _JSON_FENCE_RE.sub("", raw).strip()
    try:
        return json.loads(text)
    except json.JSONDecodeError as exc:
        raise ValueError(
            f"Orchestrator response was not valid JSON:\n{raw}"
        ) from exc
