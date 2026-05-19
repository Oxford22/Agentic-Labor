"""Hypothesis-based property tests.

Invariants we check across random inputs:
- Every IBAN we accept passes MOD-97.
- Every USt-IdNr we accept matches its country regex AND stdnum's checksum.
- netto + mwst = brutto for every InvoiceFields that constructs successfully.
"""

from __future__ import annotations

from decimal import Decimal

import pytest
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

from putsch_docs.validators import (
    amounts_consistent,
    is_valid_iban,
    is_valid_ustid,
)


@st.composite
def german_iban_strategy(draw: st.DrawFn) -> str:
    """Generate German IBANs with valid MOD-97 by reverse-engineering the check digits."""
    bban = "".join(str(draw(st.integers(0, 9))) for _ in range(18))
    # Build a temp IBAN with "00" as check digits, compute correct check digits
    rearranged = bban + "131400"  # DE → 1314, 00 placeholder
    n = int(rearranged) % 97
    check = 98 - n
    return f"DE{check:02d}{bban}"


@given(german_iban_strategy())
@settings(
    max_examples=200,
    suppress_health_check=[HealthCheck.too_slow],
    deadline=None,
)
def test_generated_german_ibans_pass_validation(iban: str) -> None:
    assert is_valid_iban(iban), f"IBAN should pass: {iban}"


@given(st.text(min_size=15, max_size=34))
@settings(max_examples=200, deadline=None)
def test_random_strings_rarely_pass_iban(s: str) -> None:
    # Vanishingly rare to randomly hit a valid IBAN — we just want non-crash
    is_valid_iban(s)  # must not raise


# USt-IdNr: build DE + 9 digits with correct checksum via stdnum's ISO 7064 mod 11,10.
from stdnum.iso7064 import mod_11_10


@st.composite
def german_ustid_strategy(draw: st.DrawFn) -> str:
    # First digit cannot be 0 (stdnum rule for DE VAT)
    first = str(draw(st.integers(1, 9)))
    rest = "".join(str(draw(st.integers(0, 9))) for _ in range(7))
    digits = first + rest
    return f"DE{digits}{mod_11_10.calc_check_digit(digits)}"


@given(german_ustid_strategy())
@settings(max_examples=200, deadline=None)
def test_generated_german_ustids_pass_validation(ustid: str) -> None:
    assert is_valid_ustid(ustid), f"USt-IdNr should pass: {ustid}"


# Arithmetic invariant


@given(
    netto=st.decimals(
        min_value=Decimal("0"),
        max_value=Decimal("1000000"),
        places=2,
        allow_nan=False,
        allow_infinity=False,
    ),
    rate=st.sampled_from([Decimal("0"), Decimal("7"), Decimal("19")]),
)
@settings(max_examples=200, deadline=None)
def test_arithmetic_consistency_holds_when_computed_exactly(
    netto: Decimal, rate: Decimal
) -> None:
    mwst = (netto * rate / Decimal(100)).quantize(Decimal("0.01"))
    brutto = (netto + mwst).quantize(Decimal("0.01"))
    assert amounts_consistent(netto, mwst, brutto)


@given(
    netto=st.decimals(
        min_value=Decimal("1.00"),
        max_value=Decimal("10000"),
        places=2,
        allow_nan=False,
        allow_infinity=False,
    ),
)
@settings(max_examples=100, deadline=None)
def test_arithmetic_rejects_wrong_brutto(netto: Decimal) -> None:
    mwst = (netto * Decimal("0.19")).quantize(Decimal("0.01"))
    wrong_brutto = (netto + mwst + Decimal("1.00")).quantize(Decimal("0.01"))
    assert not amounts_consistent(netto, mwst, wrong_brutto)
