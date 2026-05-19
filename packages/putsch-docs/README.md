# putsch-docs — Document/OCR layer for the AP Crew

Production-grade entry point of the Putsch Group Eingangsrechnung pipeline.
Every downstream agent — Match-Agent, Buchungs-Agent, master-data — depends
on what comes out of this module. If extraction is 90% accurate, the stack
caps at 90%. If it's 99%, the stack compounds to near-zero exceptions.

## The accuracy bet

We do **not** treat document extraction as a one-model problem. We run two
models with **uncorrelated failure modes** and reconcile them through a
per-field confidence calibrator.

```
  PDF / image / XRechnung
            │
            ▼
   ┌─────────────────┐
   │   DOCLING +     │   structural parser, IBM Research,
   │ Granite-Docling │   LF AI & Data governed, MIT.
   │      258M       │   TableFormer for tables, native German.
   └────────┬────────┘
            │  markdown + per-region scores
            ▼
   ┌─────────────────┐
   │  DSPy program   │   coerces markdown → InvoiceFields
   │  + validators   │   (IBAN MOD-97, USt-IdNr, arithmetic)
   └────────┬────────┘
            │
            ▼
   ┌─────────────────┐
   │   Confidence    │   per-field calibrator: docling score,
   │   calibrator    │   validator pass/fail, structure
   │                 │   consistency, LLM-as-judge
   └────────┬────────┘
            │
       confident?
       /        \
      yes        no
      │           │
      │           ▼
      │     ┌─────────────────┐
      │     │  Qwen2.5-VL-72B │   different architecture entirely.
      │     │   via vLLM      │   vision-language reasoner.
      │     │   (Frankfurt)   │   different failure modes.
      │     └────────┬────────┘
      │              │
      └───── reconcile per-field ──────► InvoiceFields + ConfidenceReport
                                          + ExtractionTrace
```

### Why hybrid Docling + VLM?

Docling and Qwen-VL fail on different inputs. Docling stumbles on
handwritten annotations, low-DPI scans, and unusual stamp placement.
Qwen-VL stumbles on dense numerical tables and rare-format invoices.
Stacking them yields a near-complementary error surface. The fallback
is not "try harder" — it's a fundamentally different model architecture
applied to the same input. That's the bet.

### Why per-field confidence, not per-document?

An invoice often has high-confidence header fields and one low-confidence
line item. Running the entire fallback path because of a single bad
line item wastes minutes; conversely, accepting the whole document
because the header looks fine costs cash when the line item is wrong.

The calibrator scores each field independently and triggers fallback only
on the regions that need it. The trace records per-field provenance —
which path produced which value — so the AP Crew's audit replay knows
exactly what happened.

### Why arithmetic consistency is the highest-signal validation

LLMs report confidence the LLM understands, not the confidence the
business needs. A model can return brutto = 1190.00 with 0.97 confidence
when the netto and MwSt it also returned sum to 1191.00 — the model
doesn't check itself.

We do. `netto + mwst = brutto` collapses confidence on every amount
field when it fails, regardless of model self-report. Same for IBAN
MOD-97, USt-IdNr country format, and line-items summing to netto. These
deterministic checks catch hallucinations the LLM cannot self-detect.

## Public surface

```python
from putsch_docs import DoclingExtractor

extractor = DoclingExtractor()
result = await extractor.extract("eingangsrechnung_2026.pdf")

result.invoice            # InvoiceFields — strongly typed, validated
result.confidence         # ConfidenceReport — per-field scores
result.trace              # ExtractionTrace — which path produced what
```

Errors are typed:

| Error                  | Meaning                                              |
| ---------------------- | ---------------------------------------------------- |
| `DoclingError`         | Primary structural parser failed                     |
| `FallbackError`        | VLM path failed                                      |
| `ConfidenceError`      | Both paths ran; neither met critical threshold       |
| `FieldValidationError` | Format / arithmetic check failed at the schema level |

## Integrations

- **CrewAI**: `putsch_docs.tools.ExtractInvoiceTool` — drop into any Crew.
- **MCP**: `putsch-docs-mcp` console script — stdio server for LangGraph
  nodes and future external integrations.
- **DSPy**: prompts are version-pinned signatures (`SIGNATURE_VERSION`),
  not strings buried in code. Eval-blocking on signature change.
- **LiteLLM**: every model call routes through it. Swap Mistral Large
  for Mistral Medium with a config change, no code edit.

## Configuration

Everything is env-driven. See `.env.example`. Settings are validated by
pydantic-settings at startup, so misconfigurations fail loud at process
boot, not at runtime.

The default points all model traffic at Frankfurt-hosted infrastructure:
Mistral La Plateforme (Paris) for extraction LLM, self-hosted vLLM for
Qwen-VL fallback, self-hosted Langfuse for traces. **No US-hosted
inference. No EU-out data flows.**

## Observability

- structlog JSON output, correlation IDs (document_id, run_id) propagated
  from the calling Crew via contextvars.
- Langfuse trace per extraction, child span per model decision.
- PII redaction at the structlog boundary. Invoice content (IBAN,
  USt-IdNr, Steuernummer, email, phone, Steuernummer) never appears
  unredacted in logs.
- Per-field confidence is logged on every call. Drift in confidence
  distributions is observable in Grafana without changes here.

## Tests

```bash
pip install -e ".[dev]"
pytest                           # unit + property tests (fast)
pytest -m "integration"          # nightly — real Docling
pytest -m "performance"          # latency budget checks
```

Property tests (Hypothesis): IBAN MOD-97, USt-IdNr per-country format,
arithmetic invariant `netto + mwst = brutto`.

Fixtures: 5 synthetic German invoices covering clean PDF, scanned PDF,
multi-page tables, handwritten annotation, watermark + Eingangsstempel.
Regenerated deterministically via `python -m tests.fixtures.generate_fixtures`.

## Eval harness

```bash
python scripts/eval.py --strict --baseline 0.94 --tolerance 0.02
```

Runs against `tests/fixtures/labels.json`, computes field-level F1, ships
to Langfuse as a Dataset Run, exits non-zero if any critical field's F1
drops more than 2% vs. the trailing baseline. Wire into CI to block
regressions.

## What this module is **not**

- Not a generic PDF extractor — it's tuned for German B2B invoices.
- Not a Lieferschein or Bestellung extractor — those are future modules
  reusing the same Docling primitives.
- Not a journal poster — the Buchungs-Agent owns DATEV mapping.
- Not a 3-way matcher — the Match-Agent owns SAP PO matching.

## German domain glossary

| Term                | Meaning                                                  |
| ------------------- | -------------------------------------------------------- |
| Eingangsrechnung    | Incoming invoice (from a vendor)                         |
| Rechnungsnummer     | Invoice number (vendor-issued)                           |
| Rechnungsdatum      | Invoice date                                             |
| Leistungsdatum      | Date of service/delivery (§14 UStG required >€250 net)   |
| Lieferant           | Vendor                                                   |
| USt-IdNr            | EU VAT identification number                             |
| Steuernummer        | German Finanzamt-issued tax number                       |
| Skonto              | Early-payment discount                                   |
| XRechnung           | EN 16931 / UBL 2.1 standardized German e-invoice         |
| Belege online       | DATEV's invoice document service                         |
| Eingangsstempel     | "Received" stamp marked on invoices by AP department     |

See [ADR-002.md](./ADR-002.md) for the full architectural rationale.
