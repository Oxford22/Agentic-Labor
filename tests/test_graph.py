"""End-to-end run of the compiled LangGraph with stub models.

Walks through: init -> progress -> dispatch -> progress (satisfied) ->
finalize, then a separate scenario where the orchestrator stalls and
replans before completing.
"""

from __future__ import annotations

import json

from swarm.graph import build_graph
from swarm.models import ModelRouter
from swarm.orchestrator import Orchestrator
from swarm.workers import Worker, WorkerRegistry

from .conftest import StubModel


def _build_router(scripts):
    cache = {}

    def factory(model_id):
        if model_id not in cache:
            cache[model_id] = StubModel(responses=list(scripts.get(model_id, [])))
        return cache[model_id]

    router = ModelRouter(factory=factory, default_model="orch")
    router.register("orchestrator", "orch")
    router.register("procurement", "proc")
    router.register("finance", "fin")
    return router


def _registry():
    r = WorkerRegistry()
    r.register(Worker(
        name="procurement", description="POs", system_prompt="proc"
    ))
    r.register(Worker(
        name="finance", description="AP", system_prompt="fin"
    ))
    return r


def test_graph_happy_path():
    scripts = {
        "orch": [
            json.dumps({
                "facts": ["invoice 1187 overdue"],
                "guesses": [],
                "plan": ["[procurement] verify PO match"],
            }),
            json.dumps({
                "is_request_satisfied": False,
                "is_in_loop": False,
                "is_progress_being_made": True,
                "next_speaker": "procurement",
                "instruction_or_question": "Pull PO 4500017722.",
                "final_answer": None,
                "reasoning": "first step",
            }),
            json.dumps({
                "is_request_satisfied": True,
                "is_in_loop": False,
                "is_progress_being_made": True,
                "next_speaker": None,
                "instruction_or_question": None,
                "final_answer": "PO matches. Proceed with third dunning.",
                "reasoning": "confirmed",
            }),
        ],
        "proc": ["PO 4500017722 verified; total EUR 4,318 matches invoice 1187."],
    }
    router = _build_router(scripts)
    orch = Orchestrator(router=router, workers=_registry())
    graph = build_graph(orch)

    result = graph.invoke({"task": "check invoice 1187", "transcript": []})

    assert result["final_answer"] == "PO matches. Proceed with third dunning."
    assert result["task_ledger"].plan == ["[procurement] verify PO match"]
    assert any(speaker == "procurement" for speaker, _ in result["transcript"])


def test_graph_replan_then_complete():
    """Inner loop stalls twice on procurement, triggers a replan, then finishes via finance."""

    scripts = {
        "orch": [
            # 1) initial plan
            json.dumps({
                "facts": [], "guesses": [],
                "plan": ["[procurement] verify PO match"],
            }),
            # 2) first progress: dispatch procurement
            json.dumps({
                "is_request_satisfied": False,
                "is_in_loop": False,
                "is_progress_being_made": True,
                "next_speaker": "procurement",
                "instruction_or_question": "Pull PO 4500017722.",
                "final_answer": None, "reasoning": "first step",
            }),
            # 3) progress after procurement: stalled (no progress)
            json.dumps({
                "is_request_satisfied": False,
                "is_in_loop": True,
                "is_progress_being_made": False,
                "next_speaker": "procurement",
                "instruction_or_question": "Retry.",
                "final_answer": None, "reasoning": "no PO",
            }),
            # 4) one more stall to reach threshold
            json.dumps({
                "is_request_satisfied": False,
                "is_in_loop": True,
                "is_progress_being_made": False,
                "next_speaker": "procurement",
                "instruction_or_question": "Retry.",
                "final_answer": None, "reasoning": "still stuck",
            }),
            # 5) replan: switch to finance
            json.dumps({
                "facts": ["procurement could not locate PO"],
                "guesses": [],
                "plan": ["[finance] check AP open item by invoice number"],
            }),
            # 6) progress: dispatch finance
            json.dumps({
                "is_request_satisfied": False,
                "is_in_loop": False,
                "is_progress_being_made": True,
                "next_speaker": "finance",
                "instruction_or_question": "Look up invoice 1187 in AP.",
                "final_answer": None, "reasoning": "new angle",
            }),
            # 7) progress: satisfied
            json.dumps({
                "is_request_satisfied": True,
                "is_in_loop": False,
                "is_progress_being_made": True,
                "next_speaker": None,
                "instruction_or_question": None,
                "final_answer": "AP entry confirms posting. Hold dunning.",
                "reasoning": "found via finance",
            }),
        ],
        "proc": ["No PO found.", "Still no PO found."],
        "fin": ["Invoice 1187 posted on 2025-04-02 against vendor DE842791."],
    }

    # max_stalls_before_replan=2 (default) means stall after #3 -> #4 triggers replan
    router = _build_router(scripts)
    orch = Orchestrator(router=router, workers=_registry())
    graph = build_graph(orch)

    result = graph.invoke({"task": "check invoice 1187", "transcript": []})

    assert "Hold dunning" in result["final_answer"]
    assert result["task_ledger"].revision == 1
    assert result["replan_count"] == 1
    # Both specialists actually got called
    speakers = {speaker for speaker, _ in result["transcript"]}
    assert "procurement" in speakers
    assert "finance" in speakers


def test_graph_aborts_after_max_replans():
    """If replans exhaust without success, the graph still terminates via finalize.

    Cycle with max_stalls=1, max_replans=1:
      init -> progress(stall) -> replan -> progress(stall) -> finalize(synthesize)
    """

    stalled = json.dumps({
        "is_request_satisfied": False,
        "is_in_loop": True,
        "is_progress_being_made": False,
        "next_speaker": None,
        "instruction_or_question": None,
        "final_answer": None,
        "reasoning": "stuck",
    })
    replan = json.dumps({"facts": [], "guesses": [], "plan": ["[procurement] retry"]})
    initial = json.dumps({"facts": [], "guesses": [], "plan": ["[procurement] first"]})
    final_msg = "Best-effort answer: unable to resolve invoice 1187."

    scripts = {"orch": [initial, stalled, replan, stalled, final_msg]}
    router = _build_router(scripts)
    orch = Orchestrator(
        router=router,
        workers=_registry(),
        max_stalls_before_replan=1,
        max_replans=1,
    )
    graph = build_graph(orch)

    result = graph.invoke({"task": "check invoice 1187", "transcript": []})

    assert result["replan_count"] == 1
    assert "unable to resolve" in result["final_answer"]


def test_putsch_registry_has_seven_specialists():
    from swarm import build_putsch_registry

    reg = build_putsch_registry()
    assert set(reg.names()) == {
        "procurement", "finance", "logistics", "master_data",
        "sap_coder", "docling", "datev",
    }


def test_putsch_routing_assigns_distinct_models():
    from swarm import putsch_routing

    seen = []
    router = putsch_routing(factory=lambda mid: seen.append(mid) or _DummyModel())
    # Distinct models per role
    assert router.role_models["orchestrator"] == "mistral-large-2"
    assert router.role_models["sap_coder"] == "qwen2.5-coder-32b"
    assert router.role_models["docling"] == "granite-docling-3b"
    assert router.role_models["datev"] == "putsch-datev-finetune"


class _DummyModel:
    def invoke(self, messages):
        return ""
