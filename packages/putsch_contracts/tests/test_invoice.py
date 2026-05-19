"""Validation tests for the Invoice shape."""

from __future__ import annotations

from datetime import date
from decimal import Decimal

import pytest
from putsch_contracts.invoice import (
    BankDetails,
    Currency,
    Invoice,
    InvoiceLineItem,
    InvoiceTotals,
    PartyAddress,
    PaymentTerms,
)
from pydantic import ValidationError


def _line(
    *,
    position: int = 1,
    quantity: str = "10",
    unit_price_net: str = "100.00",
    vat_rate: str = "0.19",
) -> InvoiceLineItem:
    q = Decimal(quantity)
    upn = Decimal(unit_price_net)
    rate = Decimal(vat_rate)
    line_net = (q * upn).quantize(Decimal("0.01"))
    vat = (line_net * rate).quantize(Decimal("0.01"))
    gross = (line_net + vat).quantize(Decimal("0.01"))
    return InvoiceLineItem(
        position=position,
        description="Test article",
        quantity=q,
        unit="Stk",
        unit_price_net=upn,
        line_total_net=line_net,
        vat_rate=rate,
        vat_amount=vat,
        line_total_gross=gross,
    )


def _addr(*, name: str, country: str = "DE", vat_id: str = "DE123456789") -> PartyAddress:
    return PartyAddress(
        name=name,
        street="Hauptstraße 1",
        postal_code="60311",
        city="Frankfurt",
        country=country,
        vat_id=vat_id,
    )


def _invoice(*, lines: list[InvoiceLineItem]) -> Invoice:
    subtotal_net = sum((line.line_total_net for line in lines), Decimal("0"))
    vat_total = sum((line.vat_amount for line in lines), Decimal("0"))
    gross = subtotal_net + vat_total
    return Invoice(
        invoice_number="RE-2026-00042",
        invoice_date=date(2026, 5, 1),
        leistungsdatum=date(2026, 4, 30),
        due_date=date(2026, 5, 31),
        supplier=_addr(name="Acme Mittelstand GmbH"),
        customer=_addr(name="Putsch GmbH", vat_id="DE987654321"),
        line_items=lines,
        totals=InvoiceTotals(
            subtotal_net=subtotal_net,
            vat_total=vat_total,
            total_gross=gross,
            currency=Currency.EUR,
        ),
        payment_terms=PaymentTerms(net_days=30, skonto_percent=Decimal("2.0"), skonto_days=10),
    )


def test_valid_invoice_round_trips() -> None:
    inv = _invoice(lines=[_line(), _line(position=2, quantity="3", unit_price_net="50.00")])
    assert inv.totals.total_gross == Decimal("1368.50")
    dumped = inv.model_dump_json()
    assert "RE-2026-00042" in dumped


def test_line_arithmetic_violation_rejected() -> None:
    with pytest.raises(ValidationError) as exc:
        InvoiceLineItem(
            position=1,
            description="bad",
            quantity=Decimal("10"),
            unit="Stk",
            unit_price_net=Decimal("100"),
            line_total_net=Decimal("999"),
            vat_rate=Decimal("0.19"),
            vat_amount=Decimal("189.81"),
            line_total_gross=Decimal("1188.81"),
        )
    assert "line_total_net" in str(exc.value)


def test_totals_mismatch_against_line_sum_rejected() -> None:
    lines = [_line(), _line(position=2)]
    subtotal = sum((line.line_total_net for line in lines), Decimal("0"))
    vat = sum((line.vat_amount for line in lines), Decimal("0"))
    with pytest.raises(ValidationError) as exc:
        Invoice(
            invoice_number="RE-1",
            invoice_date=date(2026, 5, 1),
            supplier=_addr(name="S"),
            customer=_addr(name="C", vat_id="DE987654321"),
            line_items=lines,
            totals=InvoiceTotals(
                subtotal_net=subtotal + Decimal("100"),
                vat_total=vat,
                total_gross=subtotal + Decimal("100") + vat,
            ),
            payment_terms=PaymentTerms(net_days=30),
        )
    assert "subtotal_net" in str(exc.value)


def test_iban_mod97_valid() -> None:
    bd = BankDetails(
        iban="DE89370400440532013000",
        bic="COBADEFFXXX",
        account_holder="Acme Mittelstand GmbH",
    )
    assert bd.iban.startswith("DE")


def test_iban_mod97_invalid() -> None:
    with pytest.raises(ValidationError):
        BankDetails(
            iban="DE89370400440532013001",  # last digit flipped
            account_holder="Acme",
        )


def test_skonto_pair_required() -> None:
    with pytest.raises(ValidationError):
        PaymentTerms(net_days=30, skonto_percent=Decimal("2.0"))
    with pytest.raises(ValidationError):
        PaymentTerms(net_days=30, skonto_days=10)


def test_skonto_days_must_not_exceed_net_days() -> None:
    with pytest.raises(ValidationError):
        PaymentTerms(net_days=10, skonto_percent=Decimal("2.0"), skonto_days=20)


def test_leistungsdatum_after_invoice_date_rejected() -> None:
    lines = [_line()]
    subtotal = sum((line.line_total_net for line in lines), Decimal("0"))
    vat = sum((line.vat_amount for line in lines), Decimal("0"))
    with pytest.raises(ValidationError):
        Invoice(
            invoice_number="RE-1",
            invoice_date=date(2026, 5, 1),
            leistungsdatum=date(2026, 5, 2),
            supplier=_addr(name="S"),
            customer=_addr(name="C", vat_id="DE987654321"),
            line_items=lines,
            totals=InvoiceTotals(subtotal_net=subtotal, vat_total=vat, total_gross=subtotal + vat),
            payment_terms=PaymentTerms(net_days=30),
        )


def test_duplicate_positions_rejected() -> None:
    with pytest.raises(ValidationError):
        _invoice(lines=[_line(position=1), _line(position=1)])


def test_invoice_is_frozen() -> None:
    inv = _invoice(lines=[_line()])
    with pytest.raises(ValidationError):
        inv.invoice_number = "RE-NEW"  # type: ignore[misc]
