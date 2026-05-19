"""Eval harness — field-level F1 against a labeled set.

Walks a labeled-invoice corpus, runs the extractor, computes per-field
precision/recall/F1, and (when Langfuse credentials are present) ships
the run as a Langfuse Dataset Run for trend tracking and CI gating.

CI invariant: if any *critical* field's F1 drops by more than 2% vs.
the trailing baseline, exit non-zero. This is the regression block.

Usage:
    python scripts/eval.py --corpus tests/fixtures --labels tests/fixtures/labels.json
    python scripts/eval.py --baseline 0.94 --tolerance 0.02 --strict
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from putsch_docs.exceptions import ExtractionError
from putsch_docs.extractor import DoclingExtractor, ExtractionResult
from putsch_docs.observability import configure_logging, get_logger, langfuse_client
from putsch_docs.signatures import InvoiceFields

log = get_logger("putsch_docs.eval")

# Critical fields — same set as ConfidenceSettings.critical_fields, mirrored here
# so the eval harness has no runtime dependency on Settings construction order.
CRITICAL_FIELDS: frozenset[str] = frozenset(
    {
        "rechnungsnummer",
        "rechnungsdatum",
        "lieferant_ustid",
        "kunde_ustid",
        "iban",
        "netto_betrag",
        "mwst_betrag",
        "brutto_betrag",
    }
)


@dataclass
class FieldStats:
    tp: int = 0
    fp: int = 0
    fn: int = 0

    def precision(self) -> float:
        denom = self.tp + self.fp
        return self.tp / denom if denom else 1.0

    def recall(self) -> float:
        denom = self.tp + self.fn
        return self.tp / denom if denom else 1.0

    def f1(self) -> float:
        p, r = self.precision(), self.recall()
        return 2 * p * r / (p + r) if (p + r) else 0.0


def _normalize_for_compare(field: str, v: Any) -> str | None:
    if v is None:
        return None
    if field in {"iban", "lieferant_ustid", "kunde_ustid", "bic"}:
        return str(v).replace(" ", "").upper()
    if field in {"netto_betrag", "mwst_betrag", "brutto_betrag", "mwst_satz", "skonto_prozent"}:
        # Compare as 2-decimal string
        from decimal import Decimal

        try:
            return str(Decimal(str(v)).quantize(Decimal("0.01")))
        except Exception:
            return str(v)
    return str(v)


def _score(
    expected: dict[str, Any],
    extracted: InvoiceFields | None,
    stats: dict[str, FieldStats],
) -> None:
    extracted_dump = (
        extracted.model_dump(mode="json") if extracted is not None else {}
    )
    for field, exp_val in expected.items():
        if field.startswith("_"):
            continue
        ext_val = extracted_dump.get(field)
        e = _normalize_for_compare(field, exp_val)
        x = _normalize_for_compare(field, ext_val)
        s = stats[field]
        if e is None and x is None:
            continue
        if e == x:
            s.tp += 1
        elif e is None:
            s.fp += 1
        elif x is None:
            s.fn += 1
        else:
            s.fp += 1
            s.fn += 1


async def _extract_one(
    extractor: DoclingExtractor, path: Path
) -> tuple[Path, InvoiceFields | None, ExtractionError | None]:
    try:
        result: ExtractionResult = await extractor.extract(path, document_id=path.stem)
    except ExtractionError as exc:
        return path, None, exc
    return path, result.invoice, None


def _report_to_langfuse(
    f1_by_field: dict[str, float],
    overall_f1: float,
    corpus_root: Path,
) -> None:
    client = langfuse_client()
    if client is None:
        log.info("eval.langfuse_skipped", reason="no_client")
        return
    try:
        dataset_name = f"putsch_docs_eval_{datetime.now(timezone.utc):%Y%m}"
        run_name = f"{datetime.now(timezone.utc):%Y%m%dT%H%M%SZ}_{corpus_root.name}"
        client.create_dataset_run_item(
            dataset_name=dataset_name,
            run_name=run_name,
            run_metadata={
                "overall_f1": overall_f1,
                "field_f1": f1_by_field,
            },
        )
        client.flush()
        log.info("eval.langfuse_reported", dataset=dataset_name, run=run_name)
    except Exception as exc:  # pragma: no cover — telemetry is best-effort
        log.warning("eval.langfuse_failed", error=str(exc))


async def run_eval(args: argparse.Namespace) -> int:
    configure_logging()
    corpus = Path(args.corpus).resolve()
    labels_path = Path(args.labels).resolve()

    labels: dict[str, dict[str, Any]] = json.loads(labels_path.read_text())
    extractor = DoclingExtractor()

    stats: dict[str, FieldStats] = defaultdict(FieldStats)
    failures: list[str] = []

    files: list[Path] = []
    for fname in labels:
        if fname.startswith("_"):
            continue
        p = corpus / fname
        if not p.exists():
            log.warning("eval.fixture_missing", file=str(p))
            continue
        files.append(p)

    if not files:
        log.error("eval.no_fixtures_found", corpus=str(corpus))
        return 2

    sem = asyncio.Semaphore(4)

    async def _go(p: Path) -> tuple[Path, InvoiceFields | None, ExtractionError | None]:
        async with sem:
            return await _extract_one(extractor, p)

    results = await asyncio.gather(*(_go(p) for p in files))

    for p, inv, err in results:
        expected = labels[p.name]
        if err is not None:
            failures.append(f"{p.name}: {type(err).__name__}: {err}")
            # All expected fields counted as FN
            for f in expected:
                if not f.startswith("_"):
                    stats[f].fn += 1
            continue
        _score(expected, inv, stats)

    print()
    print("=== Eval results ===")
    print(f"{'field':<28}{'precision':>10}{'recall':>10}{'f1':>10}{'support':>10}")
    overall_tp = overall_fp = overall_fn = 0
    f1_by_field: dict[str, float] = {}
    critical_f1: dict[str, float] = {}
    for field in sorted(stats):
        s = stats[field]
        f1 = s.f1()
        f1_by_field[field] = f1
        if field in CRITICAL_FIELDS:
            critical_f1[field] = f1
        support = s.tp + s.fn
        print(
            f"{field:<28}{s.precision():>10.3f}{s.recall():>10.3f}"
            f"{f1:>10.3f}{support:>10}"
        )
        overall_tp += s.tp
        overall_fp += s.fp
        overall_fn += s.fn

    overall_p = overall_tp / max(1, overall_tp + overall_fp)
    overall_r = overall_tp / max(1, overall_tp + overall_fn)
    overall_f1 = 2 * overall_p * overall_r / max(1e-9, overall_p + overall_r)
    print("-" * 68)
    print(
        f"{'OVERALL':<28}{overall_p:>10.3f}{overall_r:>10.3f}"
        f"{overall_f1:>10.3f}{overall_tp + overall_fn:>10}"
    )
    print()
    if failures:
        print(f"Extraction failures ({len(failures)}):")
        for f in failures:
            print(f"  - {f}")
        print()

    _report_to_langfuse(f1_by_field, overall_f1, corpus)

    # Regression gate
    if args.strict:
        for field, f1 in critical_f1.items():
            if f1 + args.tolerance < args.baseline:
                print(
                    f"REGRESSION: critical field '{field}' F1={f1:.3f} "
                    f"below baseline {args.baseline:.3f} - {args.tolerance:.3f}",
                    file=sys.stderr,
                )
                return 1
        if overall_f1 + args.tolerance < args.baseline:
            print(
                f"REGRESSION: overall F1 {overall_f1:.3f} below baseline "
                f"{args.baseline:.3f} - {args.tolerance:.3f}",
                file=sys.stderr,
            )
            return 1

    return 0


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--corpus", default="tests/fixtures")
    parser.add_argument("--labels", default="tests/fixtures/labels.json")
    parser.add_argument("--baseline", type=float, default=0.94)
    parser.add_argument("--tolerance", type=float, default=0.02)
    parser.add_argument("--strict", action="store_true")
    args = parser.parse_args()
    sys.exit(asyncio.run(run_eval(args)))


if __name__ == "__main__":
    os.environ.setdefault("PUTSCH_DOCS_OBS__LOG_LEVEL", "INFO")
    main()
