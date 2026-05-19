"""Pydantic schemas shared across the eval harness.

Three rules govern these models:

1. **Forward-compatible**. Extra fields are *forbidden* on dataset items
   (drift kills reproducibility) but allowed on result objects (additive
   evolution is fine).
2. **Cheap to serialize**. Eval runs may produce thousands of items; we
   skip ``model_dump_json(indent=...)`` and use compact JSON everywhere.
3. **Human-readable in PRs**. The CI bot diffs JSONL on file changes, so
   field names are short and lowercase.
"""

from __future__ import annotations

from datetime import datetime, timezone
from enum import StrEnum
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator


class TargetKind(StrEnum):
    CREW = "crew"
    LANGGRAPH = "langgraph"
    DSPY = "dspy"
    LITELLM = "litellm"
    CUSTOM = "custom"


class AgentTarget(BaseModel):
    """A logical thing to evaluate.

    The runner uses the (kind, name, version) tuple to write a Langfuse
    dataset-run with stable metadata.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    kind: TargetKind
    name: str
    version: str = "0.0.0"
    git_sha: str | None = None

    def label(self) -> str:
        return f"{self.kind.value}:{self.name}@{self.version}"


# ─────────────────────────────────────────────────────────────────────────────
# Datasets
# ─────────────────────────────────────────────────────────────────────────────


class DatasetItem(BaseModel):
    """A single eval item.

    ``input`` and ``expected_output`` are intentionally typed as ``Any`` so a
    dataset can carry structured PDFs, JSON ledgers, or plain strings. The
    runner is responsible for shaping the input for the target.
    """

    model_config = ConfigDict(extra="forbid")

    item_id: str = Field(..., description="Stable; survives dataset migrations.")
    input: Any
    expected_output: Any | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)
    rubric_id: str | None = Field(
        default=None,
        description="Which rubric to apply when judging. If None, no LLM-as-judge step.",
    )

    @field_validator("item_id")
    @classmethod
    def _id_format(cls, v: str) -> str:
        if not v or "/" in v or " " in v:
            raise ValueError("item_id must be a non-empty token without slashes or spaces")
        return v


# ─────────────────────────────────────────────────────────────────────────────
# Rubrics + judgements
# ─────────────────────────────────────────────────────────────────────────────


class Rubric(BaseModel):
    """A scoring rubric for the LLM judge.

    A rubric is keyed by ``rubric_id``. Items reference it; we keep the
    rubric library separately so a rubric update doesn't churn the dataset.
    """

    model_config = ConfigDict(extra="forbid")

    rubric_id: str
    name: str
    instructions: str = Field(
        ...,
        description="System prompt for the judge. Should be unambiguous and German-aware.",
    )
    scale: tuple[float, float] = Field(default=(0.0, 1.0))
    weights: dict[str, float] = Field(
        default_factory=dict,
        description="If the rubric is multi-criterion, the weights for each sub-score.",
    )


class Judgement(BaseModel):
    """Output of the LLM judge for a single item.

    ``populate_by_name`` lets both ``{"pass": True}`` (the alias the LLM
    judge emits — ``pass`` is a Python keyword so the field is ``pass_``)
    and ``{"pass_": True}`` (the form produced by ``model_dump_json()``)
    parse. Without it, round-trip serialization through the harness diff
    pipeline silently fails on the load side.
    """

    model_config = ConfigDict(extra="allow", populate_by_name=True)

    score: float
    pass_: bool = Field(alias="pass")
    rationale: str
    confidence: float = 1.0
    sub_scores: dict[str, float] = Field(default_factory=dict)
    flagged_for_review: bool = False


# ─────────────────────────────────────────────────────────────────────────────
# Run-level results
# ─────────────────────────────────────────────────────────────────────────────


class EvalItemResult(BaseModel):
    model_config = ConfigDict(extra="allow")

    item_id: str
    target_output: Any
    judgement: Judgement | None = None
    latency_ms: float
    cost_eur: float | None = None
    error: str | None = None
    trace_id: str | None = None


class EvalRunResult(BaseModel):
    model_config = ConfigDict(extra="allow")

    run_id: str
    dataset_name: str
    dataset_version: str
    target: AgentTarget
    started_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    items: list[EvalItemResult] = Field(default_factory=list)

    # Aggregate metrics — computed once at end of run.
    n: int = 0
    n_pass: int = 0
    n_fail: int = 0
    mean_score: float = 0.0
    mean_latency_ms: float = 0.0
    total_cost_eur: float = 0.0
    regression_vs_baseline: float | None = None

    def aggregate(self) -> None:
        items = self.items
        self.n = len(items)
        scores = [i.judgement.score for i in items if i.judgement is not None]
        self.n_pass = sum(1 for i in items if i.judgement and i.judgement.pass_)
        self.n_fail = self.n - self.n_pass
        self.mean_score = sum(scores) / len(scores) if scores else 0.0
        self.mean_latency_ms = (
            sum(i.latency_ms for i in items) / self.n if self.n else 0.0
        )
        self.total_cost_eur = sum(i.cost_eur or 0.0 for i in items)


# ─────────────────────────────────────────────────────────────────────────────
# Annotation payload shape (matches Langfuse annotation queue)
# ─────────────────────────────────────────────────────────────────────────────


class AnnotationStatus(StrEnum):
    PENDING = "pending"
    APPROVED = "approved"
    REJECTED = "rejected"
    NEEDS_REVISION = "needs_revision"


class AnnotationItem(BaseModel):
    model_config = ConfigDict(extra="allow")

    trace_id: str
    queue: str
    reviewer: str | None = None
    decision: AnnotationStatus = AnnotationStatus.PENDING
    correction: Any | None = None
    notes: str | None = None
    reviewed_at: datetime | None = None
    flagged_reason: Literal[
        "low_confidence",
        "high_cost",
        "high_latency",
        "rubric_fail",
        "manual",
    ] = "manual"


__all__ = [
    "AgentTarget",
    "AnnotationItem",
    "AnnotationStatus",
    "DatasetItem",
    "EvalItemResult",
    "EvalRunResult",
    "Judgement",
    "Rubric",
    "TargetKind",
]
