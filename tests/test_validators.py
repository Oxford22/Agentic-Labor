"""Validator unit tests. Cheap, deterministic, exhaustive."""

from __future__ import annotations

from datetime import date, timedelta
from decimal import Decimal

import pytest

from putsch_docs.validators import (
    amounts_consistent,
    is_plausible_invoice_date,
    is_plausible_leistungsdatum,
    is_valid_iban,
    is_valid_steuernummer,
    is_valid_ustid,
    line_items_sum_to_netto,
    mwst_rate_plausible,
    normalize_iban,
    normalize_ustid,
)


class TestIBAN:
    @pytest.mark.parametrize(
        "iban",
        [
            "DE89370400440532013000",  # Commerzbank reference
            "DE 89 3704 0044 0532 0130 00",
            "FR1420041010050500013M02606",
            "AT611904300234573201",
            "CH9300762011623852957",
        ],
    )
    def test_valid_iban(self, iban: str) -> None:
        assert is_valid_iban(iban)

    @pytest.mark.parametrize(
        "iban",
        [
            "",
            "DE89370400440532013001",  # MOD-97 fails by 1
            "DE0000000000000000",  # length wrong for DE
            "XX89370400440532013000",  # unknown country
            "DE",
            "not-an-iban",
        ],
    )
    def test_invalid_iban(self, iban: str) -> None:
        assert not is_valid_iban(iban)

    def test_normalize_iban_strips_and_uppercases(self) -> None:
        assert (
            normalize_iban("de 89 3704 0044 0532 0130 00")
            == "DE89370400440532013000"
        )


class TestUStID:
    @pytest.mark.parametrize(
        "ustid",
        [
            "DE129273398",  # Siemens AG (real)
            "DE136695976",  # stdnum reference
            "DE811569869",  # real-world valid
            "ATU13585627",
            "FR40303265045",
            "IT00159560366",
        ],
    )
    def test_valid_ustid(self, ustid: str) -> None:
        assert is_valid_ustid(ustid)

    @pytest.mark.parametrize(
        "ustid",
        [
            "",
            "DE12345678",  # too short
            "DE1234567890",  # too long
            "XX123456789",  # unknown country
            "DE000000000",  # leading zero rejected by stdnum
            "DE129273399",  # checksum fail (correct is 8)
        ],
    )
    def test_invalid_ustid(self, ustid: str) -> None:
        assert not is_valid_ustid(ustid)

    def test_normalize_ustid_strips_internal_spaces_and_upper(self) -> None:
        assert normalize_ustid("de 129 273 398") == "DE129273398"

    def test_lowercase_valid_ustid_passes_after_normalize(self) -> None:
        # Our public API normalizes before validating; this is the contract
        # downstream agents rely on (vendors print USt-IdNr in mixed case).
        assert is_valid_ustid("de129273398")


class TestSteuernummer:
    @pytest.mark.parametrize(
        "nr",
        [
            "21/815/08151",
            "151/815/08152",
            "1234567890",
            "12 345 67890",
        ],
    )
    def test_valid(self, nr: str) -> None:
        assert is_valid_steuernummer(nr)

    def test_invalid(self) -> None:
        assert not is_valid_steuernummer("not-a-number")
        assert not is_valid_steuernummer("")


class TestArithmetic:
    def test_consistent_exact(self) -> None:
        assert amounts_consistent(
            Decimal("1000.00"), Decimal("190.00"), Decimal("1190.00")
        )

    def test_consistent_within_tolerance(self) -> None:
        # 1 cent off — within default ±2 cents
        assert amounts_consistent(
            Decimal("1000.00"), Decimal("190.01"), Decimal("1190.00")
        )

    def test_inconsistent_beyond_tolerance(self) -> None:
        assert not amounts_consistent(
            Decimal("1000.00"), Decimal("190.00"), Decimal("1191.00")
        )

    def test_line_items_sum_to_netto(self) -> None:
        assert line_items_sum_to_netto(
            Decimal("1000.00"), Decimal("1000.00")
        )
        assert line_items_sum_to_netto(
            Decimal("999.99"), Decimal("1000.00")
        )
        assert not line_items_sum_to_netto(
            Decimal("950.00"), Decimal("1000.00")
        )


class TestDates:
    def test_plausible_invoice_date(self) -> None:
        today = date(2026, 5, 18)
        assert is_plausible_invoice_date(date(2026, 5, 18), today=today)
        assert is_plausible_invoice_date(date(2026, 5, 1), today=today)
        assert is_plausible_invoice_date(date(2021, 6, 1), today=today)

    def test_implausible_far_future(self) -> None:
        today = date(2026, 5, 18)
        assert not is_plausible_invoice_date(date(2026, 6, 1), today=today)

    def test_implausible_too_old(self) -> None:
        today = date(2026, 5, 18)
        assert not is_plausible_invoice_date(date(2018, 1, 1), today=today)

    def test_leistungsdatum_in_window(self) -> None:
        r = date(2026, 4, 15)
        assert is_plausible_leistungsdatum(r - timedelta(days=20), r)
        assert is_plausible_leistungsdatum(r, r)
        assert is_plausible_leistungsdatum(r + timedelta(days=5), r)  # advance-billing slack

    def test_leistungsdatum_too_far_future(self) -> None:
        r = date(2026, 4, 15)
        assert not is_plausible_leistungsdatum(r + timedelta(days=60), r)


class TestMwstRate:
    @pytest.mark.parametrize("rate", [Decimal("0"), Decimal("7"), Decimal("19")])
    def test_plausible(self, rate: Decimal) -> None:
        assert mwst_rate_plausible(rate)

    @pytest.mark.parametrize("rate", [Decimal("5"), Decimal("16"), Decimal("20")])
    def test_implausible(self, rate: Decimal) -> None:
        assert not mwst_rate_plausible(rate)
