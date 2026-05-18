"""Reconstruction-accuracy eval — the EU AI Act Art. 12 compliance test.

Given a past Langfuse trace, can we reconstruct the agent's belief state
from memory alone? "Reconstruct" means: for every memory read the agent
performed, asking the graph the same question with `system_time =
trace.started_at` returns the same answer the agent saw.

If this eval drops below 99 %, the compliance story fails. CI gate at
100 % on the seeded golden traces; production weekly check on a sample
of real traces.
"""

from __future__ import annotations

import json
import pathlib
from dataclasses import dataclass
from datetime import datetime
from typing import TYPE_CHECKING, Any

from putsch_memory.logging import get_logger

if TYPE_CHECKING:
    from putsch_memory.graphiti_client import MemoryClient

logger = get_logger(__name__)


@dataclass(slots=True, frozen=True)
class RecordedRead:
    entity_id: str
    business_time: datetime
    expected_fact_id: str
    expected_attribute_values: dict[str, Any]


@dataclass(slots=True, frozen=True)
class ReconstructionCase:
    case_id: str
    trace_id: str
    trace_started_at: datetime
    reads: list[RecordedRead]


@dataclass(slots=True, frozen=True)
class ReconstructionReport:
    total_cases: int
    total_reads: int
    matched_reads: int
    mismatches: list[dict[str, Any]]

    @property
    def accuracy(self) -> float:
        return self.matched_reads / self.total_reads if self.total_reads else 0.0


async def run_reconstruction_accuracy(
    client: MemoryClient, cases: list[ReconstructionCase]
) -> ReconstructionReport:
    matched, total = 0, 0
    mismatches: list[dict[str, Any]] = []
    for case in cases:
        for read in case.reads:
            total += 1
            actual = await client.as_of_with_system_time(
                read.entity_id,
                business_time=read.business_time,
                system_time=case.trace_started_at,
            )
            ok = _matches(actual, read)
            if ok:
                matched += 1
            else:
                mismatches.append(
                    {
                        "case_id": case.case_id,
                        "trace_id": case.trace_id,
                        "entity_id": read.entity_id,
                        "expected": read.expected_attribute_values,
                        "actual": _project(actual, read.expected_attribute_values.keys()),
                    }
                )
    report = ReconstructionReport(
        total_cases=len(cases),
        total_reads=total,
        matched_reads=matched,
        mismatches=mismatches,
    )
    logger.info(
        "reconstruction_eval_done",
        accuracy=report.accuracy,
        mismatches=len(mismatches),
    )
    return report


def _matches(actual: dict[str, Any] | None, read: RecordedRead) -> bool:
    if actual is None:
        return False
    for k, v in read.expected_attribute_values.items():
        if actual.get(k) != v:
            return False
    return True


def _project(actual: dict[str, Any] | None, keys: Any) -> dict[str, Any] | None:
    if actual is None:
        return None
    return {k: actual.get(k) for k in keys}


def load_default_cases() -> list[ReconstructionCase]:
    path = pathlib.Path(__file__).parent / "fixtures" / "reconstruction_cases.json"
    if not path.exists():
        return []
    raw = json.loads(path.read_text(encoding="utf-8"))
    return [
        ReconstructionCase(
            case_id=c["case_id"],
            trace_id=c["trace_id"],
            trace_started_at=datetime.fromisoformat(c["trace_started_at"]),
            reads=[
                RecordedRead(
                    entity_id=r["entity_id"],
                    business_time=datetime.fromisoformat(r["business_time"]),
                    expected_fact_id=r["expected_fact_id"],
                    expected_attribute_values=r["expected_attribute_values"],
                )
                for r in c["reads"]
            ],
        )
        for c in raw
    ]
