"""CLI entry point for the eval suite.

Run as `putsch-memory-eval temporal_correctness`, etc. Exits non-zero
on regression. Used by CI as a gate on PRs that touch ontology or the
client.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from typing import Any

from putsch_memory.eval.entity_disambiguation import (
    load_default_cases as load_disambiguation_cases,
)
from putsch_memory.eval.entity_disambiguation import run_entity_disambiguation
from putsch_memory.eval.reconstruction_accuracy import (
    load_default_cases as load_reconstruction_cases,
)
from putsch_memory.eval.reconstruction_accuracy import run_reconstruction_accuracy
from putsch_memory.eval.temporal_correctness import (
    load_default_cases as load_temporal_cases,
)
from putsch_memory.eval.temporal_correctness import run_temporal_correctness
from putsch_memory.graphiti_client import MemoryClient
from putsch_memory.logging import configure_logging, get_logger

logger = get_logger(__name__)

# Pass/fail thresholds — bumped only with sign-off from the platform lead.
THRESHOLDS = {
    "temporal_correctness": 0.85,
    "entity_disambiguation": 0.80,   # F1
    "reconstruction_accuracy": 0.99,
}


async def _run_temporal() -> dict[str, Any]:
    async with await MemoryClient.from_env() as client:
        report = await run_temporal_correctness(client, load_temporal_cases())
    return {
        "suite": "temporal_correctness",
        "total": report.total,
        "passed": report.passed,
        "pass_rate": report.pass_rate,
        "failures": report.failures[:10],
    }


async def _run_disambiguation() -> dict[str, Any]:
    async with await MemoryClient.from_env() as client:
        report = await run_entity_disambiguation(client, load_disambiguation_cases())
    return {
        "suite": "entity_disambiguation",
        "total": report.total,
        "precision": report.precision,
        "recall": report.recall,
        "f1": report.f1,
        "detail": report.detail[:10],
    }


async def _run_reconstruction() -> dict[str, Any]:
    async with await MemoryClient.from_env() as client:
        report = await run_reconstruction_accuracy(client, load_reconstruction_cases())
    return {
        "suite": "reconstruction_accuracy",
        "total_cases": report.total_cases,
        "total_reads": report.total_reads,
        "matched_reads": report.matched_reads,
        "accuracy": report.accuracy,
        "mismatches": report.mismatches[:10],
    }


def main(argv: list[str] | None = None) -> int:
    configure_logging()
    parser = argparse.ArgumentParser(prog="putsch-memory-eval")
    parser.add_argument(
        "suite",
        choices=["temporal_correctness", "entity_disambiguation", "reconstruction_accuracy", "all"],
    )
    parser.add_argument("--strict", action="store_true", help="fail on threshold breach")
    parser.add_argument("--top-k", type=int, default=10, help="limit failure printout")
    args = parser.parse_args(argv)

    if args.suite == "all":
        suites = ["temporal_correctness", "entity_disambiguation", "reconstruction_accuracy"]
    else:
        suites = [args.suite]

    summaries: list[dict[str, Any]] = []
    for s in suites:
        if s == "temporal_correctness":
            summaries.append(asyncio.run(_run_temporal()))
        elif s == "entity_disambiguation":
            summaries.append(asyncio.run(_run_disambiguation()))
        elif s == "reconstruction_accuracy":
            summaries.append(asyncio.run(_run_reconstruction()))

    print(json.dumps(summaries, indent=2, default=str))

    if args.strict:
        for s in summaries:
            metric = s.get("pass_rate") or s.get("f1") or s.get("accuracy")
            threshold = THRESHOLDS.get(s["suite"], 0.0)
            if metric is None or metric < threshold:
                logger.error(
                    "eval_threshold_breach",
                    suite=s["suite"],
                    metric=metric,
                    threshold=threshold,
                )
                return 2
    return 0


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
