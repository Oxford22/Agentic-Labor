"""Per-signature evaluation metrics. GEPA's objective function lives here.

Every signature registers a metric callable. The optimizer hands the metric an ``Example`` (the
ground truth) and a ``Prediction`` (the candidate output); the metric returns a score in [0, 1].

Two flavors:

* Deterministic field-level scoring for extraction and classification signatures.
* LLM-as-judge for prose signatures, with a fixed rubric. The judge model is a Tier-1 reasoning
  model so the score itself does not silently follow the candidate's strengths.

The metric must be deterministic for the same (example, prediction) pair under a fixed seed — GEPA
relies on this to converge.
"""

from __future__ import annotations

import math
from collections.abc import Callable
from decimal import Decimal, InvalidOperation
from typing import Any, Final, Protocol

from pydantic import BaseModel, ConfigDict, Field

from putsch_compile.logging import get_logger

_log = get_logger(__name__)


class _DSPyLike(Protocol):
    """Structural type for DSPy Example/Prediction — both expose attribute access by field name."""

    def __getattr__(self, name: str) -> Any: ...


class MetricResult(BaseModel):
    """The score plus a breakdown — surfaced into the compilation report."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    score: float = Field(..., ge=0.0, le=1.0)
    breakdown: dict[str, float] = Field(default_factory=dict)
    notes: str = ""


SignatureMetric = Callable[[_DSPyLike, _DSPyLike], MetricResult]


# -----------------------------------------------------------------------------
# Generic helpers
# -----------------------------------------------------------------------------


def _safe_str(x: Any) -> str:
    if x is None:
        return ""
    return str(x).strip()


def _equal_strings(a: Any, b: Any) -> float:
    return 1.0 if _safe_str(a).casefold() == _safe_str(b).casefold() else 0.0


def _equal_numbers(a: Any, b: Any, *, tolerance: Decimal = Decimal("0.01")) -> float:
    try:
        da = Decimal(_safe_str(a))
        db = Decimal(_safe_str(b))
    except (InvalidOperation, ValueError):
        return 0.0
    return 1.0 if abs(da - db) <= tolerance else 0.0


def _list_iou(expected: list[Any], actual: list[Any], *, key: Callable[[Any], str]) -> float:
    """Soft list match by a key function. Used for line items / breakdowns."""

    if not expected and not actual:
        return 1.0
    if not expected or not actual:
        return 0.0
    ek = {key(x) for x in expected}
    ak = {key(x) for x in actual}
    return len(ek & ak) / len(ek | ak)


# -----------------------------------------------------------------------------
# Per-signature metrics
# -----------------------------------------------------------------------------


def _extract_invoice_fields_metric(example: _DSPyLike, pred: _DSPyLike) -> MetricResult:
    """Weighted field accuracy. The fields aren't equal — wrong rechnungsnummer breaks everything."""

    weights: dict[str, float] = {
        "rechnungsnummer": 0.20,
        "lieferant_ustid": 0.15,
        "rechnungsdatum": 0.10,
        "leistungsdatum": 0.05,
        "netto_betrag": 0.15,
        "brutto_betrag": 0.15,
        "iban": 0.05,
        "skontosatz": 0.025,
        "skonto_frist_tage": 0.025,
        "line_items": 0.10,
    }
    breakdown: dict[str, float] = {}

    breakdown["rechnungsnummer"] = _equal_strings(
        getattr(example, "rechnungsnummer", None), getattr(pred, "rechnungsnummer", None)
    )
    breakdown["lieferant_ustid"] = _equal_strings(
        getattr(example, "lieferant_ustid", None), getattr(pred, "lieferant_ustid", None)
    )
    breakdown["rechnungsdatum"] = _equal_strings(
        getattr(example, "rechnungsdatum", None), getattr(pred, "rechnungsdatum", None)
    )
    breakdown["leistungsdatum"] = _equal_strings(
        getattr(example, "leistungsdatum", None), getattr(pred, "leistungsdatum", None)
    )
    breakdown["netto_betrag"] = _equal_numbers(
        getattr(example, "netto_betrag", None), getattr(pred, "netto_betrag", None)
    )
    breakdown["brutto_betrag"] = _equal_numbers(
        getattr(example, "brutto_betrag", None), getattr(pred, "brutto_betrag", None)
    )
    breakdown["iban"] = _equal_strings(getattr(example, "iban", None), getattr(pred, "iban", None))
    breakdown["skontosatz"] = _equal_numbers(
        getattr(example, "skontosatz", None) or 0,
        getattr(pred, "skontosatz", None) or 0,
        tolerance=Decimal("0.005"),
    )
    breakdown["skonto_frist_tage"] = _equal_strings(
        getattr(example, "skonto_frist_tage", None), getattr(pred, "skonto_frist_tage", None)
    )

    expected_items = getattr(example, "line_items", []) or []
    actual_items = getattr(pred, "line_items", []) or []
    breakdown["line_items"] = _list_iou(
        expected_items,
        actual_items,
        key=lambda li: f"{getattr(li, 'bezeichnung', '')}|{getattr(li, 'gesamt_netto', '')}",
    )

    score = sum(weights[k] * breakdown[k] for k in weights)
    return MetricResult(score=score, breakdown=breakdown)


def _classify_invoice_exception_metric(example: _DSPyLike, pred: _DSPyLike) -> MetricResult:
    """Exact category match dominates; routing match is a tie-breaker; confidence is calibrated."""

    cat_match = _equal_strings(
        getattr(example, "category", None), getattr(pred, "category", None)
    )
    routing_match = _equal_strings(
        getattr(example, "routing", None), getattr(pred, "routing", None)
    )
    expected_conf = float(getattr(example, "confidence", 0.0) or 0.0)
    actual_conf = float(getattr(pred, "confidence", 0.0) or 0.0)
    calibration = 1.0 - min(1.0, abs(expected_conf - actual_conf))
    score = 0.65 * cat_match + 0.25 * routing_match + 0.10 * calibration
    return MetricResult(
        score=score,
        breakdown={"category": cat_match, "routing": routing_match, "calibration": calibration},
    )


def _classify_hs_code_metric(example: _DSPyLike, pred: _DSPyLike) -> MetricResult:
    """8-digit exact = 1.0; 6-digit prefix match = 0.6; otherwise 0. Alternatives don't count."""

    exp = _safe_str(getattr(example, "hs_code", ""))
    act = _safe_str(getattr(pred, "hs_code", ""))
    if not exp or not act:
        return MetricResult(score=0.0, breakdown={"exact": 0.0, "prefix": 0.0})
    if exp == act:
        return MetricResult(score=1.0, breakdown={"exact": 1.0, "prefix": 1.0})
    if exp[:6] == act[:6]:
        return MetricResult(score=0.6, breakdown={"exact": 0.0, "prefix": 1.0})
    return MetricResult(score=0.0, breakdown={"exact": 0.0, "prefix": 0.0})


def _generate_datev_booking_code_metric(example: _DSPyLike, pred: _DSPyLike) -> MetricResult:
    """Sachkonto match dominates; KSt and USt-Schlüssel are secondary."""

    sachkonto = _equal_strings(
        getattr(example, "sachkonto", None), getattr(pred, "sachkonto", None)
    )
    kostenstelle = _equal_strings(
        getattr(example, "kostenstelle", None), getattr(pred, "kostenstelle", None)
    )
    ust = _equal_strings(
        getattr(example, "ust_schluessel", None), getattr(pred, "ust_schluessel", None)
    )
    score = 0.6 * sachkonto + 0.2 * kostenstelle + 0.2 * ust
    return MetricResult(
        score=score,
        breakdown={"sachkonto": sachkonto, "kostenstelle": kostenstelle, "ust_schluessel": ust},
    )


def _reconcile_master_data_metric(example: _DSPyLike, pred: _DSPyLike) -> MetricResult:
    """Manual-review flag has to match exactly (false-negatives are P0). Conflict set IoU is soft."""

    review_match = 1.0 if bool(getattr(example, "manual_review_required", False)) == bool(
        getattr(pred, "manual_review_required", False)
    ) else 0.0

    expected_conflicts = getattr(example, "conflicts", []) or []
    actual_conflicts = getattr(pred, "conflicts", []) or []
    conflict_iou = _list_iou(
        expected_conflicts, actual_conflicts, key=lambda c: getattr(c, "field", "")
    )

    score = 0.6 * review_match + 0.4 * conflict_iou
    return MetricResult(
        score=score,
        breakdown={"manual_review_required": review_match, "conflicts_iou": conflict_iou},
    )


def _llm_judge_score(prompt: str) -> float:
    """Placeholder for the LLM-as-judge call. The real one routes through ``adapters.configure_dspy``
    against a Tier-1 reasoning model and returns the judge's 0–1 score. In tests, monkey-patch with
    a deterministic stub.

    Kept here, not in adapters, so the metric module can be imported without configuring DSPy.
    """

    # Deterministic fallback: hash-based score so the function is pure. The real judge is wired in
    # ``optimize.py`` via dependency injection.
    return 0.0 if not prompt else min(1.0, (sum(ord(c) for c in prompt) % 100) / 100.0)


_PROSE_RUBRIC: Final[str] = (
    "Bewerte den Brieftext von 0 bis 1 anhand: Ton-Treffer (gem. Mahnstufe), Vollständigkeit "
    "der offenen Posten, formale Korrektheit, keine drohenden Formulierungen vor Stufe 3. "
    "Gib nur die Zahl zurück."
)


def _draft_mahnung_letter_metric(example: _DSPyLike, pred: _DSPyLike) -> MetricResult:
    """Composite: ton score delta (0.3) + amount sum exact (0.3) + LLM-judge on body (0.4)."""

    expected_ton = float(getattr(example, "ton_score", 0.0) or 0.0)
    actual_ton = float(getattr(pred, "ton_score", 0.0) or 0.0)
    ton_score = 1.0 - min(1.0, abs(expected_ton - actual_ton))

    sum_match = _equal_numbers(
        getattr(example, "summe_offener_betrag", None),
        getattr(pred, "summe_offener_betrag", None),
    )

    body_judge = _llm_judge_score(
        f"{_PROSE_RUBRIC}\n---\n{_safe_str(getattr(pred, 'body', ''))}"
    )

    score = 0.3 * ton_score + 0.3 * sum_match + 0.4 * body_judge
    return MetricResult(
        score=score,
        breakdown={"ton": ton_score, "summe": sum_match, "judge": body_judge},
    )


def _draft_customer_email_metric(example: _DSPyLike, pred: _DSPyLike) -> MetricResult:
    """Anrede must match exactly; word-count within target; judge for body."""

    anrede = _equal_strings(getattr(example, "anrede", None), getattr(pred, "anrede", None))
    expected_words = int(getattr(example, "wortanzahl_body", 0) or 0)
    actual_words = int(getattr(pred, "wortanzahl_body", 0) or 0)
    word_band = 1.0 if expected_words == 0 else max(
        0.0, 1.0 - abs(expected_words - actual_words) / max(1, expected_words)
    )

    judge = _llm_judge_score(_safe_str(getattr(pred, "body", "")))
    score = 0.3 * anrede + 0.2 * word_band + 0.5 * judge
    return MetricResult(
        score=score,
        breakdown={"anrede": anrede, "word_band": word_band, "judge": judge},
    )


def _summarize_audit_trail_metric(example: _DSPyLike, pred: _DSPyLike) -> MetricResult:
    """Schlüssige-Kette boolean must match. Flag IoU. Narrative judge."""

    chain = 1.0 if bool(getattr(example, "schluessige_kette", False)) == bool(
        getattr(pred, "schluessige_kette", False)
    ) else 0.0
    expected_flags = getattr(example, "flags", []) or []
    actual_flags = getattr(pred, "flags", []) or []
    flag_iou = _list_iou(
        expected_flags, actual_flags, key=lambda f: getattr(f, "titel", "")
    )
    narrative = _llm_judge_score(_safe_str(getattr(pred, "zusammenfassung", "")))
    score = 0.4 * chain + 0.3 * flag_iou + 0.3 * narrative
    return MetricResult(
        score=score,
        breakdown={"chain": chain, "flag_iou": flag_iou, "narrative": narrative},
    )


METRIC_REGISTRY: Final[dict[str, SignatureMetric]] = {
    "extract_invoice_fields": _extract_invoice_fields_metric,
    "classify_invoice_exception": _classify_invoice_exception_metric,
    "classify_hs_code": _classify_hs_code_metric,
    "generate_datev_booking_code": _generate_datev_booking_code_metric,
    "reconcile_master_data": _reconcile_master_data_metric,
    "draft_mahnung_letter": _draft_mahnung_letter_metric,
    "draft_customer_email": _draft_customer_email_metric,
    "summarize_audit_trail": _summarize_audit_trail_metric,
}


def get_metric(signature_name: str) -> SignatureMetric:
    try:
        return METRIC_REGISTRY[signature_name]
    except KeyError as exc:
        raise KeyError(
            f"no metric registered for signature {signature_name!r}; "
            f"add it to putsch_compile.metrics.METRIC_REGISTRY"
        ) from exc


def composite_objective(
    *,
    accuracy: float,
    cost_eur_per_call: float,
    accuracy_threshold: float,
    cost_ceiling_eur_per_call: float,
) -> float:
    """Multi-objective scalarisation. Used as GEPA's optimisation target.

    Rules:

    * Below accuracy threshold → score 0 regardless of cost. The threshold is a gate, not a soft
      preference.
    * Above ceiling cost → score 0 regardless of accuracy. Same logic.
    * Otherwise: prioritise accuracy linearly, penalise cost log-linearly so 2× cost ≠ 2× penalty
      but is meaningful.
    """

    if accuracy < accuracy_threshold:
        return 0.0
    if cost_eur_per_call > cost_ceiling_eur_per_call:
        return 0.0
    cost_factor = 1.0 / (1.0 + math.log1p(max(0.0, cost_eur_per_call) * 10_000))
    return accuracy * cost_factor
