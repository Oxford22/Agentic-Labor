"""Vendor / customer master data shapes.

These are what ``putsch_memory.tools.lookup_vendor`` returns and what
``putsch-docs`` reconciles its extracted supplier against. Both
identity fields and attribute fields live here, with the deterministic
identity keys (USt-IdNr, IBAN, HRB-Nummer, D-U-N-S) called out.
"""

from __future__ import annotations

from datetime import date
from typing import Annotated

from pydantic import BaseModel, ConfigDict, Field, StringConstraints

from putsch_contracts.invoice import _Iban, _VatId

_HRB = Annotated[
    str,
    StringConstraints(
        pattern=r"^HR[AB]\s?\d{1,7}\s?[A-Z]{0,3}$",
        strip_whitespace=True,
        to_upper=True,
    ),
]

_Duns = Annotated[
    str,
    StringConstraints(pattern=r"^\d{9}$", strip_whitespace=True),
]


class VendorRecord(BaseModel):
    """Supplier-side master data.

    Identity precedence (most-deterministic first): ``vat_id``,
    ``hrb_number``, ``duns``, then composite of (``name`` lowercased +
    ``country``). Embedding-similarity merges require human confirmation
    per ADR-005 and are not modelled here.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    vendor_id: str = Field(min_length=1, max_length=64)
    name: str = Field(min_length=1, max_length=255)
    vat_id: _VatId | None = None
    hrb_number: _HRB | None = None
    duns: _Duns | None = None
    country: Annotated[
        str,
        StringConstraints(pattern=r"^[A-Z]{2}$", strip_whitespace=True, to_upper=True),
    ]
    iban: _Iban | None = None
    default_payment_terms_net_days: int = Field(ge=0, le=180, default=30)
    is_kleinunternehmer: bool = False
    valid_from: date
    valid_to: date | None = None


class CustomerRecord(BaseModel):
    """Customer-side master data; same identity rules as VendorRecord."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    customer_id: str = Field(min_length=1, max_length=64)
    name: str = Field(min_length=1, max_length=255)
    vat_id: _VatId | None = None
    hrb_number: _HRB | None = None
    duns: _Duns | None = None
    country: Annotated[
        str,
        StringConstraints(pattern=r"^[A-Z]{2}$", strip_whitespace=True, to_upper=True),
    ]
    credit_limit_eur: int | None = Field(default=None, ge=0)
    valid_from: date
    valid_to: date | None = None


class AccountRouting(BaseModel):
    """DATEV chart-of-accounts assignment for a vendor/customer pair.

    Used by ``putsch_compile`` signature ``datev_account_assignment`` and
    by ``putsch_memory.lookup_account_routing``. The four-digit SKR
    account is the binding key.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    vendor_id: str = Field(min_length=1, max_length=64)
    expense_account: Annotated[
        str,
        StringConstraints(pattern=r"^\d{4,8}$", strip_whitespace=True),
    ]
    cost_center: str | None = Field(default=None, max_length=16)
    tax_key: Annotated[
        str,
        StringConstraints(pattern=r"^[A-Z0-9]{1,4}$", strip_whitespace=True),
    ]
    chart_of_accounts: str = Field(default="SKR03", pattern=r"^SKR(03|04)$")
    valid_from: date
    valid_to: date | None = None
