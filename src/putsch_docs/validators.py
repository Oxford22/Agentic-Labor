"""German invoice format & structural validators.

Cheap, deterministic, high-signal. Run them all on every extraction — they
catch model hallucinations the model itself reports as high-confidence.

Domain notes:
- **USt-IdNr** (Umsatzsteuer-Identifikationsnummer): EU VAT identification.
  ISO country prefix + country-specific format. DE = 9 digits.
- **Steuernummer**: German tax number issued by the Finanzamt, distinct
  from USt-IdNr. Format varies by Bundesland.
- **IBAN**: International Bank Account Number. MOD-97 checksum (ISO 13616).
- **Leistungsdatum**: Date the service/goods were rendered. Required on
  every German invoice >€250 net (§14 Abs. 4 Nr. 6 UStG). Must precede or
  equal Rechnungsdatum, except for advance billing.
- **Skonto**: Early-payment discount. Typically 2-3% within 14 days.
"""

from __future__ import annotations

import re
from datetime import date, timedelta
from decimal import Decimal
from typing import Final

from stdnum import iban as stdnum_iban
from stdnum.eu import vat as stdnum_vat
from stdnum.exceptions import ValidationError as StdnumValidationError

# Country-specific USt-IdNr regexes. stdnum.eu.vat validates the full set;
# we keep a narrowed map for the countries Putsch transacts with for
# fast pre-filtering before the heavy validation pass.
USTID_PATTERNS: Final[dict[str, re.Pattern[str]]] = {
    "DE": re.compile(r"^DE\d{9}$"),
    "AT": re.compile(r"^ATU\d{8}$"),
    "FR": re.compile(r"^FR[A-HJ-NP-Z0-9]{2}\d{9}$"),
    "IT": re.compile(r"^IT\d{11}$"),
    "ES": re.compile(r"^ES[A-Z0-9]\d{7}[A-Z0-9]$"),
    "NL": re.compile(r"^NL\d{9}B\d{2}$"),
    "BE": re.compile(r"^BE0\d{9}$"),
    "PL": re.compile(r"^PL\d{10}$"),
    "CH": re.compile(r"^CHE\d{9}(MWST|TVA|IVA)?$"),  # not EU but common DACH counterparty
    "GB": re.compile(r"^GB(\d{9}|\d{12}|GD\d{3}|HA\d{3})$"),
}

# Steuernummer — varies by Bundesland; canonical short patterns:
STEUERNR_RE: Final[re.Pattern[str]] = re.compile(
    r"^\d{2,3}[/ ]\d{2,4}[/ ]\d{4,5}$|^\d{10,13}$"
)


# ----- IBAN ------------------------------------------------------------------------


def normalize_iban(value: str) -> str:
    """Strip spaces, uppercase. Does not validate."""
    return value.replace(" ", "").upper()


def is_valid_iban(value: str) -> bool:
    """MOD-97 (ISO 13616) plus country length table."""
    try:
        stdnum_iban.validate(normalize_iban(value))
    except StdnumValidationError:
        return False
    return True


# ----- USt-IdNr / VAT --------------------------------------------------------------


def normalize_ustid(value: str) -> str:
    """Strip whitespace, uppercase, collapse internal spaces."""
    return re.sub(r"\s+", "", value).upper()


def ustid_country(value: str) -> str | None:
    n = normalize_ustid(value)
    if len(n) < 2 or not n[:2].isalpha():
        return None
    return n[:2]


def is_valid_ustid(value: str) -> bool:
    """Country-format match plus stdnum's checksum validation."""
    n = normalize_ustid(value)
    country = ustid_country(n)
    if country is None:
        return False
    pat = USTID_PATTERNS.get(country)
    if pat is not None and not pat.match(n):
        return False
    try:
        stdnum_vat.validate(n)
    except StdnumValidationError:
        return False
    return True


# ----- Steuernummer ----------------------------------------------------------------


def is_valid_steuernummer(value: str) -> bool:
    return bool(STEUERNR_RE.match(value.strip()))


# ----- Date plausibility -----------------------------------------------------------


# B2B invoices typically have Leistungsdatum within ±30 days of Rechnungsdatum.
# Outside this window we flag for human review — extraction is likely wrong
# (e.g., picked up an order date instead of the service date).
LEISTUNGSDATUM_MAX_DELTA: Final[timedelta] = timedelta(days=30)
LEISTUNGSDATUM_PAST_MAX: Final[timedelta] = timedelta(days=365)


def is_plausible_leistungsdatum(
    leistungsdatum: date, rechnungsdatum: date
) -> bool:
    """Leistungsdatum must be <= Rechnungsdatum + 30 days and >= one year before."""
    if leistungsdatum > rechnungsdatum + LEISTUNGSDATUM_MAX_DELTA:
        return False
    return not leistungsdatum < rechnungsdatum - LEISTUNGSDATUM_PAST_MAX


def is_plausible_invoice_date(rechnungsdatum: date, *, today: date | None = None) -> bool:
    """Reject invoices dated more than 7 days in the future or 5 years in the past."""
    today = today or date.today()
    if rechnungsdatum > today + timedelta(days=7):
        return False
    return rechnungsdatum >= today - timedelta(days=365 * 5)


# ----- Arithmetic consistency ------------------------------------------------------


def amounts_consistent(
    netto: Decimal,
    mwst: Decimal,
    brutto: Decimal,
    *,
    tolerance_cents: int = 2,
) -> bool:
    """netto + mwst == brutto within ±tolerance_cents cents (default ±€0.02)."""
    diff = (netto + mwst - brutto).copy_abs()
    tolerance = Decimal(tolerance_cents) / Decimal(100)
    return diff <= tolerance


def line_items_sum_to_netto(
    line_total_sum: Decimal,
    netto: Decimal,
    *,
    tolerance_cents: int = 2,
) -> bool:
    """Sum of line item gesamtpreis ≈ invoice netto_betrag.

    Tolerance is wider than per-line because of rounding cascade.
    """
    diff = (line_total_sum - netto).copy_abs()
    tolerance = Decimal(tolerance_cents) / Decimal(100)
    return diff <= tolerance


def mwst_rate_plausible(rate: Decimal) -> bool:
    """German MwSt rates: 0% (reverse-charge), 7% (reduced), 19% (standard)."""
    return rate in {Decimal("0"), Decimal("7"), Decimal("19")}
