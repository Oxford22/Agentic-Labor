"""Diff two EvalRunResult JSON files and emit a Markdown comment.

CI feeds the head + baseline JSON into this; the output goes back into the
PR as a comment. ``--regression-threshold`` is the mean-score drop that
counts as a regression and fails the job.

Baseline outcomes are distinguished by :class:`BaselineStatus` so the
gating job can tell apart "baseline ran clean with no regression" from
"baseline never produced output" from "baseline ran but the output was
malformed". The third case must NEVER silently pass — that's how a
future PR that breaks the harness slides through unnoticed.
"""

from __future__ import annotations

import json
import sys
from enum import StrEnum
from pathlib import Path

import click

from putsch_obs.eval.schemas import EvalRunResult


class BaselineStatus(StrEnum):
    """How the baseline run resolved.

    The gating contract:
      * ``PRESENT`` + ``regressed=False`` → pass
      * ``MISSING`` + ``--first-time-package`` → pass (one-shot, see workflow)
      * ``MISSING`` without that flag → fail
      * ``ERRORED`` → always fail (harness is broken)
    """

    PRESENT = "present"
    MISSING = "missing"
    ERRORED = "errored"


def _load_baseline(path: Path) -> tuple[BaselineStatus, EvalRunResult | None, str | None]:
    """Resolve the baseline file into a (status, result, error_msg) triple."""
    if not path.exists():
        return BaselineStatus.MISSING, None, None
    try:
        raw = path.read_text()
    except OSError as exc:
        return BaselineStatus.ERRORED, None, f"read failed: {exc}"
    if not raw.strip():
        return BaselineStatus.MISSING, None, None
    try:
        result = EvalRunResult.model_validate(json.loads(raw))
    except Exception as exc:
        return BaselineStatus.ERRORED, None, f"validate failed: {exc}"
    return BaselineStatus.PRESENT, result, None


@click.command()
@click.option("--head", required=True, type=click.Path(exists=True, path_type=Path))
@click.option("--baseline", required=True, type=click.Path(path_type=Path))
@click.option("--regression-threshold", default=0.05, type=float)
@click.option(
    "--first-time-package",
    is_flag=True,
    default=False,
    help=(
        "One-shot flag. Allow MISSING baseline to pass when the package "
        "has not yet been merged to main. Remove from the workflow as "
        "soon as main contains the first eval-producing commit."
    ),
)
@click.option("--output", default="diff.md", type=click.Path(path_type=Path))
def main(
    head: Path,
    baseline: Path,
    regression_threshold: float,
    first_time_package: bool,
    output: Path,
) -> None:
    head_result = EvalRunResult.model_validate(json.loads(head.read_text()))
    status, baseline_result, baseline_err = _load_baseline(baseline)

    if status is BaselineStatus.PRESENT:
        assert baseline_result is not None
        delta: float | None = head_result.mean_score - baseline_result.mean_score
        regressed = delta < -regression_threshold
        gate_pass = not regressed
    elif status is BaselineStatus.MISSING:
        delta = None
        regressed = False
        gate_pass = first_time_package
    else:  # ERRORED
        delta = None
        regressed = False
        gate_pass = False

    md_lines = [
        f"### Eval — `{head_result.dataset_name}@{head_result.dataset_version}`",
        "",
        f"Baseline status: **{status.value}**"
        + (f" (`{baseline_err}`)" if baseline_err else ""),
        "",
        "| Metric        | Head                  | Baseline              | Δ        |",
        "| ------------- | --------------------- | --------------------- | -------- |",
    ]
    md_lines.append(
        _row(
            "mean_score",
            f"{head_result.mean_score:.4f}",
            f"{baseline_result.mean_score:.4f}" if baseline_result else "—",
            f"{delta:+.4f}" if delta is not None else "—",
            mark=("❌ regression" if regressed else "✅ ok" if delta is not None else ""),
        )
    )
    md_lines.append(
        _row(
            "pass / total",
            f"{head_result.n_pass} / {head_result.n}",
            f"{baseline_result.n_pass} / {baseline_result.n}" if baseline_result else "—",
            "",
        )
    )
    md_lines.append(
        _row(
            "mean_latency",
            f"{head_result.mean_latency_ms:.1f} ms",
            f"{baseline_result.mean_latency_ms:.1f} ms" if baseline_result else "—",
            "",
        )
    )
    md_lines.append(
        _row(
            "total_cost",
            f"€{head_result.total_cost_eur:.4f}",
            f"€{baseline_result.total_cost_eur:.4f}" if baseline_result else "—",
            "",
        )
    )
    md_lines.append("")
    if not gate_pass:
        if regressed:
            md_lines.append(
                f"> ❌ **Regression detected.** Mean-score dropped by "
                f"{abs(delta or 0):.4f} (threshold {regression_threshold:.4f})."
            )
        elif status is BaselineStatus.MISSING:
            md_lines.append(
                "> ❌ **Baseline missing** and `--first-time-package` not set. "
                "Either the baseline job failed silently, or main does not "
                "contain the harness yet — confirm and rerun."
            )
        elif status is BaselineStatus.ERRORED:
            md_lines.append(
                f"> ❌ **Baseline output unparseable** ({baseline_err}). "
                "Treat as a broken harness; investigate before merging."
            )
    elif delta is not None:
        md_lines.append(f"> ✅ No regression. Mean-score delta: {delta:+.4f}.")
    else:
        md_lines.append(
            "> ✅ First-time package — baseline missing accepted by flag."
        )

    output.write_text("\n".join(md_lines), encoding="utf-8")
    Path("diff.json").write_text(
        json.dumps(
            {
                "regressed": regressed,
                "delta": delta,
                "baseline_status": status.value,
                "baseline_error": baseline_err,
                "gate_pass": gate_pass,
                "head_mean_score": head_result.mean_score,
                "baseline_mean_score": baseline_result.mean_score
                if baseline_result
                else None,
            },
            default=float,
        )
    )
    sys.exit(0)


def _row(label: str, head: str, baseline: str, delta: str, mark: str = "") -> str:
    delta_cell = f"{delta} {mark}".strip()
    return f"| {label:<13} | {head:<21} | {baseline:<21} | {delta_cell:<8} |"


if __name__ == "__main__":
    main()
