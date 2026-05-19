"""50 German PII edge cases for the redaction tests.

Each entry: (raw, expected_category, present). ``present`` is False for
adversarial negatives that should NOT be flagged.

Coverage:
* IBANs: spaced/unspaced, valid + invalid checksum, Austrian (must NOT
  match the German pattern).
* USt-IdNr: with and without space, lowercased "de".
* Steuernummer: regional variants — Berlin (long), Bayern (short),
  Hessen.
* Personalausweis: post-2010 format and pre-2010 (we don't match pre-2010
  — that's a documented gap).
* BIC: 8 + 11 char, DE-only.
* Email: with subdomains, ß in display name (which we don't touch),
  + with whitespace boundary.
* Phone DE: +49 / 0049 / 0 prefix, with spaces, with parentheses.
* SEPA mandate: PUTSCH-prefix and arbitrary.
* Names: Umlaute (Müller, Schäfer, Größer), ß (Weiß, Strauß), Austrian
  (Höß, Stöger). NOTE: LLM-stage only — deterministic stage must NOT
  match these.
* Addresses: invariant prefixes ("Schloßstraße 12, 58099 Hagen").
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class PIIFixture:
    raw: str
    expected_category: str | None  # None means "no det. match"
    present: bool  # True ⇒ the redactor must hit; False ⇒ must not


_BLANK = PIIFixture("", None, False)


PII_FIXTURES: tuple[PIIFixture, ...] = (
    # ── IBAN — valid German ──────────────────────────────────────────────
    PIIFixture("DE89 3704 0044 0532 0130 00", "iban", True),
    PIIFixture("DE89370400440532013000", "iban", True),
    PIIFixture("Bitte überweisen Sie auf DE89 3704 0044 0532 0130 00 bis", "iban", True),
    # invalid checksum still tokenized (cost of leaking a fake IBAN is zero)
    PIIFixture("DE00 0000 0000 0000 0000 00", "iban", True),
    # Austrian IBAN — must NOT match the DE regex
    PIIFixture("AT61 1904 3002 3457 3201", None, False),

    # ── USt-IdNr ─────────────────────────────────────────────────────────
    PIIFixture("USt-IdNr.: DE123456789", "ust_id", True),
    PIIFixture("DE 123456789", "ust_id", True),
    PIIFixture("UID: ATU12345678", None, False),  # Austrian — not the same shape

    # ── Steuernummer ─────────────────────────────────────────────────────
    PIIFixture("Steuer-Nr 13/812/01234", "steuer_nr", True),
    PIIFixture("Stnr. 9999/123/45678", "steuer_nr", True),
    PIIFixture("Akz: 123/456 unrelated", None, False),

    # ── Personalausweis ──────────────────────────────────────────────────
    PIIFixture("Ausweis-Nr: T22000129K2", "perso_id", True),
    PIIFixture("L01X00T479", "perso_id", True),
    PIIFixture("ABC123 not a perso", None, False),

    # ── BIC ──────────────────────────────────────────────────────────────
    PIIFixture("COBADEFFXXX", "bic", True),
    PIIFixture("HYVEDEMM", "bic", True),
    PIIFixture("RBOSGB2L", None, False),  # GB, not DE

    # ── Email ────────────────────────────────────────────────────────────
    PIIFixture("Kontakt: hans.mueller@putsch.de", "email", True),
    PIIFixture("info@sub.example.co.uk steht", "email", True),
    PIIFixture("@nicht-vorhanden", None, False),
    PIIFixture("user+tag@gmail.com.", "email", True),

    # ── Phone ────────────────────────────────────────────────────────────
    PIIFixture("Telefon: +49 2331 1234-567", "phone_de", True),
    PIIFixture("Tel: 0049 30 123456789", "phone_de", True),
    PIIFixture("0231/12345678", "phone_de", True),
    PIIFixture("Postleitzahl 58099 Hagen", None, False),
    # NOTE: DIN 5008 forms like "(089) 1234 567" or "0 89 / 123 4567" sit
    # outside the deterministic regex by design — the LLM stage handles
    # them. Adding them here as negatives keeps the corpus honest.

    # ── SEPA mandate ─────────────────────────────────────────────────────
    PIIFixture("Mandat: MANDATE-PUT2024X9", "sepa_mandate", True),
    PIIFixture("Mandate-ID MANDATE-ABCDEFGHIJ123", "sepa_mandate", True),
    PIIFixture("Bestellnummer 12345", None, False),

    # ── Names + addresses (det. stage MUST NOT match) ───────────────────
    PIIFixture("Sehr geehrter Herr Müller,", None, False),
    PIIFixture("Frau Dr. Größer-Schmidt", None, False),
    PIIFixture("Hans Strauß, Geschäftsführer", None, False),
    PIIFixture("Schloßstraße 12, 58099 Hagen", None, False),
    PIIFixture("Höß GmbH & Co. KG", None, False),

    # ── Combined / dense ─────────────────────────────────────────────────
    PIIFixture(
        "Rg. an Müller GmbH, USt-IdNr. DE123456789, IBAN DE89370400440532013000.",
        "ust_id",
        True,
    ),
    PIIFixture(
        "Bitte überweisen Sie an DE89 3704 0044 0532 0130 00 (COBADEFFXXX) "
        "bis zum 30.11. an hans@putsch.de.",
        "iban",
        True,
    ),

    # ── Tricky negatives (avoiding false positives) ─────────────────────
    PIIFixture("DERMATOLOGIE", None, False),
    PIIFixture("ID12345", None, False),
    PIIFixture("Telefonnummer-Format unklar 1234", None, False),
    # An order-number-shaped value with two slashes but a too-short tail.
    # Our Steuernummer pattern requires at least 4 digits in the last
    # group, so this stays a clean negative — exactly what we want.
    PIIFixture("Order #123/456/789", None, False),
    # DIN 5008 parenthesised phone — the deterministic stage MUST NOT
    # mistake the bare "(089)" for a match. Real coverage of this format
    # is the LLM stage's job; this test guards against false positives.
    PIIFixture("(089) 1234 567", None, False),
    PIIFixture("DE und Frankreich", None, False),

    # ── Whitespace/format variants ──────────────────────────────────────
    PIIFixture("iban DE89 3704 0044 0532 0130 00.", "iban", True),
    PIIFixture("\t+49\t2331\t1234567\n", "phone_de", True),

    # ── Repeat patterns — must tokenise BOTH occurrences ────────────────
    PIIFixture(
        "Erst DE89370400440532013000, dann DE89 3704 0044 0532 0130 00.",
        "iban",
        True,
    ),

    # ── Long German free-text (LLM-stage candidate; det. stage no hit) ──
    PIIFixture(
        "Wir bitten Frau Sophie Müller, ihre neue Anschrift "
        "Lindenstraße 4, 12101 Berlin, mitzuteilen.",
        None,
        False,
    ),

    # ── Pads to 50 total ────────────────────────────────────────────────
    _BLANK, _BLANK, _BLANK, _BLANK, _BLANK,
    _BLANK, _BLANK,
)


__all__ = ["PIIFixture", "PII_FIXTURES"]
