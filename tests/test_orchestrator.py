"""Orchestrator loop logic, driven by canned JSON from StubModel.

Each test isolates one routing decision in the Magentic-One inner loop, so
a regression in stall counting or replan throttling will fail a focused
assertion rather than the whole graph.
"""

from __future__ import annotations

import json

from swarm.ledger import ProgressLedger, TaskLedger


def _progress(**overrides) -> dict:
    base = {
        "is_request_satisfied": False,
        "is_in_loop": False,
        "is_progress_being_made": True,
        "next_speaker": "procurement",
        "instruction_or_question": "Pull PO 4500017722.",
        "final_answer": None,
        "reasoning": "advance plan",
    }
    base.update(overrides)
    return base


def test_init_task_ledger_parses_json(orchestrator_factory):
    orch = orchestrator_factory({
        "orch": [json.dumps({
            "facts": ["invoice 1187 exists"],
            "guesses": ["supplier is German"],
            "plan": ["[procurement] verify PO match"],
        })],
    })
    out = orch.init_task_ledger({"task": "check invoice"})
    assert out["task_ledger"].facts == ["invoice 1187 exists"]
    assert out["task_ledger"].plan[0].startswith("[procurement]")
    assert out["stall_count"] == 0
    assert out["replan_count"] == 0


def test_init_strips_markdown_fence(orchestrator_factory):
    payload = {"facts": [], "guesses": [], "plan": ["[procurement] step"]}
    orch = orchestrator_factory({
        "orch": ["```json\n" + json.dumps(payload) + "\n```"],
    })
    out = orch.init_task_ledger({"task": "x"})
    assert out["task_ledger"].plan == ["[procurement] step"]


def test_progress_resets_stall_on_progress(orchestrator_factory):
    orch = orchestrator_factory({"orch": [json.dumps(_progress())]})
    out = orch.update_progress_ledger({
        "task": "x",
        "task_ledger": TaskLedger(task="x", plan=["one"]),
        "stall_count": 1,
        "transcript": [],
    })
    assert out["stall_count"] == 0
    assert out["progress_ledger"].next_speaker == "procurement"


def test_progress_increments_stall_when_looping(orchestrator_factory):
    orch = orchestrator_factory({
        "orch": [json.dumps(_progress(is_in_loop=True, is_progress_being_made=False))],
    })
    out = orch.update_progress_ledger({
        "task": "x",
        "task_ledger": TaskLedger(task="x", plan=["one"]),
        "stall_count": 1,
        "transcript": [],
    })
    assert out["stall_count"] == 2


def test_progress_increments_stall_when_no_progress_even_without_loop(orchestrator_factory):
    orch = orchestrator_factory({
        "orch": [json.dumps(_progress(is_in_loop=False, is_progress_being_made=False))],
    })
    out = orch.update_progress_ledger({
        "task": "x",
        "task_ledger": TaskLedger(task="x", plan=["one"]),
        "stall_count": 0,
        "transcript": [],
    })
    assert out["stall_count"] == 1


def test_replan_increments_counter_and_resets_stall(orchestrator_factory):
    orch = orchestrator_factory({
        "orch": [json.dumps({
            "facts": ["learned X"],
            "guesses": [],
            "plan": ["[finance] try a different angle"],
        })],
    })
    out = orch.replan_task_ledger({
        "task": "x",
        "task_ledger": TaskLedger(task="x", revision=0, plan=["[procurement] old"]),
        "stall_count": 2,
        "replan_count": 0,
        "transcript": [("procurement", "no result")],
    })
    assert out["task_ledger"].revision == 1
    assert out["task_ledger"].plan == ["[finance] try a different angle"]
    assert out["stall_count"] == 0
    assert out["replan_count"] == 1


def test_dispatch_appends_two_transcript_entries(orchestrator_factory):
    orch = orchestrator_factory({
        "proc": ["PO 4500017722 verified; total matches invoice."],
    })
    progress = ProgressLedger(
        is_request_satisfied=False,
        is_in_loop=False,
        is_progress_being_made=True,
        next_speaker="procurement",
        instruction_or_question="Pull PO 4500017722.",
        reasoning="",
    )
    out = orch.dispatch_worker({
        "task": "x",
        "progress_ledger": progress,
        "transcript": [],
    })
    assert len(out["transcript"]) == 2
    assert out["transcript"][0][0] == "orchestrator"
    assert out["transcript"][1] == (
        "procurement",
        "PO 4500017722 verified; total matches invoice.",
    )


def test_dispatch_is_noop_when_no_next_speaker(orchestrator_factory):
    orch = orchestrator_factory({})
    progress = ProgressLedger(
        is_request_satisfied=False,
        is_in_loop=False,
        is_progress_being_made=True,
        next_speaker=None,
        instruction_or_question=None,
        reasoning="",
    )
    out = orch.dispatch_worker({
        "task": "x", "progress_ledger": progress, "transcript": [],
    })
    assert out == {}


def test_route_to_dispatch_when_progressing(orchestrator_factory):
    orch = orchestrator_factory({})
    state = {
        "progress_ledger": ProgressLedger(**_progress()),
        "stall_count": 0,
        "replan_count": 0,
    }
    assert orch.route_after_progress(state) == "dispatch"


def test_route_to_replan_when_dispatch_instruction_missing(orchestrator_factory):
    orch = orchestrator_factory({})
    state = {
        "progress_ledger": ProgressLedger(
            **_progress(instruction_or_question=None)
        ),
        "stall_count": 0,
        "replan_count": 0,
    }
    assert orch.route_after_progress(state) == "replan"


def test_route_to_replan_when_stalled(orchestrator_factory):
    orch = orchestrator_factory({})
    state = {
        "progress_ledger": ProgressLedger(
            **_progress(is_in_loop=True, is_progress_being_made=False)
        ),
        "stall_count": 2,
        "replan_count": 0,
    }
    assert orch.route_after_progress(state) == "replan"


def test_route_to_finalize_when_replans_exhausted(orchestrator_factory):
    orch = orchestrator_factory({})
    state = {
        "progress_ledger": ProgressLedger(
            **_progress(is_in_loop=True, is_progress_being_made=False)
        ),
        "stall_count": 2,
        "replan_count": 2,
    }
    assert orch.route_after_progress(state) == "finalize"


def test_route_to_finalize_when_satisfied(orchestrator_factory):
    orch = orchestrator_factory({})
    state = {
        "progress_ledger": ProgressLedger(
            is_request_satisfied=True,
            is_in_loop=False,
            is_progress_being_made=True,
            final_answer="done",
            reasoning="",
        ),
        "stall_count": 0,
        "replan_count": 0,
    }
    assert orch.route_after_progress(state) == "finalize"


def test_route_to_replan_when_speaker_unknown(orchestrator_factory):
    orch = orchestrator_factory({})
    state = {
        "progress_ledger": ProgressLedger(
            **_progress(next_speaker="hallucinated_worker")
        ),
        "stall_count": 0,
        "replan_count": 0,
    }
    assert orch.route_after_progress(state) == "replan"


def test_synthesize_final_uses_progress_answer_when_present(orchestrator_factory):
    orch = orchestrator_factory({})
    progress = ProgressLedger(
        is_request_satisfied=True,
        is_in_loop=False,
        is_progress_being_made=True,
        final_answer="invoice cleared",
        reasoning="",
    )
    out = orch.synthesize_final({
        "task": "x", "progress_ledger": progress, "transcript": []
    })
    assert out["final_answer"] == "invoice cleared"


def test_synthesize_final_falls_back_to_model_when_missing(orchestrator_factory):
    orch = orchestrator_factory({"orch": ["A short synthesized answer."]})
    out = orch.synthesize_final({
        "task": "x",
        "progress_ledger": None,
        "transcript": [("procurement", "ok")],
    })
    assert out["final_answer"] == "A short synthesized answer."
