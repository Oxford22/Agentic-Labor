"""DSPy signatures + Pydantic v2 schemas for German Eingangsrechnung extraction.

Schemas are the contract between the document layer and the downstream
agents (Match-Agent, Buchungs-Agent). Every field is typed; every
domain-specific identifier carries a custom validator.

Domain glossary (all required reading for anyone editing this file):
- **Rechnungsnummer**: Invoice number issued by the vendor.
- **Rechnungsdatum**: Invoice date.
- **Leistungsdatum**: Date of service / delivery. §14 Abs. 4 Nr. 6 UStG
  requires it on every invoice >€250 net.
- **Lieferant**: Vendor (= party issuing the invoice).
- **USt-IdNr**: VAT identification number (EU). DE format: DE + 9 digits.
- **Skonto**: Cash discount for early payment. Encoded as percent + days.
- **Bestellnummer**: Customer's purchase order (PO) number — Putsch's SAP PO.
- **Lieferantennummer**: Customer's internal vendor number (Putsch's
  master-data id for the vendor).
- **XRechnung**: Standardized German B2G electronic invoice format
  (EN 16931 compliant XML).
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal
from typing import Annotated, Literal

import dspy
from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    StringConstraints,
    field_validator,
    model_validator,
)

from putsch_docs.validators import (
    amounts_consistent,
    is_valid_iban,
    is_valid_ustid,
    line_items_sum_to_netto,
    normalize_iban,
    normalize_ustid,
)

# ----- Constrained primitives ------------------------------------------------------

USTID = Annotated[
    str,
    StringConstraints(strip_whitespace=True, min_length=4, max_length=20, to_upper=True),
]
IBAN = Annotated[
    str,
    StringConstraints(strip_whitespace=True, min_length=15, max_length=34, to_upper=True),
]
BIC = Annotated[
    str,
    StringConstraints(strip_whitespace=True, min_length=8, max_length=11, to_upper=True),
]
Waehrung = Literal["EUR", "USD", "CHF", "GBP", "PLN", "CZK"]

# Monetary: 2-decimal positive Decimal. We use Decimal end-to-end — never float.
Money = Annotated[Decimal, Field(ge=Decimal("0"), max_digits=14, decimal_places=2)]
MwstRate = Annotated[Decimal, Field(ge=Decimal("0"), le=Decimal("100"), decimal_places=2)]


# ----- Pydantic models -------------------------------------------------------------


class InvoiceLineItem(BaseModel):
    """Single line item on the invoice. Maps 1:1 to DATEV journal entry detail."""

    model_config = ConfigDict(
        extra="forbid",
        frozen=True,
        str_strip_whitespace=True,
    )

    position: int = Field(ge=1, description="Line position (1-indexed).")
    material_nummer: str | None = Field(
        default=None,
        max_length=64,
        description="Vendor's article / material number. Maps to Putsch's MM in SAP.",
    )
    beschreibung: str = Field(min_length=1, max_length=2000)
    menge: Decimal = Field(gt=Decimal("0"), max_digits=12, decimal_places=4)
    einheit: str = Field(
        default="STK",
        max_length=16,
        description="Unit of measure (STK, kg, m, h, ...). DIN 13620 codes preferred.",
    )
    einzelpreis: Money
    gesamtpreis: Money
    mwst_satz: MwstRate = Field(
        description="MwSt rate in percent (e.g., Decimal('19')). Per-line "
        "because mixed-rate invoices are common in B2B."
    )

    @model_validator(mode="after")
    def _check_line_arithmetic(self) -> InvoiceLineItem:
        # einzelpreis * menge ≈ gesamtpreis within 1 cent (rounding tolerance)
        expected = (self.einzelpreis * self.menge).quantize(Decimal("0.01"))
        if (expected - self.gesamtpreis).copy_abs() > Decimal("0.01"):
            msg = (
                f"line {self.position}: einzelpreis*menge "
                f"({expected}) != gesamtpreis ({self.gesamtpreis})"
            )
            raise ValueError(msg)
        return self


class InvoiceFields(BaseModel):
    """Full extracted invoice. The contract returned to the AP Crew.

    Validation is intentionally strict — the AP Crew's exception router
    handles the rejection; downstream agents (DATEV posting, SAP match)
    assume an InvoiceFields instance is structurally sound.
    """

    model_config = ConfigDict(
        extra="forbid",
        frozen=True,
        str_strip_whitespace=True,
    )

    # --- Header ---
    rechnungsnummer: str = Field(min_length=1, max_length=64)
    rechnungsdatum: date
    leistungsdatum: date | None = Field(
        default=None,
        description="Required for invoices >€250 net (§14 Abs. 4 Nr. 6 UStG).",
    )

    # --- Lieferant ---
    lieferant_name: str = Field(min_length=1, max_length=200)
    lieferant_ustid: USTID
    lieferant_address: str = Field(min_length=1, max_length=500)

    # --- Kunde (Putsch) ---
    kunde_ustid: USTID

    # --- Banking ---
    iban: IBAN
    bic: BIC | None = None

    # --- Amounts ---
    netto_betrag: Money
    mwst_satz: MwstRate = Field(description="Headline rate; line items may override.")
    mwst_betrag: Money
    brutto_betrag: Money
    waehrung: Waehrung = "EUR"

    # --- Payment terms ---
    zahlungsziel: int | None = Field(
        default=None, ge=0, le=365, description="Payment term in days."
    )
    skonto_prozent: Decimal | None = Field(
        default=None,
        ge=Decimal("0"),
        le=Decimal("10"),
        max_digits=4,
        decimal_places=2,
    )
    skonto_frist: int | None = Field(default=None, ge=0, le=90)

    # --- References ---
    bestellnummer_ref: str | None = Field(
        default=None,
        max_length=32,
        description="Putsch SAP PO number. Match-Agent uses this for 3-way match.",
    )
    lieferantennummer_ref: str | None = Field(
        default=None,
        max_length=32,
        description="Putsch's master-data vendor id, if quoted on the invoice.",
    )

    # --- Detail ---
    line_items: list[InvoiceLineItem] = Field(default_factory=list, max_length=1000)

    # ---------- Field validators ----------

    @field_validator("lieferant_ustid", "kunde_ustid")
    @classmethod
    def _validate_ustid(cls, v: str) -> str:
        n = normalize_ustid(v)
        if not is_valid_ustid(n):
            msg = f"USt-IdNr failed format / checksum validation: {n[:4]}***"
            raise ValueError(msg)
        return n

    @field_validator("iban")
    @classmethod
    def _validate_iban(cls, v: str) -> str:
        n = normalize_iban(v)
        if not is_valid_iban(n):
            msg = "IBAN failed MOD-97 / country-length validation"
            raise ValueError(msg)
        return n

    # ---------- Cross-field invariants ----------

    @model_validator(mode="after")
    def _check_invoice_arithmetic(self) -> InvoiceFields:
        if not amounts_consistent(self.netto_betrag, self.mwst_betrag, self.brutto_betrag):
            msg = (
                f"netto ({self.netto_betrag}) + mwst ({self.mwst_betrag}) "
                f"!= brutto ({self.brutto_betrag}) within tolerance"
            )
            raise ValueError(msg)
        return self

    @model_validator(mode="after")
    def _check_line_items_sum(self) -> InvoiceFields:
        if not self.line_items:
            return self
        s = sum((li.gesamtpreis for li in self.line_items), Decimal("0"))
        if not line_items_sum_to_netto(s, self.netto_betrag):
            msg = (
                f"sum(line_items.gesamtpreis)={s} != netto_betrag="
                f"{self.netto_betrag} within tolerance"
            )
            raise ValueError(msg)
        return self

    @model_validator(mode="after")
    def _check_leistungsdatum(self) -> InvoiceFields:
        if self.leistungsdatum and self.leistungsdatum > self.rechnungsdatum:
            from datetime import timedelta

            if self.leistungsdatum - self.rechnungsdatum > timedelta(days=30):
                msg = (
                    f"Leistungsdatum ({self.leistungsdatum}) implausibly "
                    f"after Rechnungsdatum ({self.rechnungsdatum})"
                )
                raise ValueError(msg)
        return self

    @model_validator(mode="after")
    def _check_skonto_pair(self) -> InvoiceFields:
        # Skonto only makes sense if both percent and frist are set
        if (self.skonto_prozent is None) ^ (self.skonto_frist is None):
            msg = "skonto_prozent and skonto_frist must both be set or both be None"
            raise ValueError(msg)
        return self


# ----- DSPy signatures -------------------------------------------------------------
#
# All prompts live as DSPy signatures, version-pinned via __module__.__version__.
# A change to a signature requires a new eval run before merge.

SIGNATURE_VERSION = "2026.05.18+v1"


class ExtractInvoiceFromMarkdown(dspy.Signature):
    """Extract structured German invoice fields from Docling-converted markdown.

    The markdown is the structural output of Docling's DocumentConverter: it
    preserves tables (via TableFormer) and document hierarchy. You receive
    one full invoice and must populate every field of the InvoiceFields
    schema that is present in the document.

    Critical rules:
    - All monetary amounts are Decimals with 2 decimal places. Parse German
      number format: '.' as thousands separator, ',' as decimal separator.
      '1.234,56' = 1234.56.
    - Dates must parse from German format (DD.MM.YYYY) into ISO YYYY-MM-DD.
    - USt-IdNr starts with a 2-letter ISO country code. For German vendors:
      'DE' + 9 digits. Strip spaces inside the number.
    - IBAN must include the country prefix (DE, AT, FR, ...) and contain
      no spaces.
    - line_items must capture EVERY positional row in the invoice's table.
      Do not skip rows. Each line's einzelpreis * menge must equal
      gesamtpreis within rounding tolerance.
    - If a field is genuinely absent from the document, return null for
      that field. Do not hallucinate values. Do not infer the
      Lieferantennummer if it is not explicitly written.
    - mwst_satz at the invoice level is the headline rate. If line items
      mix 7% and 19%, return the dominant rate at the header and the
      true rate per line item.

    Critical fields whose accuracy gates downstream agents:
        rechnungsnummer, rechnungsdatum, lieferant_ustid, iban,
        netto_betrag, mwst_betrag, brutto_betrag, line_items
    """

    markdown: str = dspy.InputField(
        desc="Docling DocumentConverter output as markdown, with TableFormer tables intact."
    )
    invoice: InvoiceFields = dspy.OutputField(
        desc="Structured German invoice. Populate every present field; null for absent."
    )


class JudgeCriticalField(dspy.Signature):
    """Second-pass LLM verification of a single critical field.

    You are the judge. The extractor produced a candidate value for a named
    field on a German invoice. You see the surrounding document context (the
    markdown region the extractor pulled from). Decide whether the value is
    plausibly correct given the context.

    Return:
    - agree: True if the value is plausibly correct, False otherwise.
    - confidence: your confidence in your own judgement, 0.0–1.0.
    - reasoning: one short German sentence with the basis for your decision.
    """

    field_name: str = dspy.InputField()
    candidate_value: str = dspy.InputField()
    document_excerpt: str = dspy.InputField(
        desc="The surrounding ~500 chars of document markdown."
    )
    agree: bool = dspy.OutputField()
    confidence: float = dspy.OutputField()
    reasoning: str = dspy.OutputField()
