"""Diff two EvalRunResult JSON files and emit a Markdown comment.

CI feeds the head + baseline JSON into this; the output goes back into the
PR as a comment. ``--regression-threshold`` is the mean-score drop that
counts as a regression and fails the job.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import click

from putsch_obs.eval.schemas import EvalRunResult


@click.command()
@click.option("--head", required=True, type=click.Path(exists=True, path_type=Path))
@click.option("--baseline", required=True, type=click.Path(path_type=Path))
@click.option("--regression-threshold", default=0.05, type=float)
@click.option("--output", default="diff.md", type=click.Path(path_type=Path))
def main(
    head: Path,
    baseline: Path,
    regression_threshold: float,
    output: Path,
) -> None:
    head_result = EvalRunResult.model_validate(json.loads(head.read_text()))
    baseline_result = (
        EvalRunResult.model_validate(json.loads(baseline.read_text()))
        if baseline.exists()
        else None
    )

    if baseline_result is None:
        delta = None
        regressed = False
    else:
        delta = head_result.mean_score - baseline_result.mean_score
        regressed = delta < -regression_threshold

    md_lines = [
        f"### Eval — `{head_result.dataset_name}@{head_result.dataset_version}`",
        "",
        f"| Metric        | Head                    | Baseline                | Δ        |",
        f"| ------------- | ----------------------- | ----------------------- | -------- |",
        f"| mean_score    | {head_result.mean_score:.4f}              | "
        + (f"{baseline_result.mean_score:.4f}              " if baseline_result else "—".ljust(23))
        + f"| {delta:+.4f}" + (" ❌" if regressed else " ✅" if delta is not None else " —") + " |",
        f"| pass / total  | {head_result.n_pass} / {head_result.n}                | "
        + (
            f"{baseline_result.n_pass} / {baseline_result.n}                "
            if baseline_result
            else "—".ljust(23)
        )
        + "|          |",
        f"| mean_latency  | {head_result.mean_latency_ms:.1f} ms              | "
        + (
            f"{baseline_result.mean_latency_ms:.1f} ms              "
            if baseline_result
            else "—".ljust(23)
        )
        + "|          |",
        f"| total_cost    | €{head_result.total_cost_eur:.4f}              | "
        + (
            f"€{baseline_result.total_cost_eur:.4f}              "
            if baseline_result
            else "—".ljust(23)
        )
        + "|          |",
        "",
    ]
    if regressed:
        md_lines += [
            f"> ❌ **Regression detected.** Mean-score dropped by "
            f"{abs(delta):.4f} (threshold {regression_threshold:.4f})."
        ]
    elif delta is not None:
        md_lines += [
            f"> ✅ No regression. Mean-score delta: {delta:+.4f}."
        ]

    output.write_text("\n".join(md_lines), encoding="utf-8")
    Path("diff.json").write_text(
        json.dumps(
            {
                "regressed": regressed,
                "delta": delta,
                "head_mean_score": head_result.mean_score,
                "baseline_mean_score": baseline_result.mean_score
                if baseline_result
                else None,
            },
            default=float,
        )
    )
    sys.exit(0)


if __name__ == "__main__":
    main()
