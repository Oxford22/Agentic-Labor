"""Gate logic for the PR eval diff.

Covers the three baseline states (`PRESENT`, `MISSING`, `ERRORED`) and
the one-shot `--first-time-package` allowance for `MISSING`.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from click.testing import CliRunner

from putsch_obs.eval.harness_diff import BaselineStatus, main
from putsch_obs.eval.schemas import (
    AgentTarget,
    EvalItemResult,
    EvalRunResult,
    Judgement,
    TargetKind,
)


def _run(
    *,
    head_score: float,
    n: int = 5,
    n_pass: int | None = None,
) -> EvalRunResult:
    n_pass = n_pass if n_pass is not None else n
    items = [
        EvalItemResult(
            item_id=f"i-{i}",
            target_output={"v": i},
            judgement=Judgement(
                score=head_score,
                **{"pass": i < n_pass},
                rationale="x",
            ),
            latency_ms=10.0,
            cost_eur=0.001,
        )
        for i in range(n)
    ]
    r = EvalRunResult(
        run_id="r",
        dataset_name="d",
        dataset_version="v",
        target=AgentTarget(kind=TargetKind.CUSTOM, name="x"),
        items=items,
    )
    r.aggregate()
    return r


def _invoke(tmp: Path, head: EvalRunResult, baseline_content: str | None, *extra_args: str) -> dict:
    head_path = tmp / "head.json"
    base_path = tmp / "baseline.json"
    head_path.write_text(head.model_dump_json())
    if baseline_content is not None:
        base_path.write_text(baseline_content)
    runner = CliRunner()
    result = runner.invoke(
        main,
        [
            "--head",
            str(head_path),
            "--baseline",
            str(base_path),
            "--regression-threshold",
            "0.05",
            "--output",
            str(tmp / "diff.md"),
            *extra_args,
        ],
        catch_exceptions=False,
    )
    assert result.exit_code == 0, result.output
    return json.loads((Path.cwd() / "diff.json").read_text())


@pytest.fixture(autouse=True)
def _cwd_tmp(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.chdir(tmp_path)


def test_present_no_regression_passes(tmp_path: Path) -> None:
    head = _run(head_score=0.95)
    baseline = _run(head_score=0.92).model_dump_json()
    diff = _invoke(tmp_path, head, baseline)
    assert diff["baseline_status"] == BaselineStatus.PRESENT.value
    assert diff["regressed"] is False
    assert diff["gate_pass"] is True


def test_present_with_regression_fails_gate(tmp_path: Path) -> None:
    head = _run(head_score=0.70)
    baseline = _run(head_score=0.95).model_dump_json()
    diff = _invoke(tmp_path, head, baseline)
    assert diff["baseline_status"] == BaselineStatus.PRESENT.value
    assert diff["regressed"] is True
    assert diff["gate_pass"] is False


def test_missing_without_one_shot_fails_gate(tmp_path: Path) -> None:
    head = _run(head_score=0.95)
    diff = _invoke(tmp_path, head, None)
    assert diff["baseline_status"] == BaselineStatus.MISSING.value
    assert diff["gate_pass"] is False


def test_missing_with_one_shot_passes_gate(tmp_path: Path) -> None:
    head = _run(head_score=0.95)
    diff = _invoke(tmp_path, head, None, "--first-time-package")
    assert diff["baseline_status"] == BaselineStatus.MISSING.value
    assert diff["gate_pass"] is True


def test_errored_baseline_always_fails(tmp_path: Path) -> None:
    head = _run(head_score=0.95)
    diff = _invoke(tmp_path, head, "{ this is not json", "--first-time-package")
    assert diff["baseline_status"] == BaselineStatus.ERRORED.value
    assert diff["gate_pass"] is False, "ERRORED must never silently pass"
