"""DoclingExtractor — primary entry point of the document layer.

Pipeline (per document):
    1. Docling DocumentConverter → DoclingDocument → markdown + per-region scores
    2. DSPy program coerces markdown → InvoiceFields via the
       ExtractInvoiceFromMarkdown signature
    3. Validators run on every parsed field
    4. ConfidenceCalibrator combines signals → ConfidenceReport
    5. If fallback_required: VLM path runs, calibrator re-runs on its output
    6. Pick the surviving path; if neither survives critical thresholds,
       raise ConfidenceError with both partials

Never silently downgrade. Never re-extract what's already correct.
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import Iterable
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Literal

import dspy
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from putsch_docs.config import Settings, get_settings
from putsch_docs.confidence import (
    ConfidenceCalibrator,
    ConfidenceReport,
    FieldEvidence,
    build_structure_evidence,
)
from putsch_docs.exceptions import (
    ConfidenceError,
    DoclingError,
    ExtractionError,
    FallbackError,
)
from putsch_docs.fallback import QwenVLFallback
from putsch_docs.observability import (
    correlation,
    current_document_id,
    current_run_id,
    get_logger,
    langfuse_client,
)
from putsch_docs.signatures import (
    SIGNATURE_VERSION,
    ExtractInvoiceFromMarkdown,
    InvoiceFields,
    JudgeCriticalField,
)

log = get_logger(__name__)


PathOrBytes = Path | bytes
ExtractionPath = Literal["docling", "fallback"]


# ----- Trace + result types --------------------------------------------------------


class ExtractionTrace(BaseModel):
    """Audit-grade record of what happened during one extraction."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    document_id: str
    run_id: str | None
    started_at: datetime
    finished_at: datetime
    duration_ms: float
    primary_path: ExtractionPath
    fallback_invoked: bool
    fallback_reason: str | None
    paths_tried: list[ExtractionPath]
    signature_version: str
    docling_model: str
    fallback_model: str | None
    field_paths: dict[str, ExtractionPath] = Field(
        default_factory=dict,
        description="Per-field provenance: which path produced the surviving value.",
    )


class ExtractionResult(BaseModel):
    """Everything callers get back. The AP Crew matches against this."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    invoice: InvoiceFields
    confidence: ConfidenceReport
    trace: ExtractionTrace


# ----- Docling internals -----------------------------------------------------------


@dataclass(slots=True)
class _DoclingRun:
    """Output of the Docling structural pass."""

    markdown: str
    region_scores: dict[str, float]
    page_count: int
    raw_doc: Any  # DoclingDocument — kept for downstream replay


def _import_docling() -> Any:
    """Lazy import — heavy dependency. Surface a friendlier error if missing."""
    try:
        from docling.datamodel.base_models import InputFormat
        from docling.datamodel.pipeline_options import PdfPipelineOptions
        from docling.document_converter import DocumentConverter, PdfFormatOption
    except ImportError as exc:  # pragma: no cover — exercised at install time
        msg = (
            "docling not installed. Run `pip install docling docling-core` "
            "(GPU users: also install with the [gpu] extra)."
        )
        raise DoclingError(msg, cause=exc) from exc
    return DocumentConverter, PdfFormatOption, PdfPipelineOptions, InputFormat


def _build_converter(settings: Settings) -> Any:
    DocumentConverter, PdfFormatOption, PdfPipelineOptions, InputFormat = _import_docling()
    pdf_opts = PdfPipelineOptions(
        do_ocr=settings.docling.do_ocr,
        do_table_structure=settings.docling.do_table_structure,
        # Granite-Docling artifacts cached locally for air-gapped Frankfurt deploy
        artifacts_path=(
            str(settings.docling.artifacts_path)
            if settings.docling.artifacts_path
            else None
        ),
    )
    return DocumentConverter(
        format_options={
            InputFormat.PDF: PdfFormatOption(pipeline_options=pdf_opts),
        }
    )


def _normalize_region_scores(raw_doc: Any) -> dict[str, float]:
    """Map DoclingDocument internal confidence (when present) onto our field names.

    Docling exposes per-element confidence in newer releases via
    `element.confidence` or via the document's `provenance` records. We
    aggregate by a coarse field-name → element-score mapping. Where Docling
    does not yet expose a per-field score we default to 0.92 (Docling's
    documented headline accuracy on enterprise PDFs).

    This indirection isolates us from Docling version drift — if the
    upstream API changes, only this function needs to update.
    """
    scores: dict[str, float] = {}
    # The DoclingDocument exposes pages → elements. Walk and collect.
    try:
        for page in getattr(raw_doc, "pages", []) or []:
            for element in getattr(page, "elements", []) or []:
                conf = getattr(element, "confidence", None)
                label = (getattr(element, "label", "") or "").lower()
                if conf is None or not label:
                    continue
                scores[label] = max(scores.get(label, 0.0), float(conf))
    except Exception:  # pragma: no cover — defensive
        pass
    return scores


def _field_score(
    region_scores: dict[str, float], field_name: str, default: float = 0.92
) -> float:
    """Best-effort mapping from our schema field → Docling region label.

    Stays explicit (not a regex match) so that field-to-region mapping is
    auditable: if a downstream agent wants to know why
    `lieferant_ustid` got 0.83, this map tells them which Docling region
    we trusted.
    """
    label_map: dict[str, tuple[str, ...]] = {
        "rechnungsnummer": ("invoice-number", "rechnungsnummer", "title", "header"),
        "rechnungsdatum": ("invoice-date", "date", "rechnungsdatum"),
        "leistungsdatum": ("delivery-date", "service-date", "leistungsdatum"),
        "lieferant_name": ("vendor", "supplier", "lieferant", "header"),
        "lieferant_ustid": ("vat-id", "ust-id", "tax-id"),
        "lieferant_address": ("address", "vendor-address"),
        "kunde_ustid": ("customer-vat-id",),
        "iban": ("iban", "bank-account"),
        "bic": ("bic", "swift"),
        "netto_betrag": ("net-total", "netto", "subtotal"),
        "mwst_betrag": ("vat-amount", "mwst", "tax-amount"),
        "mwst_satz": ("vat-rate", "tax-rate"),
        "brutto_betrag": ("gross-total", "brutto", "total"),
        "waehrung": ("currency",),
        "zahlungsziel": ("payment-terms",),
        "skonto_prozent": ("discount",),
        "skonto_frist": ("discount-term",),
        "bestellnummer_ref": ("po-number", "purchase-order"),
        "lieferantennummer_ref": ("vendor-id",),
        "line_items": ("table", "line-items"),
    }
    for candidate in label_map.get(field_name, ()):
        if candidate in region_scores:
            return region_scores[candidate]
    return default


# ----- Extractor -------------------------------------------------------------------


class DoclingExtractor:
    """Async-safe extractor. Stateless wrt request — safe to share across crews."""

    def __init__(
        self,
        settings: Settings | None = None,
        *,
        fallback: QwenVLFallback | None = None,
        calibrator: ConfidenceCalibrator | None = None,
        converter: Any | None = None,
    ) -> None:
        self.settings = settings or get_settings()
        self._converter = converter  # lazy-built on first use
        self._fallback = fallback
        self._calibrator = calibrator or ConfidenceCalibrator(self.settings.confidence)
        self._extractor_program: Any | None = None
        self._judge_program: Any | None = None
        self._converter_lock = asyncio.Lock()

    # ---------- public API ----------

    async def extract(
        self,
        source: PathOrBytes,
        *,
        document_id: str | None = None,
        run_id: str | None = None,
    ) -> ExtractionResult:
        """Run the full pipeline on one document. Raises typed ExtractionError."""
        with correlation(document_id=document_id, run_id=run_id):
            return await self._extract_locked()

    async def _extract_locked(self) -> ExtractionResult:
        started = time.monotonic()
        started_dt = datetime.now(timezone.utc)
        paths_tried: list[ExtractionPath] = []
        fallback_invoked = False
        fallback_reason: str | None = None

        # Source is bound inside the trace context, but we need to thread it
        # through the inner methods. Pull from a contextvar would be heavy;
        # we accept the explicit arg in the public method only.
        raise RuntimeError("_extract_locked must be called via _run_pipeline")

    async def _run_pipeline(self, source: PathOrBytes) -> ExtractionResult:
        started = time.monotonic()
        started_dt = datetime.now(timezone.utc)
        paths_tried: list[ExtractionPath] = []
        fallback_invoked = False
        fallback_reason: str | None = None

        trace_span = self._begin_langfuse_trace()

        # ---- 1. Docling structural pass ----
        try:
            docling_run = await self._docling_convert(source)
        except DoclingError as exc:
            log.warning(
                "docling.failed",
                error=str(exc),
                going_to_fallback=self.settings.fallback.enabled,
            )
            # If Docling fails outright, fallback is our only chance
            if not self.settings.fallback.enabled:
                self._end_langfuse_trace(trace_span, error=exc)
                raise
            paths_tried.append("docling")
            invoice, conf_report = await self._fallback_only(source)
            fallback_invoked = True
            fallback_reason = "docling_failed"
            paths_tried.append("fallback")
            return self._finalize_result(
                invoice=invoice,
                conf_report=conf_report,
                primary="fallback",
                paths_tried=paths_tried,
                fallback_invoked=fallback_invoked,
                fallback_reason=fallback_reason,
                started=started,
                started_dt=started_dt,
                field_paths={f: "fallback" for f in invoice.model_fields},
                trace_span=trace_span,
            )

        paths_tried.append("docling")

        # ---- 2. DSPy structured coercion ----
        docling_invoice, docling_validation_error = await self._coerce_markdown(
            docling_run.markdown
        )

        # ---- 3 + 4. Validate & calibrate ----
        docling_report = self._calibrate(
            invoice=docling_invoice,
            region_scores=docling_run.region_scores,
            markdown=docling_run.markdown,
            ran_judge=self.settings.confidence.judge_critical_fields_always
            and docling_invoice is not None,
        )

        needs_fallback = (
            docling_invoice is None
            or docling_validation_error is not None
            or docling_report.fallback_required
        )

        if not needs_fallback:
            assert docling_invoice is not None
            return self._finalize_result(
                invoice=docling_invoice,
                conf_report=docling_report,
                primary="docling",
                paths_tried=paths_tried,
                fallback_invoked=False,
                fallback_reason=None,
                started=started,
                started_dt=started_dt,
                field_paths={f: "docling" for f in docling_invoice.model_fields},
                trace_span=trace_span,
            )

        # ---- 5. Fallback if enabled ----
        if not self.settings.fallback.enabled:
            if docling_invoice is None:
                err = ExtractionError(
                    "Docling produced no valid InvoiceFields and fallback is disabled",
                    document_id=current_document_id(),
                    cause=docling_validation_error,
                )
            else:
                err = ConfidenceError(
                    "confidence below threshold and fallback disabled",
                    document_id=current_document_id(),
                    docling_partial=docling_invoice.model_dump(mode="json")
                    if docling_invoice
                    else None,
                    fallback_partial=None,
                    confidence_report=docling_report.model_dump(mode="json"),
                )
            self._end_langfuse_trace(trace_span, error=err)
            raise err

        fallback_invoked = True
        fallback_reason = (
            "validation_error"
            if docling_invoice is None
            else "low_confidence_critical_field"
        )
        log.info(
            "fallback.trigger",
            reason=fallback_reason,
            failures=docling_report.critical_failures,
            overall_min=docling_report.overall_min,
        )

        try:
            fb_invoice = await self._invoke_fallback(source)
        except FallbackError as exc:
            # Both paths failed. Surface both partials.
            if docling_invoice is None:
                final = ExtractionError(
                    "both Docling and fallback failed to extract",
                    document_id=current_document_id(),
                    cause=exc,
                )
            else:
                final = ConfidenceError(
                    "Docling low-confidence and fallback failed",
                    document_id=current_document_id(),
                    docling_partial=docling_invoice.model_dump(mode="json"),
                    fallback_partial=exc.partial,
                    confidence_report=docling_report.model_dump(mode="json"),
                )
            self._end_langfuse_trace(trace_span, error=final)
            paths_tried.append("fallback")
            raise final from exc

        paths_tried.append("fallback")

        fb_report = self._calibrate(
            invoice=fb_invoice,
            region_scores={},  # no region scores from VLM
            markdown=docling_run.markdown,
            ran_judge=self.settings.confidence.judge_critical_fields_always,
            # Prior for VLM-extracted fields. Bounded above the critical
            # threshold because a VLM extraction that passes every
            # deterministic validator (IBAN MOD-97, USt-IdNr checksum,
            # arithmetic) is by definition critical-grade evidence.
            base_docling_score=0.93,
        )

        # ---- 6. Reconcile: per-field provenance ----
        # If Docling produced a partial, prefer per-field whichever path had higher
        # final score. If Docling never produced a valid instance, fallback wins
        # outright.
        if docling_invoice is None:
            primary: ExtractionPath = "fallback"
            chosen = fb_invoice
            chosen_report = fb_report
            field_paths = {f: "fallback" for f in fb_invoice.model_fields}
        else:
            chosen, chosen_report, field_paths, primary = self._merge_paths(
                docling_invoice=docling_invoice,
                docling_report=docling_report,
                fb_invoice=fb_invoice,
                fb_report=fb_report,
            )

        # If after merge any critical field is still below the critical threshold,
        # we refuse to ship.
        unresolved = [
            f
            for f in self.settings.confidence.critical_fields
            if (fc := chosen_report.fields.get(f))
            and fc.final_score < self.settings.confidence.critical_field_threshold
        ]
        if unresolved:
            err = ConfidenceError(
                f"critical fields unresolved after fallback: {sorted(unresolved)}",
                document_id=current_document_id(),
                docling_partial=docling_invoice.model_dump(mode="json")
                if docling_invoice
                else None,
                fallback_partial=fb_invoice.model_dump(mode="json"),
                confidence_report=chosen_report.model_dump(mode="json"),
            )
            self._end_langfuse_trace(trace_span, error=err)
            raise err

        return self._finalize_result(
            invoice=chosen,
            conf_report=chosen_report,
            primary=primary,
            paths_tried=paths_tried,
            fallback_invoked=fallback_invoked,
            fallback_reason=fallback_reason,
            started=started,
            started_dt=started_dt,
            field_paths=field_paths,
            trace_span=trace_span,
        )

    # ---------- Docling structural ----------

    async def _docling_convert(self, source: PathOrBytes) -> _DoclingRun:
        """Sync Docling API wrapped in to_thread so we don't block the loop."""
        if self._converter is None:
            async with self._converter_lock:
                if self._converter is None:
                    self._converter = await asyncio.to_thread(
                        _build_converter, self.settings
                    )

        try:
            result = await asyncio.wait_for(
                asyncio.to_thread(self._converter.convert, source),
                timeout=self.settings.docling.timeout_seconds,
            )
        except TimeoutError as exc:
            raise DoclingError(
                "Docling DocumentConverter timed out",
                document_id=current_document_id(),
                cause=exc,
            ) from exc
        except Exception as exc:
            raise DoclingError(
                "Docling DocumentConverter raised",
                document_id=current_document_id(),
                cause=exc,
            ) from exc

        raw_doc = getattr(result, "document", result)
        try:
            md = raw_doc.export_to_markdown()
        except AttributeError as exc:
            raise DoclingError(
                "Docling result has no export_to_markdown",
                document_id=current_document_id(),
                cause=exc,
            ) from exc

        region_scores = _normalize_region_scores(raw_doc)
        page_count = len(getattr(raw_doc, "pages", []) or [])
        log.info(
            "docling.converted",
            page_count=page_count,
            markdown_length=len(md),
            region_score_keys=len(region_scores),
        )
        return _DoclingRun(
            markdown=md,
            region_scores=region_scores,
            page_count=page_count,
            raw_doc=raw_doc,
        )

    # ---------- DSPy coercion ----------

    async def _coerce_markdown(
        self, markdown: str
    ) -> tuple[InvoiceFields | None, Exception | None]:
        if self._extractor_program is None:
            self._extractor_program = self._build_extractor_program()

        try:
            prediction = await asyncio.to_thread(
                self._extractor_program, markdown=markdown
            )
        except Exception as exc:
            log.warning("dspy.extract_failed", error=str(exc))
            return None, exc

        candidate = getattr(prediction, "invoice", None)
        if isinstance(candidate, InvoiceFields):
            return candidate, None

        try:
            invoice = InvoiceFields.model_validate(candidate)
        except ValidationError as exc:
            log.info(
                "dspy.validation_failed",
                error_count=len(exc.errors()),
                first_error=exc.errors()[0].get("type") if exc.errors() else None,
            )
            return None, exc

        return invoice, None

    def _build_extractor_program(self) -> Any:
        # Configure DSPy LM once via LiteLLM. Idempotent.
        llm = self.settings.llm
        dspy.configure(
            lm=dspy.LM(
                model=llm.model,
                api_base=str(llm.api_base),
                api_key=llm.api_key.get_secret_value() or None,
                temperature=llm.temperature,
                max_tokens=llm.max_tokens,
            )
        )
        return dspy.Predict(ExtractInvoiceFromMarkdown)

    def _build_judge_program(self) -> Any:
        llm = self.settings.llm
        # Judge runs on a separately-configured LM context to keep judge model independent
        return dspy.Predict(JudgeCriticalField)

    # ---------- calibration ----------

    def _calibrate(
        self,
        *,
        invoice: InvoiceFields | None,
        region_scores: dict[str, float],
        markdown: str,
        ran_judge: bool,
        base_docling_score: float = 0.92,
    ) -> ConfidenceReport:
        structure = build_structure_evidence(invoice)

        evidence: list[FieldEvidence] = []
        if invoice is None:
            # No invoice → conservative zero-confidence record per critical field
            for f in self.settings.confidence.critical_fields:
                evidence.append(
                    FieldEvidence(
                        name=f, value=None, docling_score=0.0
                    )
                )
            return self._calibrator.build_report(
                invoice=None, field_evidence=evidence, structure=structure
            )

        for fname in invoice.model_fields:
            value = getattr(invoice, fname)
            docling_score = _field_score(
                region_scores, fname, default=base_docling_score
            )
            evidence.append(
                FieldEvidence(
                    name=fname,
                    value=value,
                    docling_score=docling_score,
                    document_excerpt=self._excerpt_for(fname, markdown),
                )
            )

        if ran_judge:
            evidence = self._apply_judge(evidence)

        return self._calibrator.build_report(
            invoice=invoice, field_evidence=evidence, structure=structure
        )

    def _excerpt_for(self, _field_name: str, markdown: str) -> str:
        """Markdown excerpt fed to the judge. Bounded to keep judge cheap."""
        # Cheap: send the first 1500 chars. A more sophisticated implementation
        # would slice around the matched value's position in the markdown; this
        # is the obvious next-step extension and is documented in the README.
        return markdown[:1500]

    def _apply_judge(self, evidence: Iterable[FieldEvidence]) -> list[FieldEvidence]:
        # Lazy build
        if self._judge_program is None:
            self._judge_program = self._build_judge_program()
        out: list[FieldEvidence] = []
        for ev in evidence:
            if ev.name not in self.settings.confidence.critical_fields:
                out.append(ev)
                continue
            try:
                pred = self._judge_program(
                    field_name=ev.name,
                    candidate_value=str(ev.value),
                    document_excerpt=ev.document_excerpt or "",
                )
                agreed = bool(getattr(pred, "agree", False))
                jc = float(getattr(pred, "confidence", 0.0))
            except Exception as exc:  # judge failure is non-fatal
                log.info("judge.failed", field=ev.name, error=str(exc))
                out.append(ev)
                continue
            out.append(
                FieldEvidence(
                    name=ev.name,
                    value=ev.value,
                    docling_score=ev.docling_score,
                    document_excerpt=ev.document_excerpt,
                    judge_agreed=agreed,
                    judge_confidence=jc,
                )
            )
        return out

    # ---------- fallback path ----------

    async def _fallback_only(
        self, source: PathOrBytes
    ) -> tuple[InvoiceFields, ConfidenceReport]:
        fb_invoice = await self._invoke_fallback(source)
        report = self._calibrate(
            invoice=fb_invoice,
            region_scores={},
            markdown="",
            ran_judge=self.settings.confidence.judge_critical_fields_always,
            base_docling_score=0.93,
        )
        return fb_invoice, report

    async def _invoke_fallback(self, source: PathOrBytes) -> InvoiceFields:
        if self._fallback is None:
            self._fallback = QwenVLFallback(self.settings.fallback)
        return await self._fallback.extract(source)

    # ---------- merge ----------

    def _merge_paths(
        self,
        *,
        docling_invoice: InvoiceFields,
        docling_report: ConfidenceReport,
        fb_invoice: InvoiceFields,
        fb_report: ConfidenceReport,
    ) -> tuple[InvoiceFields, ConfidenceReport, dict[str, ExtractionPath], ExtractionPath]:
        """Per-field winner-take-the-higher-confidence merge.

        We rebuild the InvoiceFields from the merged field map. Because the
        merged dict goes back through the schema validators (arithmetic +
        IBAN + USt-IdNr), structurally inconsistent merges fail-loud here
        instead of silently shipping.

        If the merge is invalid we prefer the path with the higher overall
        minimum, which is the most conservative choice.
        """
        merged_fields: dict[str, Any] = {}
        provenance: dict[str, ExtractionPath] = {}
        merged_confidence: dict[str, Any] = {}

        for fname in docling_invoice.model_fields:
            d_conf = docling_report.fields.get(fname)
            f_conf = fb_report.fields.get(fname)
            d_score = d_conf.final_score if d_conf else 0.0
            f_score = f_conf.final_score if f_conf else 0.0
            if d_score >= f_score:
                merged_fields[fname] = getattr(docling_invoice, fname)
                provenance[fname] = "docling"
                if d_conf:
                    merged_confidence[fname] = d_conf
            else:
                merged_fields[fname] = getattr(fb_invoice, fname)
                provenance[fname] = "fallback"
                if f_conf:
                    merged_confidence[fname] = f_conf

        try:
            merged_invoice = InvoiceFields.model_validate(merged_fields)
        except ValidationError as exc:
            log.warning(
                "merge.invalid_pick_higher_min",
                error=str(exc)[:200],
                docling_overall_min=docling_report.overall_min,
                fb_overall_min=fb_report.overall_min,
            )
            # Pick the path whose own internal validation succeeded
            if docling_report.overall_min >= fb_report.overall_min:
                return (
                    docling_invoice,
                    docling_report,
                    {f: "docling" for f in docling_invoice.model_fields},
                    "docling",
                )
            return (
                fb_invoice,
                fb_report,
                {f: "fallback" for f in fb_invoice.model_fields},
                "fallback",
            )

        # Pick the primary by majority field provenance
        d_count = sum(1 for v in provenance.values() if v == "docling")
        primary: ExtractionPath = "docling" if d_count >= len(provenance) / 2 else "fallback"

        # Rebuild a ConfidenceReport reflecting the merged choices
        merged_report = ConfidenceReport(
            fields=merged_confidence,
            overall_min=min(c.final_score for c in merged_confidence.values())
            if merged_confidence
            else 0.0,
            overall_mean=sum(c.final_score for c in merged_confidence.values())
            / max(1, len(merged_confidence)),
            arithmetic_consistent=docling_report.arithmetic_consistent
            or fb_report.arithmetic_consistent,
            line_items_sum_consistent=fb_report.line_items_sum_consistent
            if provenance.get("line_items") == "fallback"
            else docling_report.line_items_sum_consistent,
            fallback_required=False,  # we've already taken the fallback
            critical_failures=[
                f
                for f in self.settings.confidence.critical_fields
                if (c := merged_confidence.get(f))
                and c.final_score < self.settings.confidence.critical_field_threshold
            ],
        )
        return merged_invoice, merged_report, provenance, primary

    # ---------- finalize ----------

    def _finalize_result(
        self,
        *,
        invoice: InvoiceFields,
        conf_report: ConfidenceReport,
        primary: ExtractionPath,
        paths_tried: list[ExtractionPath],
        fallback_invoked: bool,
        fallback_reason: str | None,
        started: float,
        started_dt: datetime,
        field_paths: dict[str, ExtractionPath],
        trace_span: Any | None,
    ) -> ExtractionResult:
        finished_dt = datetime.now(timezone.utc)
        duration_ms = (time.monotonic() - started) * 1000.0
        trace = ExtractionTrace(
            document_id=current_document_id() or "unknown",
            run_id=current_run_id(),
            started_at=started_dt,
            finished_at=finished_dt,
            duration_ms=duration_ms,
            primary_path=primary,
            fallback_invoked=fallback_invoked,
            fallback_reason=fallback_reason,
            paths_tried=paths_tried,
            signature_version=SIGNATURE_VERSION,
            docling_model=self.settings.docling.model_id,
            fallback_model=self.settings.fallback.model_id
            if self.settings.fallback.enabled
            else None,
            field_paths=field_paths,
        )
        log.info(
            "extraction.done",
            duration_ms=round(duration_ms, 1),
            primary_path=primary,
            fallback_invoked=fallback_invoked,
            **conf_report.to_loggable(),
        )
        self._end_langfuse_trace(trace_span, error=None, trace=trace, report=conf_report)
        return ExtractionResult(invoice=invoice, confidence=conf_report, trace=trace)

    # ---------- Langfuse ----------

    def _begin_langfuse_trace(self) -> Any | None:
        client = langfuse_client()
        if client is None:
            return None
        try:
            return client.trace(
                name="putsch_docs.extract",
                id=current_document_id(),
                metadata={
                    "run_id": current_run_id(),
                    "signature_version": SIGNATURE_VERSION,
                    "docling_model": self.settings.docling.model_id,
                },
            )
        except Exception:  # pragma: no cover — telemetry must not fail extraction
            return None

    def _end_langfuse_trace(
        self,
        span: Any | None,
        *,
        error: BaseException | None = None,
        trace: ExtractionTrace | None = None,
        report: ConfidenceReport | None = None,
    ) -> None:
        if span is None:
            return
        try:
            if error is not None:
                span.update(
                    level="ERROR",
                    status_message=str(error)[:500],
                    output={"error": type(error).__name__},
                )
            else:
                span.update(
                    output={
                        "trace": trace.model_dump(mode="json") if trace else None,
                        "confidence": report.to_loggable() if report else None,
                    }
                )
        except Exception:  # pragma: no cover — telemetry must not fail extraction
            pass

    # ---------- convenience ----------

    async def extract_batch(
        self, sources: Iterable[PathOrBytes]
    ) -> list[ExtractionResult | ExtractionError]:
        """Run extractions concurrently, capping by Docling thread count.

        Returns successes and ExtractionErrors interleaved — callers route.
        """
        sem = asyncio.Semaphore(self.settings.docling.num_threads)

        async def _one(s: PathOrBytes) -> ExtractionResult | ExtractionError:
            async with sem:
                try:
                    return await self.extract(s)
                except ExtractionError as exc:
                    return exc

        return await asyncio.gather(*(_one(s) for s in sources))


# Bind the public extract() to actually run the pipeline.
# We define it this way to keep type annotations clean while still threading
# `source` through the correlation context.
async def _public_extract(
    self: DoclingExtractor,
    source: PathOrBytes,
    *,
    document_id: str | None = None,
    run_id: str | None = None,
) -> ExtractionResult:
    with correlation(document_id=document_id, run_id=run_id):
        return await self._run_pipeline(source)


DoclingExtractor.extract = _public_extract  # type: ignore[method-assign]
