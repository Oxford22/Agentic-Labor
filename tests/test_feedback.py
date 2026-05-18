"""Annotation feedback loop: pull, validate, dedupe, append, commit."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import orjson
import pytest

from putsch_compile import config as cfg_mod
from putsch_compile.feedback import FeedbackSync, _annotation_to_row, validate_dataset_file


def _ann(
    *,
    trace_id: str,
    labeled_by: str = "s.vogt@putsch.example",
    output: dict[str, Any] | None = None,
    inputs: dict[str, Any] | None = None,
) -> dict[str, Any]:
    return {
        "trace_id": trace_id,
        "labeled_by": labeled_by,
        "labeled_at": datetime.now(UTC),
        "label_confidence": 1.0,
        "payload": output
        or {
            "hs_code": "82075019",
            "confidence": 0.93,
            "rationale": "Kap 82",
            "alternativen": [],
        },
        "input_snapshot": inputs
        or {
            "produkt_beschreibung": "Spiralbohrer 8mm",
            "material": "HSS",
            "verwendung": "Metallbearbeitung",
            "herkunftsland": "DE",
        },
    }


def test_annotation_to_row_happy_path() -> None:
    row, err = _annotation_to_row(_ann(trace_id="t1"), signature_name="classify_hs_code")
    assert err is None
    assert row["labeled_by"] == "s.vogt@putsch.example"
    assert row["source_trace_id"] == "t1"
    assert row["hs_code"] == "82075019"


def test_annotation_rejected_without_email() -> None:
    bad = _ann(trace_id="t1", labeled_by="anon")
    row, err = _annotation_to_row(bad, signature_name="classify_hs_code")
    assert row == {}
    assert err is not None and "labeled_by" in err


def test_annotation_rejected_when_no_output_fields() -> None:
    bad = _ann(trace_id="t1", output={"unrelated_field": "x"})
    row, err = _annotation_to_row(bad, signature_name="classify_hs_code")
    assert row == {}
    assert err is not None


def test_annotation_rejected_without_trace_id() -> None:
    bad = _ann(trace_id="")
    row, err = _annotation_to_row(bad, signature_name="classify_hs_code")
    assert row == {}
    assert err is not None


@pytest.mark.asyncio
async def test_sync_dedupes_existing_trace_ids(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Point repo_root at tmp_path so the dataset write lands in the test sandbox.
    monkeypatch.setenv("PUTSCH_COMPILE_REPO_ROOT", str(tmp_path))
    cfg_mod.get_settings.cache_clear()

    dataset_dir = tmp_path / "evals" / "datasets"
    dataset_dir.mkdir(parents=True)
    dataset_path = dataset_dir / "classify_hs_code.jsonl"
    # One row already present with trace_id "existing".
    dataset_path.write_text(
        orjson.dumps(
            {
                "produkt_beschreibung": "Spiralbohrer",
                "material": "HSS",
                "verwendung": "Metallbearbeitung",
                "herkunftsland": "DE",
                "hs_code": "82075019",
                "confidence": 0.9,
                "rationale": "x",
                "alternativen": [],
                "labeled_by": "r.weiss@putsch.example",
                "labeled_at": "2026-01-01T00:00:00Z",
                "label_confidence": 1.0,
                "source_trace_id": "existing",
            }
        ).decode("utf-8")
        + "\n",
        encoding="utf-8",
    )

    monkeypatch.setattr(
        "putsch_compile.feedback.FeedbackSync._pull_annotations",
        lambda self, name, since: [
            _ann(trace_id="existing"),  # dedup
            _ann(trace_id="new"),       # keep
            _ann(trace_id="new"),       # dedup against itself
        ],
    )
    # Avoid actual git invocation.
    monkeypatch.setattr(
        "putsch_compile.feedback.FeedbackSync._commit_to_git",
        lambda self, **kw: ("auto/feedback/test", "deadbeef"),
    )

    sync = FeedbackSync()
    report = await sync.sync_signature("classify_hs_code")
    assert report.pulled == 3
    assert report.appended == 1
    assert report.duplicates_skipped == 2


def test_validate_dataset_file_accepts_seed_data() -> None:
    path = Path(__file__).resolve().parents[1] / "evals" / "datasets" / "classify_hs_code.jsonl"
    n = validate_dataset_file(path)
    assert n >= 4
