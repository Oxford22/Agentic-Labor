"""Shared fixtures for the workspace-level integration tests."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from putsch_contracts import Invoice

_FIXTURES = Path(__file__).resolve().parent.parent / "fixtures"


@pytest.fixture(scope="session")
def real_shape_invoice_path() -> Path:
    path = _FIXTURES / "invoice_real_shape.json"
    assert path.exists(), f"fixture missing: {path}"
    return path


@pytest.fixture(scope="session")
def real_shape_invoice(real_shape_invoice_path: Path) -> Invoice:
    """A real-shaped Putsch Eingangsrechnung, validated by ``putsch_contracts``.

    The same JSON is the canonical fixture every module's E2E test must
    parse — this is rule (c) of the merge gate made concrete.
    """
    with real_shape_invoice_path.open(encoding="utf-8") as fh:
        data = json.load(fh)
    return Invoice.model_validate(data)
