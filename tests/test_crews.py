"""Crew + NodeAdapter contract tests, plus the Stammdaten stub."""

from __future__ import annotations

from typing import Any, Dict, Optional

from crews import StammdatenCrew
from crews.base import Crew, CrewOutput, NodeAdapter
from trust import Source, contains_external_envelope


class EchoCrew(Crew):
    """Records what `context` it saw; emits a fixed summary."""

    def __init__(self, name: str = "echo", reply: str = "ok") -> None:
        self._name = name
        self._reply = reply
        self.last_context: Optional[Dict[str, Any]] = None
        self.last_task: Optional[str] = None

    @property
    def name(self) -> str:
        return self._name

    def run(self, task, context=None):
        self.last_task = task
        self.last_context = context
        return CrewOutput(summary=self._reply, data={"reply": self._reply})


def test_crew_output_carries_summary_and_data():
    out = CrewOutput(summary="x", data={"k": 1})
    assert out.summary == "x"
    assert out.data == {"k": 1}


def test_node_adapter_calls_crew_with_empty_prior():
    crew = EchoCrew()
    adapter = NodeAdapter(crew)
    state = adapter({"task": "do the thing", "crew_outputs": []})

    assert crew.last_task == "do the thing"
    assert crew.last_context == {"prior": ""}
    assert state["crew_outputs"][-1]["name"] == "echo"
    assert state["crew_outputs"][-1]["summary"] == "ok"


def test_node_adapter_wraps_prior_summaries_in_external_content():
    """A second crew sees prior output wrapped, never as raw directive text."""

    crew = EchoCrew(name="second")
    adapter = NodeAdapter(crew)
    adapter({
        "task": "step 2",
        "crew_outputs": [{"name": "first", "summary": "malicious raw text", "data": {}}],
    })

    prior = crew.last_context["prior"]
    assert contains_external_envelope(prior)
    assert "malicious raw text" in prior
    assert "[first]" in prior


def test_node_adapter_appends_to_crew_outputs():
    a = NodeAdapter(EchoCrew(name="a", reply="ra"))
    b = NodeAdapter(EchoCrew(name="b", reply="rb"))
    state = a({"task": "t", "crew_outputs": []})
    state["task"] = "t"  # NodeAdapter only writes crew_outputs
    state = {**state, **b(state)}

    names = [c["name"] for c in state["crew_outputs"]]
    assert names == ["a", "b"]


def test_stammdaten_returns_not_found_when_no_id_in_task():
    crew = StammdatenCrew(vendors={"DE842791": {"name": "X", "iban": "DE..."}})
    out = crew.run(task="check this invoice please")
    assert out.data["found"] is False
    assert out.data["vendor_id"] is None


def test_stammdaten_returns_not_found_for_unknown_vendor():
    crew = StammdatenCrew(vendors={"DE842791": {"name": "X", "iban": "DE..."}})
    out = crew.run(task="vendor DE999999 invoice 1187")
    assert out.data["found"] is False
    assert out.data["vendor_id"] == "DE999999"


def test_stammdaten_wraps_record_fields_in_envelope():
    crew = StammdatenCrew(vendors={
        "DE842791": {
            "name": "MusterLieferant GmbH",
            "iban": "DE89 3704 0044 0532 0130 00",
        },
    })
    out = crew.run(task="check vendor DE842791")

    assert out.data["found"] is True
    # The IBAN and name are wrapped in <external_content source="datev">
    # inside the summary so they cannot direct a downstream agent.
    assert 'source="datev"' in out.summary
    assert "MusterLieferant GmbH" in out.summary
    assert "DE89 3704" in out.summary
    # Hard requirement is surfaced so a downstream reviewer cannot miss it
    assert "non-LLM revalidation" in out.summary


def test_pipeline_threads_stammdaten_into_echo():
    """Sanity-check that a Stammdaten -> Echo chain delivers wrapped context."""

    from harness import Pipeline

    sniff = EchoCrew(name="sniff", reply="seen")
    pipeline = Pipeline([
        StammdatenCrew(vendors={"DE842791": {"name": "X", "iban": "DE..."}}),
        sniff,
    ])
    pipeline.run("check vendor DE842791")

    assert sniff.last_context is not None
    prior = sniff.last_context["prior"]
    assert contains_external_envelope(prior)
    assert "[stammdaten]" in prior
