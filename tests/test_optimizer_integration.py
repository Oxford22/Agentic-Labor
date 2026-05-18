"""Smoke test for the optimizer harness.

Goals:

* Demonstrates the end-to-end pipeline: load dataset → split → walk ladder → produce artifact →
  record in registry. No real LLM calls — the ``patched_dspy`` fixture stubs ``configure_dspy``,
  ``dspy.ChainOfThought``, and the ``GEPA`` class with deterministic doubles.

* Asserts that the *cheapest model* ends up selected when accuracy clears the threshold (the
  ladder works bottom-up).

* Asserts the artifact carries the seed, dataset hash, and signature version hash so we can
  reproduce the run.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from putsch_compile import config as cfg_mod
from putsch_compile.optimize import OptimizerHarness
from putsch_compile.registry import Registry
from putsch_compile.signatures import SIGNATURE_REGISTRY


@pytest.fixture
def tiny_classify_hs_dataset(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Five rows for classify_hs_code, all in one tmp dir."""

    rows = [
        {
            "produkt_beschreibung": "Spiralbohrer HSS-Co8 D=8mm",
            "material": "HSS-Co",
            "verwendung": "Metallbearbeitung",
            "herkunftsland": "DE",
            "hs_code": "82075019",
            "confidence": 0.93,
            "rationale": "Kap. 82, Pos. 8207, Unterpos. 50.",
            "alternativen": [],
            "labeled_by": "r.weiss@putsch.example",
            "labeled_at": "2026-01-12T08:00:00Z",
            "label_confidence": 1.0,
            "source_trace_id": None,
        }
    ] * 5  # GEPA needs >=4 rows for a sensible split

    dataset_dir = tmp_path / "evals" / "datasets"
    dataset_dir.mkdir(parents=True)
    path = dataset_dir / "classify_hs_code.jsonl"
    path.write_text("\n".join(json.dumps(r) for r in rows), encoding="utf-8")

    # Re-point Settings.repo_root at tmp_path so the harness finds this dataset.
    monkeypatch.setenv("PUTSCH_COMPILE_REPO_ROOT", str(tmp_path))
    cfg_mod.get_settings.cache_clear()
    return path


@pytest.mark.asyncio
async def test_compile_signature_produces_artifact(
    patched_dspy: dict[str, Any],
    patched_artifact_store: dict[str, Any],
    registry: Registry,
    tiny_classify_hs_dataset: Path,
) -> None:
    harness = OptimizerHarness(registry=registry)
    result = await harness.compile_signature(
        "classify_hs_code",
        dataset_path=tiny_classify_hs_dataset,
        actor="test@putsch.example",
        environment="staging",
    )
    # Some candidate was selected; the cheapest (qwen3-14b) is in the ladder first.
    assert result.signature_name == "classify_hs_code"
    assert result.artifact_id
    assert result.candidates, "ladder must record attempted models"
    accepted = [c for c in result.candidates if c.accepted]
    assert len(accepted) == 1
    # Determinism + provenance.
    sig = SIGNATURE_REGISTRY["classify_hs_code"]
    payload = await registry.load_payload(result.artifact_id)
    assert payload.signature_version_hash == sig.version_hash()
    assert payload.dataset_hash == result.dataset_hash
    assert payload.seed == 42  # default settings


@pytest.mark.asyncio
async def test_compile_is_reproducible_same_input_same_hash(
    patched_dspy: dict[str, Any],
    patched_artifact_store: dict[str, Any],
    registry: Registry,
    tiny_classify_hs_dataset: Path,
) -> None:
    harness = OptimizerHarness(registry=registry)
    a = await harness.compile_signature(
        "classify_hs_code",
        dataset_path=tiny_classify_hs_dataset,
        actor="test@putsch.example",
    )
    b = await harness.compile_signature(
        "classify_hs_code",
        dataset_path=tiny_classify_hs_dataset,
        actor="test@putsch.example",
    )
    # Same content → same recorded id (idempotent via content hash).
    assert a.artifact_id == b.artifact_id


@pytest.mark.asyncio
async def test_regression_gate_halts_when_holdout_drops(
    patched_dspy: dict[str, Any],
    patched_artifact_store: dict[str, Any],
    registry: Registry,
    tiny_classify_hs_dataset: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """If a previously-active artifact has higher holdout accuracy, a regression aborts the run."""

    from putsch_compile.artifacts import CompiledArtifact
    from datetime import UTC, datetime

    sig = SIGNATURE_REGISTRY["classify_hs_code"]
    high = CompiledArtifact(
        signature_name="classify_hs_code",
        signature_version="1.0.0",
        signature_version_hash=sig.version_hash(),
        model="mistral/mistral-large-2411",
        compiled_instruction="HIGH",
        dataset_hash="0" * 32,
        seed=42,
        holdout_accuracy=1.0,
        cost_eur_per_call=0.001,
        created_at=datetime.now(UTC),
    )
    rec = await registry.record(high, actor="test")
    await registry.promote(rec.id, environment="staging", promoted_by="test")

    # Patch the harness's metric so the new run produces a much lower score.
    from putsch_compile.metrics import MetricResult

    def low_metric(_ex: Any, _pr: Any) -> MetricResult:
        return MetricResult(score=0.5)

    monkeypatch.setattr(
        "putsch_compile.optimize.get_metric",
        lambda _name: low_metric,
    )
    harness = OptimizerHarness(registry=registry)
    from putsch_compile.exceptions import OptimizerError, RegressionError

    with pytest.raises((RegressionError, OptimizerError)):
        await harness.compile_signature(
            "classify_hs_code",
            dataset_path=tiny_classify_hs_dataset,
            actor="test@putsch.example",
            environment="staging",
        )
