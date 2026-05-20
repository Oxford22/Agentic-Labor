"""Hypothesis-driven property tests.

The two invariants we care about most:

1. **Idempotency**: redact(redact(x)) == redact(x). Re-redacting an already
   redacted string must not produce new hits, change the token wrappers,
   or leak content.
2. **No raw IBAN/USt-IdNr ever survives**. Generated patterns must always
   be replaced.
3. **No false negatives on the deterministic patterns**: across a wide
   string-space, any string containing a generated pattern must register
   at least one hit.
"""

from __future__ import annotations

import pytest
from hypothesis import HealthCheck, given, settings, strategies as st

from putsch_obs.redaction import RedactionEngine


@pytest.fixture()
def engine(memory_vault):  # type: ignore[no-untyped-def]
    return RedactionEngine(vault=memory_vault)


# DE IBAN with formatted spacing. We avoid mod-97 here — the redactor
# matches by shape, so the strategy is shape-only.
_iban_block = st.text(alphabet="0123456789", min_size=4, max_size=4)


@st.composite
def _de_iban(draw: st.DrawFn, spaces: bool = True) -> str:
    blocks = [draw(_iban_block) for _ in range(4)]
    tail = draw(st.text(alphabet="0123456789", min_size=2, max_size=2))
    body = (" " if spaces else "").join(blocks)
    body = f"DE{draw(_iban_block)[:2]}{(' ' if spaces else '')}{body}"
    return f"{body}{(' ' if spaces else '')}{tail}"


_ust = st.from_regex(r"DE\d{9}", fullmatch=True)
_email = st.from_regex(
    r"[a-z]{2,8}@[a-z]{2,8}\.[a-z]{2,4}",
    fullmatch=True,
)
_phone = st.from_regex(r"\+49\d{6,12}", fullmatch=True)


# A function-scoped fixture is intentional here: the RedactionEngine is
# cheap to build, and re-using it across generated inputs is fine because
# redaction is stateless (the vault is per-call). Hypothesis warns about
# function-scoped fixtures because they are not reset between generated
# inputs; we suppress that health check explicitly to make the choice
# auditable in code review rather than implicit.
_SUPPRESSED = [HealthCheck.too_slow, HealthCheck.function_scoped_fixture]


@given(payload=st.one_of(_de_iban(), _de_iban(spaces=False), _ust, _email, _phone))
@settings(max_examples=200, suppress_health_check=_SUPPRESSED)
def test_generated_pii_is_always_redacted(
    engine: RedactionEngine, payload: str
) -> None:
    text = f"prefix-text {payload} more-text"
    res = engine.redact(text)
    assert res.deterministic_hits >= 1, (
        f"missed: {payload!r} in: {text!r}"
    )
    assert payload not in res.redacted


@given(text=st.text(min_size=0, max_size=400))
@settings(max_examples=200, suppress_health_check=_SUPPRESSED)
def test_idempotency(engine: RedactionEngine, text: str) -> None:
    once = engine.redact(text)
    twice = engine.redact(once.redacted)
    # Re-running on the already-redacted output may produce additional
    # hits ONLY if the generated tokens themselves accidentally look like
    # PII. We forbid that: the wrapper format guarantees it won't.
    assert twice.deterministic_hits == 0, (
        f"second pass produced new hits — wrappers leaked. "
        f"once={once.redacted!r} twice.mapping={dict(twice.mapping)}"
    )
    # And the output must be byte-identical on the second pass.
    assert twice.redacted == once.redacted


@given(text=st.text(min_size=0, max_size=200))
@settings(max_examples=100, suppress_health_check=_SUPPRESSED)
def test_attrs_passthrough_for_non_strings(
    engine: RedactionEngine, text: str
) -> None:
    attrs = {
        "input.value": text,
        "gen_ai.usage.input_tokens": 12,
        "putsch.cache_hit": True,
        "putsch.latency_ms": 4.5,
    }
    out = engine.redact_attrs(attrs)
    assert out["gen_ai.usage.input_tokens"] == 12
    assert out["putsch.cache_hit"] is True
    assert out["putsch.latency_ms"] == 4.5
"""Hypothesis-driven property tests.

The two invariants we care about most:

1. **Idempotency**: redact(redact(x)) == redact(x). Re-redacting an already
   redacted string must not produce new hits, change the token wrappers,
   or leak content.
2. **No raw IBAN/USt-IdNr ever survives**. Generated patterns must always
   be replaced.
3. **No false negatives on the deterministic patterns**: across a wide
   string-space, any string containing a generated pattern must register
   at least one hit.
"""

from __future__ import annotations

import pytest
from hypothesis import HealthCheck, given, settings, strategies as st

from putsch_obs.redaction import RedactionEngine


@pytest.fixture()
def engine(memory_vault):  # type: ignore[no-untyped-def]
    return RedactionEngine(vault=memory_vault)


# DE IBAN with formatted spacing. We avoid mod-97 here — the redactor
# matches by shape, so the strategy is shape-only.
_iban_block = st.text(alphabet="0123456789", min_size=4, max_size=4)


@st.composite
def _de_iban(draw: st.DrawFn, spaces: bool = True) -> str:
    blocks = [draw(_iban_block) for _ in range(4)]
    tail = draw(st.text(alphabet="0123456789", min_size=2, max_size=2))
    body = (" " if spaces else "").join(blocks)
    body = f"DE{draw(_iban_block)[:2]}{(' ' if spaces else '')}{body}"
    return f"{body}{(' ' if spaces else '')}{tail}"


_ust = st.from_regex(r"DE\d{9}", fullmatch=True)
_email = st.from_regex(
    r"[a-z]{2,8}@[a-z]{2,8}\.[a-z]{2,4}",
    fullmatch=True,
)
_phone = st.from_regex(r"\+49\d{6,12}", fullmatch=True)


@given(payload=st.one_of(_de_iban(), _de_iban(spaces=False), _ust, _email, _phone))
@settings(max_examples=200, suppress_health_check=[HealthCheck.too_slow])
def test_generated_pii_is_always_redacted(
    engine: RedactionEngine, payload: str
) -> None:
    text = f"prefix-text {payload} more-text"
    res = engine.redact(text)
    assert res.deterministic_hits >= 1, (
        f"missed: {payload!r} in: {text!r}"
    )
    assert payload not in res.redacted


@given(text=st.text(min_size=0, max_size=400))
@settings(max_examples=200, suppress_health_check=[HealthCheck.too_slow])
def test_idempotency(engine: RedactionEngine, text: str) -> None:
    once = engine.redact(text)
    twice = engine.redact(once.redacted)
    # Re-running on the already-redacted output may produce additional
    # hits ONLY if the generated tokens themselves accidentally look like
    # PII. We forbid that: the wrapper format guarantees it won't.
    assert twice.deterministic_hits == 0, (
        f"second pass produced new hits — wrappers leaked. "
        f"once={once.redacted!r} twice.mapping={dict(twice.mapping)}"
    )
    # And the output must be byte-identical on the second pass.
    assert twice.redacted == once.redacted


@given(text=st.text(min_size=0, max_size=200))
@settings(max_examples=100)
def test_attrs_passthrough_for_non_strings(
    engine: RedactionEngine, text: str
) -> None:
    attrs = {
        "input.value": text,
        "gen_ai.usage.input_tokens": 12,
        "putsch.cache_hit": True,
        "putsch.latency_ms": 4.5,
    }
    out = engine.redact_attrs(attrs)
    assert out["gen_ai.usage.input_tokens"] == 12
    assert out["putsch.cache_hit"] is True
    assert out["putsch.latency_ms"] == 4.5
