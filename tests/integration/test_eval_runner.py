"""End-to-end eval-runner integration: 5-item dataset, no real Langfuse."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from putsch_obs.eval.datasets import load_dataset
from putsch_obs.eval.judges import LLMJudge
from putsch_obs.eval.runners import EvalRunner
from putsch_obs.eval.schemas import AgentTarget, Judgement, TargetKind


class _FakeJudge(LLMJudge):
    """In-process judge that returns a deterministic score."""

    async def __aenter__(self) -> "_FakeJudge":  # type: ignore[override]
        return self

    async def __aexit__(self, *_: Any) -> None:
        return None

    async def judge(
        self, *, rubric_id: str, actual: Any, expected: Any | None
    ) -> Judgement:
        # Perfect match → 1.0, else 0.5.
        is_match = json.dumps(actual, sort_keys=True, default=str) == json.dumps(
            expected, sort_keys=True, default=str
        )
        return Judgement(
            score=1.0 if is_match else 0.5,
            **{"pass": is_match},
            rationale="auto-graded by fake judge",
            confidence=1.0,
        )


@pytest.fixture()
def small_dataset(tmp_path: Path) -> Path:
    items = [
        {"item_id": f"i-{i}", "input": {"v": i}, "expected_output": {"v": i}, "rubric_id": "invoice_extraction"}
        for i in range(5)
    ]
    p = tmp_path / "datasets"
    p.mkdir()
    f = p / "tiny.jsonl"
    f.write_text("\n".join(json.dumps(i) for i in items), encoding="utf-8")
    return p


async def test_runner_end_to_end(small_dataset: Path) -> None:
    dataset = load_dataset("tiny", root=small_dataset)
    target = AgentTarget(kind=TargetKind.CUSTOM, name="echo", version="1")

    def echo(x: Any) -> Any:
        return x

    judge = _FakeJudge()
    async with EvalRunner(target=target, target_fn=echo, judge=judge) as runner:
        run = await runner.run(dataset)
    assert run.n == 5
    assert run.n_pass == 5
    assert run.mean_score == pytest.approx(1.0)
    assert run.total_cost_eur == 0.0


async def test_runner_flags_failures(small_dataset: Path) -> None:
    dataset = load_dataset("tiny", root=small_dataset)
    target = AgentTarget(kind=TargetKind.CUSTOM, name="bad", version="1")

    def wrong(x: Any) -> Any:
        return {"v": -1}

    judge = _FakeJudge()
    async with EvalRunner(target=target, target_fn=wrong, judge=judge) as runner:
        run = await runner.run(dataset)
    assert run.n_pass == 0
    assert run.n_fail == 5
    assert run.mean_score == pytest.approx(0.5)
