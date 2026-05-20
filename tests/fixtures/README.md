# Fixtures

5 synthetic German B2B invoices covering the failure modes our pipeline must handle.

PDFs are **not** checked in directly — they are regenerated deterministically from
`generate_fixtures.py` (requires `reportlab`). This keeps the git history clean
and avoids binary blob churn on every cosmetic tweak.

Generate them:

```bash
pip install reportlab
python -m tests.fixtures.generate_fixtures
```

| File                          | Failure mode covered                                |
| ----------------------------- | --------------------------------------------------- |
| `clean_invoice.pdf`           | Baseline — clean digital PDF, single page           |
| `clean_xrechnung.xml`         | UBL Invoice 2.1 (EN 16931), no rasterization needed |
| `scanned_invoice.pdf`         | Scanner-noise overlay, drives Docling OCR path      |
| `multipage_tables.pdf`        | Table spans 2 pages, TableFormer stitching          |
| `handwritten_annotation.pdf`  | Margin annotation — Docling weak, Qwen-VL strong    |
| `watermark_stamp.pdf`         | Diagonal BEZAHLT watermark + Eingangsstempel        |

Ground truth lives in `labels.json` keyed by filename. The eval harness
(`scripts/eval.py`) computes per-field F1 against this file.
