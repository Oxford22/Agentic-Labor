"""GEPA compilation harness.

Pipeline (one call to ``OptimizerHarness.compile_signature``):

1. Load the dataset for the signature from ``evals/datasets/<name>.jsonl``. Schema-validate every
   row. Refuse anonymous labels.
2. Deterministic shuffle + train/holdout split using the configured seed.
3. Walk the cheapest-model-first ladder from the router. For each candidate:
   a. Configure DSPy with that model + the BAML adapter.
   b. Wrap the signature in ``dspy.ChainOfThought``.
   c. Run GEPA against the train set with the signature's metric.
   d. Evaluate the compiled program on the holdout.
   e. Compute composite (accuracy + cost) score using ``metrics.composite_objective``.
   f. Keep the first candidate that clears both the accuracy threshold and the cost ceiling.
4. Compare the new candidate against the currently active production artifact's holdout accuracy.
   A regression beyond ``settings.compilation.regression_tolerance`` halts the pipeline — the
   previous artifact stays active and ``RegressionError`` propagates.
5. Persist artifact to MinIO via ``ArtifactStore``, record metadata in the Postgres registry.
6. Open a Langfuse compilation report with the model ladder, scores, dataset hash, and diff.

Determinism: same dataset + seed + GEPA config + model → same ``content_hash``. CI's regression
test relies on this. Non-determinism in this file is a bug.
"""

from __future__ import annotations

import asyncio
import random
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import dspy
import orjson
from pydantic import BaseModel, ConfigDict

from putsch_compile.adapters import configure_dspy
from putsch_compile.artifacts import CompiledArtifact, OptimizedDemo, hash_dataset
from putsch_compile.config import Settings, get_settings
from putsch_compile.exceptions import (
    DatasetError,
    OptimizerError,
    RegistryError,
    RegressionError,
)
from putsch_compile.logging import correlation_scope, get_logger
from putsch_compile.metrics import MetricResult, composite_objective, get_metric
from putsch_compile.registry import CompiledArtifactRecord, Registry
from putsch_compile.routing import ModelCard, Router
from putsch_compile.signatures import SIGNATURE_REGISTRY, PutschSignature
from putsch_compile.tracing import compilation_report

_log = get_logger(__name__)


# -----------------------------------------------------------------------------
# Results
# -----------------------------------------------------------------------------


class CandidateResult(BaseModel):
    """One row in the ladder report — which model was tried, what it scored, what it cost."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    model: str
    train_score: float
    holdout_score: float
    holdout_breakdown: dict[str, float]
    cost_eur_per_call: float
    objective: float
    accepted: bool


class CompilationResult(BaseModel):
    """The outcome of a compile run — what got persisted, the ladder, the diff."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    signature_name: str
    artifact_id: str
    selected_model: str
    holdout_accuracy: float
    cost_eur_per_call: float
    dataset_hash: str
    candidates: list[CandidateResult]
    previous_artifact_id: str | None
    previous_holdout_accuracy: float | None
    is_regression: bool
    compiled_at: datetime


# -----------------------------------------------------------------------------
# Dataset loading
# -----------------------------------------------------------------------------


@dataclass(frozen=True)
class _SplitDataset:
    """Train / holdout / canonical dataset hash. All rows are validated dicts."""

    train: list[dict[str, Any]]
    holdout: list[dict[str, Any]]
    dataset_hash: str
    all_rows: list[dict[str, Any]]


def _load_jsonl(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        raise DatasetError(f"dataset not found: {path}", context={"path": str(path)})
    rows: list[dict[str, Any]] = []
    for lineno, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
        if not line.strip():
            continue
        try:
            rows.append(orjson.loads(line))
        except orjson.JSONDecodeError as exc:
            raise DatasetError(
                f"invalid JSON in {path}:{lineno}",
                context={"path": str(path), "line": lineno},
            ) from exc
    if not rows:
        raise DatasetError(f"dataset is empty: {path}", context={"path": str(path)})
    return rows


def _validate_provenance(rows: list[dict[str, Any]], *, path: Path) -> None:
    """Refuse anonymous labels. Every row must have ``labeled_by``, ``labeled_at``, ``label_confidence``."""

    required = {"labeled_by", "labeled_at", "label_confidence"}
    for i, row in enumerate(rows):
        missing = required - row.keys()
        if missing:
            raise DatasetError(
                f"row {i} in {path.name} missing provenance fields: {sorted(missing)}",
                context={"row_index": i, "missing": sorted(missing)},
            )
        if not str(row["labeled_by"]).strip():
            raise DatasetError(
                f"row {i} in {path.name} has empty labeled_by", context={"row_index": i}
            )


def _split_dataset(
    rows: list[dict[str, Any]],
    *,
    holdout_fraction: float,
    seed: int,
) -> _SplitDataset:
    indices = list(range(len(rows)))
    rng = random.Random(seed)
    rng.shuffle(indices)
    n_hold = max(1, int(round(len(rows) * holdout_fraction)))
    hold_idx = set(indices[:n_hold])
    train = [rows[i] for i in range(len(rows)) if i not in hold_idx]
    holdout = [rows[i] for i in range(len(rows)) if i in hold_idx]
    if not train:
        raise DatasetError(
            "after split, train set is empty — dataset too small",
            context={"n_rows": len(rows), "holdout_fraction": holdout_fraction},
        )
    return _SplitDataset(
        train=train,
        holdout=holdout,
        dataset_hash=hash_dataset(rows),
        all_rows=rows,
    )


def _to_dspy_example(row: dict[str, Any], signature: type[PutschSignature]) -> dspy.Example:
    """Turn a dataset row into a ``dspy.Example`` with explicit input marking.

    Dataset rows contain *both* inputs and outputs. We split via the signature's field metadata so
    ``with_inputs(...)`` correctly tells DSPy which keys are the program's inputs.
    """

    input_keys: list[str] = []
    output_keys: list[str] = []
    for fname, field in signature.iter_dspy_fields().items():
        meta_extra = getattr(field, "json_schema_extra", None) or {}
        role = meta_extra.get("__dspy_field_type", "")
        if role == "input":
            input_keys.append(fname)
        elif role == "output":
            output_keys.append(fname)

    provenance_keys = {"labeled_by", "labeled_at", "label_confidence", "source_trace_id"}
    payload = {k: v for k, v in row.items() if k not in provenance_keys}

    return dspy.Example(**payload).with_inputs(*input_keys)


# -----------------------------------------------------------------------------
# GEPA wrapper
# -----------------------------------------------------------------------------


def _gepa_cls() -> Any:
    """Import-tolerant GEPA accessor."""

    try:
        from dspy.teleprompt import GEPA  # type: ignore[import-not-found]

        return GEPA
    except ImportError:
        pass
    try:
        from dspy import GEPA  # type: ignore[import-not-found,no-redef]

        return GEPA
    except ImportError as exc:  # pragma: no cover - env mismatch
        raise OptimizerError(
            "GEPA optimizer not importable — check dspy-ai >= 2.5.20",
            context={"error": str(exc)},
        ) from exc


# -----------------------------------------------------------------------------
# Harness
# -----------------------------------------------------------------------------


class OptimizerHarness:
    """One per process. Stateless aside from the registry / router handles."""

    def __init__(
        self,
        *,
        registry: Registry | None = None,
        router: Router | None = None,
        settings: Settings | None = None,
    ) -> None:
        self._settings = settings or get_settings()
        self._registry = registry or Registry()
        self._router = router or Router()

    async def compile_signature(
        self,
        signature_name: str,
        *,
        dataset_path: Path | None = None,
        actor: str,
        environment: str = "staging",
    ) -> CompilationResult:
        signature = self._lookup_signature(signature_name)
        meta = signature.meta()
        dataset_path = dataset_path or (
            self._settings.absolute_dataset_dir / f"{signature_name}.jsonl"
        )
        rows = _load_jsonl(dataset_path)
        _validate_provenance(rows, path=dataset_path)
        split = _split_dataset(
            rows,
            holdout_fraction=self._settings.compilation.holdout_fraction,
            seed=self._settings.compilation.seed,
        )

        with correlation_scope(
            signature=signature_name,
            dataset_hash=split.dataset_hash,
            seed=self._settings.compilation.seed,
        ):
            _log.info(
                "compile.start",
                signature=signature_name,
                n_train=len(split.train),
                n_holdout=len(split.holdout),
                dataset_hash=split.dataset_hash,
            )

            with compilation_report(
                signature_name=signature_name,
                dataset_hash=split.dataset_hash,
                seed=self._settings.compilation.seed,
                metadata={
                    "n_train": len(split.train),
                    "n_holdout": len(split.holdout),
                    "actor": actor,
                },
            ):
                candidates = self._router.candidates_cheapest_first(signature_name)
                ladder_results: list[CandidateResult] = []
                accepted: tuple[CandidateResult, dspy.Module, ModelCard] | None = None

                for card in candidates:
                    result, compiled = await asyncio.to_thread(
                        self._run_one_candidate,
                        signature=signature,
                        model_card=card,
                        split=split,
                    )
                    ladder_results.append(result)
                    if result.accepted:
                        accepted = (result, compiled, card)
                        break

                if accepted is None:
                    raise OptimizerError(
                        f"no candidate met threshold for {signature_name}",
                        context={
                            "signature": signature_name,
                            "threshold": meta.accuracy_threshold,
                            "ceiling_eur_per_1k": meta.cost_ceiling_eur_per_1k_calls,
                            "ladder": [c.model for c in candidates],
                        },
                    )

                result, compiled, card = accepted
                prev_record, is_regression = await self._regression_check(
                    signature_name=signature_name,
                    new_score=result.holdout_score,
                    environment=environment,
                )

                if is_regression:
                    raise RegressionError(
                        f"{signature_name} regressed beyond tolerance — halting",
                        context={
                            "new_score": result.holdout_score,
                            "previous_score": prev_record.holdout_accuracy
                            if prev_record
                            else None,
                            "tolerance": self._settings.compilation.regression_tolerance,
                        },
                    )

                artifact = self._materialize_artifact(
                    signature=signature,
                    compiled=compiled,
                    model_card=card,
                    split=split,
                    result=result,
                )
                record = await self._registry.record(artifact, actor=actor)

                _log.info(
                    "compile.success",
                    signature=signature_name,
                    artifact_id=record.id,
                    selected_model=card.id,
                    holdout_score=result.holdout_score,
                    cost_eur_per_call=result.cost_eur_per_call,
                )

                return CompilationResult(
                    signature_name=signature_name,
                    artifact_id=record.id,
                    selected_model=card.id,
                    holdout_accuracy=result.holdout_score,
                    cost_eur_per_call=result.cost_eur_per_call,
                    dataset_hash=split.dataset_hash,
                    candidates=ladder_results,
                    previous_artifact_id=prev_record.id if prev_record else None,
                    previous_holdout_accuracy=prev_record.holdout_accuracy
                    if prev_record
                    else None,
                    is_regression=False,
                    compiled_at=datetime.now(UTC),
                )

    # ------------------------------------------------------------------
    # Inner helpers
    # ------------------------------------------------------------------

    def _lookup_signature(self, signature_name: str) -> type[PutschSignature]:
        try:
            return SIGNATURE_REGISTRY[signature_name]
        except KeyError as exc:
            raise OptimizerError(
                f"unknown signature {signature_name!r}",
                context={"known": sorted(SIGNATURE_REGISTRY)},
            ) from exc

    def _run_one_candidate(
        self,
        *,
        signature: type[PutschSignature],
        model_card: ModelCard,
        split: _SplitDataset,
    ) -> tuple[CandidateResult, dspy.Module]:
        """Run GEPA + evaluate one candidate model. Synchronous — wrapped in ``asyncio.to_thread``."""

        meta = signature.meta()
        configure_dspy(model=model_card.id)
        student = dspy.ChainOfThought(signature)
        gepa = _gepa_cls()(
            metric=lambda ex, pr, trace=None: get_metric(meta.name)(ex, pr).score,
            num_threads=self._settings.compilation.num_threads,
            seed=self._settings.compilation.seed,
        )

        train_examples = [_to_dspy_example(r, signature) for r in split.train]
        holdout_examples = [_to_dspy_example(r, signature) for r in split.holdout]

        try:
            compiled = gepa.compile(student, trainset=train_examples)
        except Exception as exc:  # GEPA can raise many things; coerce to typed error
            raise OptimizerError(
                f"GEPA.compile failed for {meta.name} on {model_card.id}",
                context={
                    "signature": meta.name,
                    "model": model_card.id,
                    "error": str(exc),
                },
            ) from exc

        train_score = _mean_score(self._score_on(compiled, train_examples, meta.name))
        holdout_results = self._score_on(compiled, holdout_examples, meta.name)
        holdout_score = _mean_score(holdout_results)
        holdout_breakdown = _aggregate_breakdown(holdout_results)

        cost = self._estimate_cost(model_card, signature=signature, n_holdout=len(holdout_examples))

        objective = composite_objective(
            accuracy=holdout_score,
            cost_eur_per_call=cost,
            accuracy_threshold=meta.accuracy_threshold,
            cost_ceiling_eur_per_call=meta.cost_ceiling_eur_per_1k_calls / 1000.0,
        )
        accepted = objective > 0.0

        _log.info(
            "compile.candidate",
            signature=meta.name,
            model=model_card.id,
            train_score=train_score,
            holdout_score=holdout_score,
            cost_eur_per_call=cost,
            objective=objective,
            accepted=accepted,
        )

        return (
            CandidateResult(
                model=model_card.id,
                train_score=train_score,
                holdout_score=holdout_score,
                holdout_breakdown=holdout_breakdown,
                cost_eur_per_call=cost,
                objective=objective,
                accepted=accepted,
            ),
            compiled,
        )

    def _score_on(
        self,
        compiled: dspy.Module,
        examples: Sequence[dspy.Example],
        signature_name: str,
    ) -> list[MetricResult]:
        metric = get_metric(signature_name)
        out: list[MetricResult] = []
        for ex in examples:
            try:
                pred = compiled(**ex.inputs())
            except Exception as exc:
                _log.warning(
                    "compile.eval.predict_failed",
                    signature=signature_name,
                    error=str(exc),
                )
                out.append(MetricResult(score=0.0, breakdown={}, notes="predict_failed"))
                continue
            out.append(metric(ex, pred))
        return out

    def _estimate_cost(
        self,
        card: ModelCard,
        *,
        signature: type[PutschSignature],
        n_holdout: int,
    ) -> float:
        """Order-of-magnitude cost-per-call estimate, used as the second optimisation criterion.

        We approximate token counts from the signature's instruction + demos + average dataset row
        size. The real cost is observed at the LiteLLM proxy and reconciled in Langfuse — this is
        the *compile-time* estimate the optimiser uses to rank candidates.
        """

        meta = signature.meta()
        in_tokens_approx = (
            len(meta.instruction) // 4
            + sum(len(orjson.dumps(d.model_dump())) for d in meta.demos) // 4
            + 800  # average input payload in chars/4
        )
        out_tokens_approx = 400  # conservative
        return self._router.estimate_cost_eur_per_call(
            card.id, in_tokens=in_tokens_approx, out_tokens=out_tokens_approx
        )

    async def _regression_check(
        self,
        *,
        signature_name: str,
        new_score: float,
        environment: str,
    ) -> tuple[CompiledArtifactRecord | None, bool]:
        try:
            prev = await self._registry.get_active(signature_name, environment)
        except RegistryError:
            return None, False
        tolerance = self._settings.compilation.regression_tolerance
        is_regression = new_score < (prev.holdout_accuracy - tolerance)
        return prev, is_regression

    def _materialize_artifact(
        self,
        *,
        signature: type[PutschSignature],
        compiled: dspy.Module,
        model_card: ModelCard,
        split: _SplitDataset,
        result: CandidateResult,
    ) -> CompiledArtifact:
        meta = signature.meta()
        instruction, demos = _extract_compiled_artifacts(compiled)
        return CompiledArtifact(
            signature_name=meta.name,
            signature_version=meta.version,
            signature_version_hash=signature.version_hash(),
            model=model_card.id,
            compiled_instruction=instruction,
            compiled_demos=demos,
            optimizer="GEPA",
            optimizer_config={
                "seed": self._settings.compilation.seed,
                "num_threads": self._settings.compilation.num_threads,
                "holdout_fraction": self._settings.compilation.holdout_fraction,
            },
            dataset_hash=split.dataset_hash,
            seed=self._settings.compilation.seed,
            holdout_accuracy=result.holdout_score,
            cost_eur_per_call=result.cost_eur_per_call,
            metadata={
                "tier": model_card.tier.name,
                "train_score": result.train_score,
                "holdout_breakdown": result.holdout_breakdown,
            },
        )


# -----------------------------------------------------------------------------
# Module-private helpers
# -----------------------------------------------------------------------------


def _mean_score(results: Sequence[MetricResult]) -> float:
    if not results:
        return 0.0
    return sum(r.score for r in results) / len(results)


def _aggregate_breakdown(results: Sequence[MetricResult]) -> dict[str, float]:
    bucket: dict[str, list[float]] = {}
    for r in results:
        for k, v in r.breakdown.items():
            bucket.setdefault(k, []).append(v)
    return {k: sum(vs) / len(vs) for k, vs in bucket.items()}


def _extract_compiled_artifacts(
    compiled: dspy.Module,
) -> tuple[str, tuple[OptimizedDemo, ...]]:
    """Inspect a compiled DSPy module and pull out the optimised instruction + demos.

    DSPy's module layout varies slightly across versions; we walk known attribute paths and fall
    back to ``compiled.signature`` for the instruction. If we cannot find demos we return an empty
    tuple — better empty than wrong.
    """

    instruction: str = ""
    raw_demos: list[Any] = []

    for candidate_path in (
        ("predict", "signature", "instructions"),
        ("signature", "instructions"),
        ("predictor", "signature", "instructions"),
    ):
        target: Any = compiled
        ok = True
        for attr in candidate_path:
            target = getattr(target, attr, None)
            if target is None:
                ok = False
                break
        if ok and isinstance(target, str) and target:
            instruction = target
            break

    for demo_path in (
        ("predict", "demos"),
        ("predictor", "demos"),
        ("demos",),
    ):
        target = compiled
        for attr in demo_path:
            target = getattr(target, attr, None)
            if target is None:
                break
        if target is not None:
            raw_demos = list(target)
            break

    demos: list[OptimizedDemo] = []
    for d in raw_demos:
        try:
            inputs = d.inputs() if callable(getattr(d, "inputs", None)) else dict(getattr(d, "inputs", {}))
            outputs = {
                k: v
                for k, v in (d.toDict() if hasattr(d, "toDict") else dict(d)).items()
                if k not in inputs
            }
            demos.append(OptimizedDemo(inputs=dict(inputs), outputs=outputs))
        except Exception:  # pragma: no cover - demo shape varies
            continue

    if not instruction:
        instruction = "(empty — GEPA returned a program without an optimized instruction)"

    return instruction, tuple(demos)
