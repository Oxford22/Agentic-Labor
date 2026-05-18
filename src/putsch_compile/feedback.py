"""Annotation feedback loop. The flywheel.

Lifecycle of a Sachbearbeiter correction:

1. Production agent emits a Langfuse trace with input + output + ``compiled_artifact_id``.
2. Sachbearbeiter opens the trace in Langfuse, opens an annotation, corrects the model output.
3. This module pulls the annotation, validates it against the signature schema, dedupes by
   ``source_trace_id``, and appends a new row to ``evals/datasets/<signature>.jsonl``.
4. A scheduled CI workflow runs ``putsch-compile compile <signature>`` and opens a PR if a cheaper
   model now meets threshold or accuracy improves above tolerance.

The metric ``compile.feedback.examples_absorbed`` is emitted to Langfuse every run — the
README-promised proof that the loop is closed. If that counter doesn't move week-over-week, the
loop is broken and the Sachbearbeiter's corrections are dead-lettered. That is a P1 alert.
"""

from __future__ import annotations

import asyncio
import subprocess
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Final

import orjson
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from putsch_compile.config import get_settings
from putsch_compile.exceptions import DatasetError
from putsch_compile.logging import correlation_scope, get_logger
from putsch_compile.signatures import SIGNATURE_REGISTRY
from putsch_compile.tracing import get_langfuse

_log = get_logger(__name__)


_PROVENANCE_KEYS: Final[set[str]] = {
    "labeled_by",
    "labeled_at",
    "label_confidence",
    "source_trace_id",
}


class AbsorbedRow(BaseModel):
    """What got committed back. Surfaced in the Langfuse metric."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    signature: str
    source_trace_id: str
    labeled_by: str
    label_confidence: float = Field(..., ge=0.0, le=1.0)


class SyncReport(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    signature: str
    pulled: int
    appended: int
    duplicates_skipped: int
    invalid_skipped: int
    git_branch: str | None = None
    git_commit: str | None = None
    rows: list[AbsorbedRow]


class FeedbackSync:
    """Pulls annotations from Langfuse and appends them to the dataset for one signature."""

    def __init__(self, *, dry_run: bool = False) -> None:
        self._dry_run = dry_run
        self._settings = get_settings()

    async def sync_signature(
        self,
        signature_name: str,
        *,
        since: datetime | None = None,
        committed_by: str = "agentic-platform-bot@putsch.example",
    ) -> SyncReport:
        if signature_name not in SIGNATURE_REGISTRY:
            raise DatasetError(
                f"unknown signature {signature_name!r}",
                context={"known": sorted(SIGNATURE_REGISTRY)},
            )
        dataset_path = (
            self._settings.absolute_dataset_dir / f"{signature_name}.jsonl"
        )
        existing_trace_ids = _existing_trace_ids(dataset_path)
        with correlation_scope(signature=signature_name, mode="feedback_sync"):
            annotations = await asyncio.to_thread(self._pull_annotations, signature_name, since)

            absorbed: list[AbsorbedRow] = []
            invalid = 0
            duplicates = 0
            new_lines: list[str] = []

            for ann in annotations:
                row, error = _annotation_to_row(ann, signature_name=signature_name)
                if error is not None:
                    invalid += 1
                    _log.warning(
                        "feedback.row_rejected",
                        signature=signature_name,
                        trace_id=ann.get("trace_id"),
                        error=error,
                    )
                    continue
                if row["source_trace_id"] in existing_trace_ids:
                    duplicates += 1
                    continue
                new_lines.append(orjson.dumps(row).decode("utf-8"))
                absorbed.append(
                    AbsorbedRow(
                        signature=signature_name,
                        source_trace_id=row["source_trace_id"],
                        labeled_by=row["labeled_by"],
                        label_confidence=row["label_confidence"],
                    )
                )
                existing_trace_ids.add(row["source_trace_id"])

            branch: str | None = None
            commit: str | None = None
            if new_lines and not self._dry_run:
                _append_jsonl(dataset_path, new_lines)
                branch, commit = self._commit_to_git(
                    dataset_path=dataset_path,
                    signature_name=signature_name,
                    n=len(new_lines),
                    committed_by=committed_by,
                )

            _emit_metric(signature_name, absorbed)

            return SyncReport(
                signature=signature_name,
                pulled=len(annotations),
                appended=len(new_lines),
                duplicates_skipped=duplicates,
                invalid_skipped=invalid,
                git_branch=branch,
                git_commit=commit,
                rows=absorbed,
            )

    # ------------------------------------------------------------------
    # Internal — Langfuse pull
    # ------------------------------------------------------------------

    def _pull_annotations(
        self, signature_name: str, since: datetime | None
    ) -> list[dict[str, Any]]:
        client = get_langfuse()
        if client is None:
            _log.warning(
                "feedback.langfuse_unavailable", signature=signature_name
            )
            return []
        try:
            result = client.fetch_annotation_queue_items(
                queue_name=f"signature.{signature_name}",
                from_timestamp=since,
                state="completed",
            )
        except Exception as exc:  # pragma: no cover - network
            _log.warning(
                "feedback.fetch_failed", signature=signature_name, error=str(exc)
            )
            return []
        items = getattr(result, "data", None) or []
        return [self._normalize_annotation(item) for item in items]

    def _normalize_annotation(self, item: Any) -> dict[str, Any]:
        # Langfuse SDK returns objects; flatten to dict for downstream code.
        return {
            "trace_id": getattr(item, "trace_id", None),
            "labeled_by": getattr(item, "annotator_email", None)
            or getattr(item, "user_id", None),
            "labeled_at": getattr(item, "completed_at", None)
            or getattr(item, "updated_at", None)
            or datetime.now(UTC),
            "label_confidence": getattr(item, "confidence", 1.0),
            "payload": getattr(item, "corrected_output", None) or getattr(item, "value", {}),
            "input_snapshot": getattr(item, "input", None) or {},
        }

    # ------------------------------------------------------------------
    # Internal — git commit
    # ------------------------------------------------------------------

    def _commit_to_git(
        self,
        *,
        dataset_path: Path,
        signature_name: str,
        n: int,
        committed_by: str,
    ) -> tuple[str, str]:
        gs = self._settings.git_feedback
        branch = f"{gs.branch_prefix}{signature_name}-{datetime.now(UTC).strftime('%Y%m%d-%H%M%S')}"
        env = {
            "GIT_AUTHOR_NAME": gs.service_account_name,
            "GIT_AUTHOR_EMAIL": gs.service_account_email,
            "GIT_COMMITTER_NAME": gs.service_account_name,
            "GIT_COMMITTER_EMAIL": gs.service_account_email,
        }
        repo_root = self._settings.repo_root
        cmds = [
            ["git", "checkout", "-B", branch],
            ["git", "add", str(dataset_path.relative_to(repo_root))],
            [
                "git",
                "commit",
                "-m",
                f"feedback({signature_name}): absorb {n} Sachbearbeiter corrections\n\n"
                f"Committed by: {committed_by}",
            ],
        ]
        for cmd in cmds:
            result = subprocess.run(  # noqa: S603 - controlled args
                cmd,
                cwd=repo_root,
                env={**env, "PATH": "/usr/bin:/bin:/usr/local/bin"},
                check=False,
                capture_output=True,
                text=True,
            )
            if result.returncode != 0:
                _log.warning(
                    "feedback.git_failed",
                    cmd=" ".join(cmd),
                    stdout=result.stdout,
                    stderr=result.stderr,
                )
                return branch, ""

        sha = subprocess.run(  # noqa: S603
            ["git", "rev-parse", "HEAD"],
            cwd=repo_root,
            check=False,
            capture_output=True,
            text=True,
        ).stdout.strip()
        return branch, sha


# -----------------------------------------------------------------------------
# Helpers
# -----------------------------------------------------------------------------


def _existing_trace_ids(path: Path) -> set[str]:
    if not path.exists():
        return set()
    out: set[str] = set()
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        try:
            row = orjson.loads(line)
        except orjson.JSONDecodeError:
            continue
        tid = row.get("source_trace_id")
        if tid:
            out.add(str(tid))
    return out


def _annotation_to_row(
    ann: dict[str, Any], *, signature_name: str
) -> tuple[dict[str, Any], str | None]:
    """Convert one Langfuse annotation to a dataset row. Return (row, error_or_none)."""

    payload = ann.get("payload") or {}
    if not isinstance(payload, dict) or not payload:
        return {}, "empty payload"
    labeled_by = (ann.get("labeled_by") or "").strip()
    if not labeled_by or "@" not in labeled_by:
        return {}, f"invalid labeled_by: {labeled_by!r}"
    labeled_at = ann.get("labeled_at")
    if not isinstance(labeled_at, datetime):
        return {}, "missing or non-datetime labeled_at"

    inputs = ann.get("input_snapshot") or {}
    if not isinstance(inputs, dict) or not inputs:
        return {}, "missing input snapshot"

    row: dict[str, Any] = {
        **inputs,
        **payload,
        "labeled_by": labeled_by,
        "labeled_at": labeled_at.astimezone(UTC).isoformat(),
        "label_confidence": float(ann.get("label_confidence") or 1.0),
        "source_trace_id": str(ann.get("trace_id") or ""),
    }
    if not row["source_trace_id"]:
        return {}, "missing trace_id"

    # Refuse if the row hasn't got at least one of the signature's output fields. Otherwise we'd
    # absorb a sample that the metric can't grade.
    sig = SIGNATURE_REGISTRY[signature_name]
    output_fields = [
        name
        for name, field in sig.iter_dspy_fields().items()
        if (getattr(field, "json_schema_extra", None) or {}).get("__dspy_field_type") == "output"
    ]
    if not any(field in payload for field in output_fields):
        return {}, "payload has none of the signature's output fields"
    return row, None


def _append_jsonl(path: Path, lines: list[str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as fp:
        for line in lines:
            fp.write(line)
            fp.write("\n")


def _emit_metric(signature_name: str, rows: list[AbsorbedRow]) -> None:
    """Emit ``compile.feedback.examples_absorbed`` to Langfuse — the loop-closed proof."""

    client = get_langfuse()
    if client is None:
        return
    try:
        client.event(
            name="compile.feedback.examples_absorbed",
            metadata={
                "signature": signature_name,
                "count": len(rows),
                "trace_ids": [r.source_trace_id for r in rows],
            },
        )
        client.flush()
    except Exception:  # pragma: no cover - network
        return


def validate_dataset_file(path: Path) -> int:
    """Strict Pydantic validation of every line. Used by the CI eval-dataset-schema job."""

    from evals.datasets._schema import DatasetEntry  # late: keep evals importable standalone

    count = 0
    for lineno, line in enumerate(
        path.read_text(encoding="utf-8").splitlines(), start=1
    ):
        if not line.strip():
            continue
        try:
            row = orjson.loads(line)
        except orjson.JSONDecodeError as exc:
            raise DatasetError(
                f"invalid JSON in {path}:{lineno}", context={"line": lineno}
            ) from exc
        try:
            DatasetEntry.model_validate(row)
        except ValidationError as exc:
            raise DatasetError(
                f"row {lineno} in {path.name} fails schema: {exc.errors()[0]['msg']}",
                context={"line": lineno, "errors": exc.errors()},
            ) from exc
        for k in _PROVENANCE_KEYS - {"source_trace_id"}:
            if k not in row:
                raise DatasetError(
                    f"row {lineno} missing required provenance key {k}",
                    context={"line": lineno},
                )
        count += 1
    return count
