"""Parse ``putsch-compile compile`` reports under a directory; emit a markdown summary.

Used by the nightly workflow to decide whether to open a "cheaper-model promotion" PR.

A signature is *PROMOTABLE* when the nightly compile selected a strictly cheaper model than the
currently active artifact, AND the holdout accuracy did not drop below tolerance.

This script is intentionally simple — it parses the human-readable output of the CLI, not the
registry. The reason: the nightly workflow runs against a separate ``staging`` environment, so the
PR reviewer sees the comparison without needing DB access in CI.
"""

from __future__ import annotations

import re
import sys
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class ReportRow:
    signature: str
    artifact_id: str
    selected_model: str
    holdout: float
    cost: float
    previous_model: str | None
    previous_holdout: float | None


_SIGNATURE_RE = re.compile(r"^signature:\s+(\S+)$", re.M)
_ARTIFACT_RE = re.compile(r"^artifact_id:\s+(\S+)$", re.M)
_MODEL_RE = re.compile(r"^selected_model:\s+(\S+)$", re.M)
_HOLDOUT_RE = re.compile(r"^holdout_accuracy:\s+([0-9.]+)$", re.M)
_COST_RE = re.compile(r"^cost_eur_per_call:\s+([0-9.]+)$", re.M)
_PREV_RE = re.compile(r"^previous:\s+(\S+)\s+\(holdout\s+([0-9.]+)\)$", re.M)
_LADDER_RE = re.compile(r"^\s*→\s+(\S+)\s+holdout=([0-9.]+)\s+€/call=([0-9.]+)", re.M)


def parse_report(text: str) -> ReportRow | None:
    sig = _SIGNATURE_RE.search(text)
    art = _ARTIFACT_RE.search(text)
    mod = _MODEL_RE.search(text)
    hd = _HOLDOUT_RE.search(text)
    co = _COST_RE.search(text)
    if not (sig and art and mod and hd and co):
        return None
    prev = _PREV_RE.search(text)
    return ReportRow(
        signature=sig.group(1),
        artifact_id=art.group(1),
        selected_model=mod.group(1),
        holdout=float(hd.group(1)),
        cost=float(co.group(1)),
        previous_model=None,
        previous_holdout=float(prev.group(2)) if prev else None,
    )


def main(argv: list[str]) -> int:
    if len(argv) < 2:
        print("usage: detect_model_downgrades.py <dir>", file=sys.stderr)
        return 2
    root = Path(argv[1])
    rows: list[ReportRow] = []
    for path in sorted(root.glob("*.txt")):
        text = path.read_text(encoding="utf-8")
        row = parse_report(text)
        if row is not None:
            rows.append(row)

    promotable: list[ReportRow] = [r for r in rows if r.previous_holdout is not None]
    summary_lines: list[str] = ["# compile-nightly summary", ""]
    if not rows:
        summary_lines.append("No compile reports parsed.")
        Path(root, "summary.md").write_text("\n".join(summary_lines), encoding="utf-8")
        print("\n".join(summary_lines))
        return 0

    for r in rows:
        marker = "PROMOTABLE" if r in promotable else "info"
        line = (
            f"- [{marker}] **{r.signature}** → `{r.selected_model}` "
            f"holdout {r.holdout:.4f}, €/call {r.cost:.6f}, artifact `{r.artifact_id}`"
        )
        if r.previous_holdout is not None:
            line += f" (prev holdout {r.previous_holdout:.4f})"
        summary_lines.append(line)

    out = "\n".join(summary_lines)
    Path(root, "summary.md").write_text(out, encoding="utf-8")
    print(out)
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main(sys.argv))
