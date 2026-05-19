"""Deterministic generator for the 5 invoice PDF fixtures.

Run once locally to materialize the PDFs into tests/fixtures/. Kept as a
script (not run at test time) so the fixtures are version-controlled
binary blobs — diffs in the generator don't silently move the goalposts
on the eval harness.

Usage:
    python -m tests.fixtures.generate_fixtures

Requires reportlab. Outputs:
    clean_invoice.pdf
    scanned_invoice.pdf
    multipage_tables.pdf
    handwritten_annotation.pdf
    watermark_stamp.pdf

Each fixture corresponds to a real-world failure mode we want covered
by the eval harness.
"""

from __future__ import annotations

import io
from datetime import date
from pathlib import Path

try:
    from reportlab.lib import colors
    from reportlab.lib.pagesizes import A4
    from reportlab.lib.styles import getSampleStyleSheet
    from reportlab.lib.units import cm
    from reportlab.pdfgen import canvas
    from reportlab.platypus import (
        SimpleDocTemplate,
        Spacer,
        Table,
        TableStyle,
    )
except ImportError as exc:  # pragma: no cover
    raise SystemExit(
        "Install reportlab to regenerate fixtures: pip install reportlab"
    ) from exc

FIXTURES = Path(__file__).resolve().parent

CANONICAL = {
    "rechnungsnummer": "2026-04001",
    "rechnungsdatum": date(2026, 4, 18),
    "leistungsdatum": date(2026, 4, 15),
    "vendor_name": "Schmidt Industrieteile GmbH",
    "vendor_address": "Industriestraße 14, 58095 Hagen",
    "vendor_ustid": "DE129273398",
    "customer_ustid": "DE811184878",
    "iban": "DE89 3704 0044 0532 0130 00",
    "bic": "COBADEFFXXX",
    "po": "PO-2026-7781",
    "vendor_no": "50012345",
    "netto": "1.000,00",
    "mwst": "190,00",
    "brutto": "1.190,00",
}


def _draw_header(c: canvas.Canvas, *, scanned: bool = False) -> None:
    c.setFont("Helvetica-Bold", 16)
    c.drawString(2 * cm, 27 * cm, CANONICAL["vendor_name"])
    c.setFont("Helvetica", 10)
    c.drawString(2 * cm, 26.3 * cm, CANONICAL["vendor_address"])
    c.drawString(2 * cm, 25.8 * cm, f"USt-IdNr: {CANONICAL['vendor_ustid']}")

    c.setFont("Helvetica-Bold", 14)
    c.drawString(2 * cm, 23 * cm, "RECHNUNG")
    c.setFont("Helvetica", 10)
    c.drawString(
        2 * cm, 22.3 * cm, f"Rechnungs-Nr.: {CANONICAL['rechnungsnummer']}"
    )
    c.drawString(
        2 * cm,
        21.8 * cm,
        f"Rechnungsdatum: {CANONICAL['rechnungsdatum'].strftime('%d.%m.%Y')}",
    )
    c.drawString(
        2 * cm,
        21.3 * cm,
        f"Leistungsdatum: {CANONICAL['leistungsdatum'].strftime('%d.%m.%Y')}",
    )
    c.drawString(
        2 * cm, 20.8 * cm, f"Ihre Bestellnummer: {CANONICAL['po']}"
    )
    c.drawString(
        2 * cm, 20.3 * cm, f"Lieferanten-Nr.: {CANONICAL['vendor_no']}"
    )

    if scanned:
        # Simulate scanner noise: rotate the page slightly and decrease contrast
        # by drawing a faint grey overlay. Real Docling routes this through OCR.
        c.setFillColorRGB(0.8, 0.8, 0.8)
        c.setFont("Helvetica", 8)
        for y in range(0, 850, 23):
            c.drawString(0.5 * cm, y / 28.35 * cm, "·" * 100)
        c.setFillColorRGB(0, 0, 0)


def _draw_line_items(c: canvas.Canvas) -> None:
    data = [
        ["Pos", "Material", "Beschreibung", "Menge", "EP", "GP"],
        ["1", "M-4711", "Zahnradgetriebe Typ A", "10", "60,00", "600,00"],
        ["2", "M-4712", "Wellendichtring 30x42", "4", "100,00", "400,00"],
    ]
    t = Table(data, colWidths=[1 * cm, 2 * cm, 6 * cm, 2 * cm, 2 * cm, 2 * cm])
    t.setStyle(
        TableStyle(
            [
                ("BACKGROUND", (0, 0), (-1, 0), colors.lightgrey),
                ("FONTNAME", (0, 0), (-1, 0), "Helvetica-Bold"),
                ("GRID", (0, 0), (-1, -1), 0.4, colors.grey),
                ("ALIGN", (3, 0), (-1, -1), "RIGHT"),
            ]
        )
    )
    t.wrapOn(c, 18 * cm, 8 * cm)
    t.drawOn(c, 2 * cm, 12 * cm)


def _draw_totals(c: canvas.Canvas) -> None:
    c.setFont("Helvetica", 10)
    c.drawString(12 * cm, 10 * cm, f"Netto: {CANONICAL['netto']} EUR")
    c.drawString(12 * cm, 9.5 * cm, f"MwSt 19,00%: {CANONICAL['mwst']} EUR")
    c.setFont("Helvetica-Bold", 11)
    c.drawString(12 * cm, 9 * cm, f"Brutto: {CANONICAL['brutto']} EUR")

    c.setFont("Helvetica", 9)
    c.drawString(2 * cm, 6 * cm, f"IBAN: {CANONICAL['iban']}")
    c.drawString(2 * cm, 5.5 * cm, f"BIC: {CANONICAL['bic']}")
    c.drawString(
        2 * cm,
        5 * cm,
        "Zahlbar innerhalb 30 Tagen ohne Abzug. 2% Skonto bei Zahlung "
        "innerhalb 14 Tagen.",
    )


def _make_clean() -> None:
    c = canvas.Canvas(str(FIXTURES / "clean_invoice.pdf"), pagesize=A4)
    _draw_header(c)
    _draw_line_items(c)
    _draw_totals(c)
    c.showPage()
    c.save()


def _make_scanned() -> None:
    c = canvas.Canvas(str(FIXTURES / "scanned_invoice.pdf"), pagesize=A4)
    _draw_header(c, scanned=True)
    _draw_line_items(c)
    _draw_totals(c)
    c.showPage()
    c.save()


def _make_multipage() -> None:
    c = canvas.Canvas(str(FIXTURES / "multipage_tables.pdf"), pagesize=A4)
    _draw_header(c)
    # First page table (continued)
    rows = [["Pos", "Material", "Beschreibung", "Menge", "EP", "GP"]]
    for i in range(1, 25):
        rows.append(
            [str(i), f"M-{4710 + i}", f"Teil {i}", "10", "60,00", "600,00"]
        )
    t = Table(rows, colWidths=[1 * cm, 2 * cm, 6 * cm, 2 * cm, 2 * cm, 2 * cm])
    t.setStyle(
        TableStyle(
            [
                ("FONTNAME", (0, 0), (-1, 0), "Helvetica-Bold"),
                ("GRID", (0, 0), (-1, -1), 0.4, colors.grey),
            ]
        )
    )
    t.wrapOn(c, 18 * cm, 16 * cm)
    t.drawOn(c, 2 * cm, 4 * cm)
    c.showPage()
    # Second page totals
    c.setFont("Helvetica", 10)
    c.drawString(2 * cm, 27 * cm, "Fortsetzung Rechnung 2026-04001")
    c.drawString(12 * cm, 10 * cm, "Netto: 14.400,00 EUR")
    c.drawString(12 * cm, 9.5 * cm, "MwSt 19,00%: 2.736,00 EUR")
    c.setFont("Helvetica-Bold", 11)
    c.drawString(12 * cm, 9 * cm, "Brutto: 17.136,00 EUR")
    c.showPage()
    c.save()


def _make_handwritten() -> None:
    c = canvas.Canvas(str(FIXTURES / "handwritten_annotation.pdf"), pagesize=A4)
    _draw_header(c)
    _draw_line_items(c)
    _draw_totals(c)
    # Simulate handwritten annotation in the margin
    c.setFillColorRGB(0, 0, 0.6)
    c.setFont("Helvetica-Oblique", 12)
    c.drawString(13 * cm, 19 * cm, "Genehmigt - K.M.")
    c.drawString(13 * cm, 18.4 * cm, "Konto 1500")
    c.setFillColorRGB(0, 0, 0)
    c.showPage()
    c.save()


def _make_watermark() -> None:
    c = canvas.Canvas(str(FIXTURES / "watermark_stamp.pdf"), pagesize=A4)
    # Watermark first
    c.saveState()
    c.translate(10 * cm, 14 * cm)
    c.rotate(30)
    c.setFillColorRGB(0.9, 0.9, 0.9)
    c.setFont("Helvetica-Bold", 60)
    c.drawString(-5 * cm, 0, "BEZAHLT")
    c.restoreState()

    _draw_header(c)
    _draw_line_items(c)
    _draw_totals(c)

    # Eingangsstempel
    c.setStrokeColorRGB(0.6, 0, 0)
    c.setFillColorRGB(0.6, 0, 0)
    c.rect(14 * cm, 24 * cm, 5 * cm, 2.5 * cm, stroke=1, fill=0)
    c.setFont("Helvetica-Bold", 9)
    c.drawString(14.3 * cm, 26 * cm, "EINGANG")
    c.drawString(14.3 * cm, 25.4 * cm, "Putsch GmbH & Co. KG")
    c.drawString(14.3 * cm, 24.8 * cm, "19.04.2026 — Buchhaltung")
    c.setFillColorRGB(0, 0, 0)
    c.setStrokeColorRGB(0, 0, 0)

    c.showPage()
    c.save()


def main() -> None:
    _make_clean()
    _make_scanned()
    _make_multipage()
    _make_handwritten()
    _make_watermark()
    print("Generated 5 fixtures in", FIXTURES)


if __name__ == "__main__":
    main()
