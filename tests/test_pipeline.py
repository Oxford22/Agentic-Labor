"""Pipeline tests including an end-to-end Stammdaten + SwarmCrew run with stubs."""

from __future__ import annotations

import json

from crews import StammdatenCrew, SwarmCrew
from crews.base import Crew, CrewOutput
from harness import Pipeline
from swarm import Orchestrator
from swarm.workers import Worker, WorkerRegistry

from .conftest import make_stub_router


def test_pipeline_requires_at_least_one_crew():
    import pytest
    with pytest.raises(ValueError):
        Pipeline([])


def test_pipeline_executes_crews_in_order():
    calls = []

    class Recorder(Crew):
        def __init__(self, name): self._name, self._calls = name, calls
        @property
        def name(self): return self._name
        def run(self, task, context=None):
            self._calls.append(self._name)
            return CrewOutput(summary=f"{self._name}-done")

    pipeline = Pipeline([Recorder("a"), Recorder("b"), Recorder("c")])
    result = pipeline.run("t")
    assert calls == ["a", "b", "c"]
    assert result.final_summary == "c-done"
    assert len(result.crew_outputs) == 3


def test_pipeline_crew_names_exposed():
    pipeline = Pipeline([
        StammdatenCrew(vendors={}),
        StammdatenCrew(vendors={}, name="stammdaten_b"),
    ])
    assert pipeline.crew_names == ["stammdaten", "stammdaten_b"]


def test_end_to_end_stammdaten_to_swarm_with_stubs():
    """Run a real pipeline: StammdatenCrew -> SwarmCrew.

    Stub models for the swarm walk: init -> progress(dispatch) ->
    progress(satisfied) -> finalize. The point of this test is that the
    pipeline plumbing actually moves data between the crews and that the
    Stammdaten finding arrives in the orchestrator's first prompt as
    wrapped <external_content>.
    """

    scripts = {
        "orch": [
            json.dumps({
                "facts": ["vendor record received from stammdaten"],
                "guesses": [],
                "plan": ["[procurement] confirm PO match"],
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
                "final_answer": "Cleared. Proceed with DATEV booking 4400 -> 3100.",
                "reasoning": "done",
            }),
        ],
        "proc": ["PO 4500017722 confirmed; line total matches invoice 1187."],
    }
    router = make_stub_router(scripts)
    registry = WorkerRegistry()
    registry.register(Worker(
        name="procurement", description="POs", system_prompt="proc",
    ))
    orchestrator = Orchestrator(router=router, workers=registry)

    vendors = {"DE842791": {"name": "MusterLieferant GmbH", "iban": "DE.."}}
    pipeline = Pipeline([
        StammdatenCrew(vendors=vendors),
        SwarmCrew(orchestrator=orchestrator),
    ])

    result = pipeline.run(
        "Eingangsrechnung 2025-04-1187, Lieferant DE842791. Plausibilitaet pruefen."
    )

    assert result.final_summary == "Cleared. Proceed with DATEV booking 4400 -> 3100."
    assert pipeline.crew_names == ["stammdaten", "swarm"]
    assert len(result.crew_outputs) == 2

    # Stammdaten finding was passed into the swarm's first orchestrator prompt
    # as wrapped <external_content>, not as raw text.
    from .conftest import StubModel
    orch_stub: StubModel = router.for_role("orchestrator")  # type: ignore[assignment]
    first_user_msg = orch_stub.received[0][-1]["content"]
    assert "Context from prior crews" in first_user_msg
    assert '<external_content source="worker">' in first_user_msg
    assert "MusterLieferant" in first_user_msg
