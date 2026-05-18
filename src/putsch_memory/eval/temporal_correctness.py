"""Temporal-correctness eval.

A German-business adapted version of LongMemEval. 100 scenarios, each
consisting of:

* a sequence of episodes (with timestamps) seeded into the graph
* a question with a `business_time` qualifier
* an expected answer (typed, not free-text — comparing free-text would
  hide silent regressions)

The eval is *not* a model benchmark. It tests the graph + the temporal
query layer assuming the LLM extraction is perfect; the
`entity_disambiguation` suite separately tests the extractor.

Failures here are P0 and block release. The number we report is the
fraction of scenarios where the typed answer is bit-identical to the
expected one.
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
class SeedFact:
    entity_id: str
    label: str
    attributes: dict[str, Any]
    business_time_from: datetime
    business_time_to: datetime | None
    source_system: str


@dataclass(slots=True, frozen=True)
class TemporalCase:
    case_id: str
    description: str
    seeds: list[SeedFact]
    question_entity_id: str
    question_attribute: str
    question_business_time: datetime
    expected_value: Any
    rationale: str


@dataclass(slots=True, frozen=True)
class TemporalEvalReport:
    total: int
    passed: int
    failures: list[dict[str, Any]]

    @property
    def pass_rate(self) -> float:
        return self.passed / self.total if self.total else 0.0


async def run_temporal_correctness(
    client: MemoryClient, cases: list[TemporalCase]
) -> TemporalEvalReport:
    failures: list[dict[str, Any]] = []
    passed = 0
    for case in cases:
        try:
            actual = await _evaluate_case(client, case)
            if _equal(actual, case.expected_value):
                passed += 1
            else:
                failures.append(
                    {
                        "case_id": case.case_id,
                        "expected": case.expected_value,
                        "actual": actual,
                        "rationale": case.rationale,
                    }
                )
        except Exception as exc:  # eval should never crash on one bad case
            logger.exception("temporal_case_errored", case_id=case.case_id)
            failures.append({"case_id": case.case_id, "error": str(exc)})

    report = TemporalEvalReport(total=len(cases), passed=passed, failures=failures)
    logger.info(
        "temporal_correctness_eval_done",
        total=report.total,
        passed=report.passed,
        pass_rate=report.pass_rate,
    )
    return report


async def _evaluate_case(client: MemoryClient, case: TemporalCase) -> Any:
    fact = await client.as_of(case.question_entity_id, business_time=case.question_business_time)
    if fact is None:
        return None
    return fact.get(case.question_attribute)


def _equal(a: Any, b: Any) -> bool:
    """Tolerant equality — same value, same type (modulo str/Number coercions)."""
    if a is None and b is None:
        return True
    if isinstance(a, float) or isinstance(b, float):
        try:
            return float(a) == float(b)
        except (TypeError, ValueError):
            return False
    return a == b


# ---------------------------------------------------------------------------
# Test set loader
# ---------------------------------------------------------------------------


def load_default_cases() -> list[TemporalCase]:
    """Load the bundled 100-scenario German-business test set."""
    path = pathlib.Path(__file__).parent / "fixtures" / "german_business_scenarios.json"
    if not path.exists():
        return []
    raw = json.loads(path.read_text(encoding="utf-8"))
    cases: list[TemporalCase] = []
    for c in raw:
        seeds = [
            SeedFact(
                entity_id=s["entity_id"],
                label=s["label"],
                attributes=s["attributes"],
                business_time_from=datetime.fromisoformat(s["business_time_from"]),
                business_time_to=datetime.fromisoformat(s["business_time_to"])
                if s.get("business_time_to")
                else None,
                source_system=s["source_system"],
            )
            for s in c["seeds"]
        ]
        cases.append(
            TemporalCase(
                case_id=c["case_id"],
                description=c["description"],
                seeds=seeds,
                question_entity_id=c["question_entity_id"],
                question_attribute=c["question_attribute"],
                question_business_time=datetime.fromisoformat(c["question_business_time"]),
                expected_value=c["expected_value"],
                rationale=c["rationale"],
            )
        )
    return cases
