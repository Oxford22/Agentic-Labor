"""Deterministic redaction tests, including the 50-item PII corpus."""

from __future__ import annotations

import re

import pytest

from putsch_obs.config import RedactionMode
from putsch_obs.redaction import (
    PIICategory,
    RedactionEngine,
    _iban_is_valid,
)
from tests.fixtures.pii_corpus import PII_FIXTURES


@pytest.fixture()
def engine(memory_vault):  # type: ignore[no-untyped-def]
    return RedactionEngine(vault=memory_vault)


@pytest.mark.parametrize("case", [c for c in PII_FIXTURES if c.raw])
def test_pii_corpus_redaction(engine: RedactionEngine, case) -> None:  # type: ignore[no-untyped-def]
    result = engine.redact(case.raw)
    if case.present:
        assert result.deterministic_hits >= 1, (
            f"missed PII in: {case.raw!r} (expected category={case.expected_category})"
        )
        # Token wrappers must replace the raw value.
        assert "<<PII:" in result.redacted
        # The expected category must appear in the mapping. Dense strings
        # may produce additional categories (e.g. an IBAN next to a USt-IdNr);
        # we only require the expected one to be present.
        categories = {cat.value for _, (cat, _) in result.mapping.items()}
        assert case.expected_category in categories, (
            f"expected category {case.expected_category!r} not in detected "
            f"categories {categories} for input {case.raw!r}"
        )
        for token in result.mapping:
            assert token in result.redacted
    else:
        # NEGATIVE: must not produce ANY redaction unless we explicitly
        # accept a known false-positive in the corpus.
        if case.expected_category is None:
            assert result.deterministic_hits == 0, (
                f"unexpected redaction of {case.raw!r}: "
                f"hits={result.deterministic_hits} mapping={dict(result.mapping)}"
            )


def test_iban_mod97() -> None:
    assert _iban_is_valid("DE89 3704 0044 0532 0130 00")
    assert _iban_is_valid("DE89370400440532013000")
    assert not _iban_is_valid("DE00 0000 0000 0000 0000 00")
    assert not _iban_is_valid("AT61 1904 3002 3457 3201")


def test_no_double_substitution(engine: RedactionEngine) -> None:
    text = "Tax: DE123456789 IBAN DE89370400440532013000"
    result = engine.redact(text)
    # Ensure no token-wrapper substring shows up un-wrapped.
    assert "DE123456789" not in result.redacted
    assert "DE89370400440532013000" not in result.redacted
    # Token wrappers themselves must not contain raw digits from a category
    # they don't represent.
    for token in result.mapping:
        assert re.fullmatch(r"[A-Za-z0-9_-]+", token)


def test_redact_attrs_passes_allowlist(engine: RedactionEngine) -> None:
    attrs = {
        "gen_ai.request.model": "mistral-large-latest",
        "gen_ai.usage.input_tokens": 42,
        "input.value": "Bitte an hans@putsch.de schicken.",
        "putsch.routing.decision": "fastpath",
    }
    out = engine.redact_attrs(attrs)
    assert out["gen_ai.request.model"] == "mistral-large-latest"
    assert out["gen_ai.usage.input_tokens"] == 42
    assert "hans@putsch.de" not in out["input.value"]
    assert "<<PII:email:" in out["input.value"]


def test_redact_mode_off(monkeypatch: pytest.MonkeyPatch, memory_vault) -> None:  # type: ignore[no-untyped-def]
    from putsch_obs.config import PutschObsSettings

    cfg = PutschObsSettings(redaction_mode=RedactionMode.OFF)
    eng = RedactionEngine(settings=cfg, vault=memory_vault)
    res = eng.redact("DE89 3704 0044 0532 0130 00")
    assert res.deterministic_hits == 0
    assert "DE89" in res.redacted


def test_vault_records_each_token(engine: RedactionEngine, memory_vault) -> None:  # type: ignore[no-untyped-def]
    engine.redact("DE123456789 und DE89370400440532013000")
    # 2 distinct tokens stored.
    assert len(memory_vault.store_calls) == 2
    cats = {call[1] for call in memory_vault.store_calls}
    assert cats == {"ust_id", "iban"}
