import pytest
from pydantic import ValidationError

from swarm.ledger import ProgressLedger, TaskLedger


def test_task_ledger_defaults():
    ledger = TaskLedger(task="check invoice 1187")
    assert ledger.facts == []
    assert ledger.guesses == []
    assert ledger.plan == []
    assert ledger.revision == 0


def test_task_ledger_revision_tracked():
    ledger = TaskLedger(
        task="x", facts=["a"], guesses=["b"], plan=["[finance] post"], revision=3
    )
    assert ledger.revision == 3
    assert ledger.plan == ["[finance] post"]


def test_progress_ledger_satisfied_path():
    p = ProgressLedger(
        is_request_satisfied=True,
        is_in_loop=False,
        is_progress_being_made=True,
        final_answer="done",
        reasoning="all checks passed",
    )
    assert p.final_answer == "done"
    assert p.next_speaker is None
    assert p.instruction_or_question is None


def test_progress_ledger_dispatch_path():
    p = ProgressLedger(
        is_request_satisfied=False,
        is_in_loop=False,
        is_progress_being_made=True,
        next_speaker="procurement",
        instruction_or_question="Pull PO 4500017722.",
        reasoning="need PO match",
    )
    assert p.next_speaker == "procurement"
    assert p.final_answer is None


def test_progress_ledger_rejects_missing_required_fields():
    with pytest.raises(ValidationError):
        ProgressLedger(is_request_satisfied=True)  # type: ignore[call-arg]
