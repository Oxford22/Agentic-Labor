"""Golden-artifact regression tests.

For each signature with a versioned golden artifact under ``tests/golden/``, this test loads the
golden, recomputes the signature's ``version_hash``, and asserts they match.

The intent is *not* to lock the compiled prompt text — that is what GEPA produces and is allowed
to vary. The intent is to lock the *signature's declared identity*: if a signature's fields or
metadata change, the version_hash changes, and the golden goes stale. The CI failure forces a
conscious update — either by recompiling and committing a new golden, or by reverting the
unintended signature change.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from putsch_compile.signatures import SIGNATURE_REGISTRY

_GOLDEN_DIR = Path(__file__).parent / "golden"


def _golden_files() -> list[Path]:
    if not _GOLDEN_DIR.exists():
        return []
    return sorted(_GOLDEN_DIR.glob("*.json"))


def _is_placeholder(payload: dict[str, object]) -> bool:
    """Goldens are bootstrapped on first nightly-compile; until then, skip."""

    return str(payload.get("signature_version_hash", "")).startswith("PLACEHOLDER")


@pytest.mark.golden
@pytest.mark.parametrize("path", _golden_files(), ids=[p.name for p in _golden_files()])
def test_golden_version_hash_matches_signature(path: Path) -> None:
    payload = json.loads(path.read_text(encoding="utf-8"))
    if _is_placeholder(payload):
        pytest.skip(f"placeholder golden {path.name}: nothing to lock yet")
    sig_name = payload["signature_name"]
    sig = SIGNATURE_REGISTRY.get(sig_name)
    assert sig is not None, f"unknown signature {sig_name!r} referenced in {path.name}"
    expected = payload["signature_version_hash"]
    actual = sig.version_hash()
    assert expected == actual, (
        f"{sig_name}: signature changed since golden {path.name} was recorded. "
        f"If the change is intentional, recompile and update the golden; otherwise revert."
    )


@pytest.mark.golden
@pytest.mark.parametrize("path", _golden_files(), ids=[p.name for p in _golden_files()])
def test_golden_pins_an_accuracy_floor(path: Path) -> None:
    payload = json.loads(path.read_text(encoding="utf-8"))
    if _is_placeholder(payload):
        pytest.skip(f"placeholder golden {path.name}: nothing to lock yet")
    sig_name = payload["signature_name"]
    sig = SIGNATURE_REGISTRY[sig_name]
    threshold = sig.meta().accuracy_threshold
    holdout = float(payload["holdout_accuracy"])
    assert holdout >= threshold, (
        f"{sig_name}: golden holdout {holdout:.3f} below current threshold {threshold:.3f}. "
        "Either tighten the metric or recompile and replace the golden."
    )
