"""German invoice types ("Eingangsrechnung").

Shape lifted from §14 UStG plus the per-field requirements the AP Crew
needs for DATEV booking. The fields here are what ``putsch-docs``
produces and what ``putsch-swarm``'s AP specialist hands to the DATEV
booking step. The same shape feeds the eval set in ``putsch-obs``.
"""

from __future__ import annotations

import re
from datetime import date
from decimal import Decimal
from enum import StrEnum
from typing import Annotated, ClassVar

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    StringConstraints,
    field_validator,
    model_validator,
)


class Currency(StrEnum):
    EUR = "EUR"
    USD = "USD"
    GBP = "GBP"
    CHF = "CHF"


_VatId = Annotated[
    str,
    StringConstraints(
        pattern=r"^[A-Z]{2}[A-Z0-9]{2,12}$",
        strip_whitespace=True,
        to_upper=True,
    ),
]

_Iban = Annotated[
    str,
    StringConstraints(
        pattern=r"^[A-Z]{2}[0-9]{2}[A-Z0-9]{11,30}$",
        strip_whitespace=True,
        to_upper=True,
    ),
]

_Bic = Annotated[
    str,
    StringConstraints(
        pattern=r"^[A-Z]{6}[A-Z0-9]{2}([A-Z0-9]{3})?$",
        strip_whitespace=True,
        to_upper=True,
    ),
]

_InvoiceNumber = Annotated[
    str,
    StringConstraints(min_length=1, max_length=64, strip_whitespace=True),
]


class BankDetails(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    iban: _Iban
    bic: _Bic | None = None
    account_holder: str = Field(min_length=1, max_length=255)

    @field_validator("iban")
    @classmethod
    def _check_iban_mod97(cls, v: str) -> str:
        rearranged = v[4:] + v[:4]
        numeric = "".join(str(int(c, 36)) if c.isalpha() else c for c in rearranged)
        if int(numeric) % 97 != 1:
            raise ValueError("IBAN fails MOD-97 checksum")
        return v


class PaymentTerms(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    net_days: int = Field(ge=0, le=180)
    skonto_percent: Decimal | None = Field(default=None, ge=0, le=Decimal("20"))
    skonto_days: int | None = Field(default=None, ge=0, le=90)

    @model_validator(mode="after")
    def _check_skonto_pair(self) -> PaymentTerms:
        if (self.skonto_percent is None) != (self.skonto_days is None):
            raise ValueError("skonto_percent and skonto_days must be set together or both omitted")
        if self.skonto_days is not None and self.skonto_days > self.net_days:
            raise ValueError("skonto_days must not exceed net_days")
        return self


class PartyAddress(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    name: str = Field(min_length=1, max_length=255)
    street: str = Field(min_length=1, max_length=255)
    postal_code: str = Field(min_length=2, max_length=16)
    city: str = Field(min_length=1, max_length=128)
    country: Annotated[
        str,
        StringConstraints(pattern=r"^[A-Z]{2}$", strip_whitespace=True, to_upper=True),
    ]
    vat_id: _VatId | None = None
    tax_number: str | None = Field(default=None, max_length=32)


class InvoiceLineItem(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    position: int = Field(ge=1)
    description: str = Field(min_length=1, max_length=512)
    quantity: Decimal = Field(gt=0, max_digits=12, decimal_places=4)
    unit: str = Field(min_length=1, max_length=16)
    unit_price_net: Decimal = Field(ge=0, max_digits=14, decimal_places=4)
    line_total_net: Decimal = Field(ge=0, max_digits=14, decimal_places=2)
    vat_rate: Decimal = Field(ge=0, le=Decimal("0.30"), max_digits=4, decimal_places=4)
    vat_amount: Decimal = Field(ge=0, max_digits=14, decimal_places=2)
    line_total_gross: Decimal = Field(ge=0, max_digits=14, decimal_places=2)
    article_number: str | None = Field(default=None, max_length=64)
    hs_code: Annotated[
        str | None,
        StringConstraints(pattern=r"^[0-9]{4,10}$"),
    ] = None

    _ARITHMETIC_TOLERANCE: ClassVar[Decimal] = Decimal("0.02")

    @model_validator(mode="after")
    def _check_arithmetic(self) -> InvoiceLineItem:
        expected_net = (self.quantity * self.unit_price_net).quantize(Decimal("0.01"))
        if abs(expected_net - self.line_total_net) > self._ARITHMETIC_TOLERANCE:
            raise ValueError("line_total_net must equal quantity * unit_price_net within 0.02")
        expected_vat = (self.line_total_net * self.vat_rate).quantize(Decimal("0.01"))
        if abs(expected_vat - self.vat_amount) > self._ARITHMETIC_TOLERANCE:
            raise ValueError("vat_amount must equal line_total_net * vat_rate within 0.02")
        expected_gross = (self.line_total_net + self.vat_amount).quantize(Decimal("0.01"))
        if abs(expected_gross - self.line_total_gross) > self._ARITHMETIC_TOLERANCE:
            raise ValueError("line_total_gross must equal line_total_net + vat_amount within 0.02")
        return self


class InvoiceTotals(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    subtotal_net: Decimal = Field(ge=0, max_digits=16, decimal_places=2)
    vat_total: Decimal = Field(ge=0, max_digits=16, decimal_places=2)
    total_gross: Decimal = Field(ge=0, max_digits=16, decimal_places=2)
    currency: Currency = Currency.EUR

    _TOLERANCE: ClassVar[Decimal] = Decimal("0.02")

    @model_validator(mode="after")
    def _check_sum(self) -> InvoiceTotals:
        if abs((self.subtotal_net + self.vat_total) - self.total_gross) > self._TOLERANCE:
            raise ValueError("total_gross must equal subtotal_net + vat_total within 0.02")
        return self


class Invoice(BaseModel):
    """A real-shaped German Eingangsrechnung.

    What the AP Crew receives from ``putsch-docs`` and what every
    downstream consumer (memory write, DATEV booking, exception
    routing, eval ingest) reads. Validators enforce §14 UStG line-level
    arithmetic and IBAN MOD-97; per-field confidence is *not* part of
    the Invoice — it travels alongside in ``ExtractionResult``.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    invoice_number: _InvoiceNumber
    invoice_date: date
    leistungsdatum: date | None = Field(
        default=None,
        description="Date of supply / service (§14 Abs. 4 UStG)",
    )
    due_date: date | None = None

    supplier: PartyAddress
    customer: PartyAddress

    line_items: list[InvoiceLineItem] = Field(min_length=1, max_length=500)
    totals: InvoiceTotals
    payment_terms: PaymentTerms

    bank_details: BankDetails | None = None
    purchase_order_reference: str | None = Field(default=None, max_length=64)
    delivery_note_reference: str | None = Field(default=None, max_length=64)
    notes: str | None = Field(default=None, max_length=2048)

    _LINE_SUM_TOLERANCE: ClassVar[Decimal] = Decimal("0.05")

    @model_validator(mode="after")
    def _check_invoice_arithmetic(self) -> Invoice:
        if self.leistungsdatum is not None and self.leistungsdatum > self.invoice_date:
            raise ValueError("leistungsdatum cannot be after invoice_date")
        if self.due_date is not None and self.due_date < self.invoice_date:
            raise ValueError("due_date cannot be before invoice_date")

        line_net = sum((item.line_total_net for item in self.line_items), Decimal("0"))
        line_vat = sum((item.vat_amount for item in self.line_items), Decimal("0"))
        if abs(line_net - self.totals.subtotal_net) > self._LINE_SUM_TOLERANCE:
            raise ValueError("sum(line_total_net) must equal totals.subtotal_net within 0.05")
        if abs(line_vat - self.totals.vat_total) > self._LINE_SUM_TOLERANCE:
            raise ValueError("sum(vat_amount) must equal totals.vat_total within 0.05")

        positions = [item.position for item in self.line_items]
        if positions != sorted(set(positions)) or len(positions) != len(set(positions)):
            raise ValueError("line_items must have strictly increasing positions")
        return self

    @field_validator("invoice_number")
    @classmethod
    def _strip_whitespace(cls, v: str) -> str:
        cleaned = re.sub(r"\s+", " ", v).strip()
        if not cleaned:
            raise ValueError("invoice_number must not be empty after stripping")
        return cleaned
