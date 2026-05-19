"""Entity-disambiguation eval.

Multi-entity scenarios exercising the reconciliation layer:

* Two vendors with very similar names ("Müller GmbH" Hagen vs
  "Müller GmbH" Asheville) and different USt-IdNr.
* Same vendor under three SAP numbers across subsidiaries.
* Customer entities that share a parent company but are legally
  distinct.

Metric: precision and recall on the "are these the same entity" call.
We track both because the cost of false-merge (wrong vendor gets the
payment) is higher than false-split (manual review queue grows).
"""

from __future__ import annotations

import json
import pathlib
from dataclasses import dataclass
from typing import TYPE_CHECKING

from putsch_memory.logging import get_logger
from putsch_memory.tools.reconcile_master_data import reconcile_master_data

if TYPE_CHECKING:
    from putsch_memory.graphiti_client import MemoryClient

logger = get_logger(__name__)


@dataclass(slots=True, frozen=True)
class DisambiguationCase:
    case_id: str
    description: str
    candidate_key: str
    key_kind: str
    expected_disagreement_attributes: tuple[str, ...]
    expected_requires_human: bool


@dataclass(slots=True, frozen=True)
class DisambiguationReport:
    total: int
    true_positive: int
    false_positive: int
    false_negative: int
    detail: list[dict[str, object]]

    @property
    def precision(self) -> float:
        denom = self.true_positive + self.false_positive
        return self.true_positive / denom if denom else 1.0

    @property
    def recall(self) -> float:
        denom = self.true_positive + self.false_negative
        return self.true_positive / denom if denom else 1.0

    @property
    def f1(self) -> float:
        p, r = self.precision, self.recall
        return (2 * p * r / (p + r)) if (p + r) else 0.0


async def run_entity_disambiguation(
    client: MemoryClient, cases: list[DisambiguationCase]
) -> DisambiguationReport:
    tp, fp, fn = 0, 0, 0
    detail: list[dict[str, object]] = []
    for case in cases:
        result = await reconcile_master_data(
            client,
            entity_type="Lieferant",  # the bundled set is vendor-focused
            candidate_key=case.candidate_key,
            key_kind=case.key_kind,  # type: ignore[arg-type]
        )
        detected = {d.attribute for d in result.disagreements}
        expected = set(case.expected_disagreement_attributes)
        local_tp = len(detected & expected)
        local_fp = len(detected - expected)
        local_fn = len(expected - detected)
        tp += local_tp
        fp += local_fp
        fn += local_fn
        detail.append(
            {
                "case_id": case.case_id,
                "detected": sorted(detected),
                "expected": sorted(expected),
                "requires_human_actual": result.requires_human,
                "requires_human_expected": case.expected_requires_human,
            }
        )
    report = DisambiguationReport(
        total=len(cases),
        true_positive=tp,
        false_positive=fp,
        false_negative=fn,
        detail=detail,
    )
    logger.info(
        "entity_disambiguation_eval_done",
        precision=report.precision,
        recall=report.recall,
        f1=report.f1,
    )
    return report


def load_default_cases() -> list[DisambiguationCase]:
    path = pathlib.Path(__file__).parent / "fixtures" / "disambiguation_cases.json"
    if not path.exists():
        return []
    raw = json.loads(path.read_text(encoding="utf-8"))
    return [
        DisambiguationCase(
            case_id=c["case_id"],
            description=c["description"],
            candidate_key=c["candidate_key"],
            key_kind=c["key_kind"],
            expected_disagreement_attributes=tuple(c["expected_disagreement_attributes"]),
            expected_requires_human=bool(c["expected_requires_human"]),
        )
        for c in raw
    ]
