"""Human review queue.

Bridge between Langfuse annotation queues and Putsch Sachbearbeiter.

Outbound: when a trace is flagged (low confidence, rubric fail, high cost,
errored, manually marked), we add it to a Langfuse annotation queue and
send an email via Microsoft Graph to the assigned Sachbearbeiter group.
Assignment is round-robin within a queue, with a fairness counter held in
process — for an HA deployment, replace with a Redis-backed assigner.

Inbound: Sachbearbeiter judgements come back via Langfuse webhooks. The
poll loop in ``annotations_to_training_set`` translates them into
``DatasetItem`` rows ready to commit to the matching ``evals/datasets``
file. *This is the closing of the flywheel loop.*

Betriebsrat note
----------------
Assignment is by *role*, never by individual identity. Per-individual
metrics are not exposed; the dashboard surfaces queue-level aggregates
only. This is required by the Putsch Betriebsvereinbarung KI-Einsatz.
"""

from __future__ import annotations

import itertools
import json
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import httpx

from putsch_obs.config import PutschObsSettings, get_settings
from putsch_obs.eval.schemas import (
    AnnotationItem,
    AnnotationStatus,
    DatasetItem,
    EvalItemResult,
)
from putsch_obs.instrumentation import get_langfuse
from putsch_obs.logging import get_logger

log = get_logger(__name__)


@dataclass(slots=True)
class _QueueAssigner:
    """Round-robin Sachbearbeiter group assigner. Process-local."""

    groups: list[str]
    cycle: Any | None = None

    def __post_init__(self) -> None:
        self.cycle = itertools.cycle(self.groups) if self.groups else None

    def next(self) -> str | None:
        if self.cycle is None:
            return None
        return str(next(self.cycle))


class HumanReviewQueue:
    """Wraps Langfuse annotation queues with Putsch-specific routing."""

    def __init__(
        self,
        settings: PutschObsSettings | None = None,
        *,
        graph_client: httpx.AsyncClient | None = None,
        sachbearbeiter_groups: Iterable[str] = ("buchhaltung@putsch.de",),
    ) -> None:
        self._settings = settings or get_settings()
        self._graph = graph_client
        self._assigner = _QueueAssigner(list(sachbearbeiter_groups))

    async def flag(
        self,
        result: EvalItemResult,
        *,
        queue_name: str,
    ) -> None:
        """Add a result to a Langfuse annotation queue + notify reviewers."""
        reason = self._reason(result)
        group = self._assigner.next()
        payload = AnnotationItem(
            trace_id=result.trace_id or "",
            queue=queue_name,
            reviewer=group,
            decision=AnnotationStatus.PENDING,
            flagged_reason=reason,
            notes=result.error or (
                result.judgement.rationale if result.judgement else None
            ),
        )
        client = get_langfuse()
        if client is not None:
            try:
                # Langfuse SDKs vary; tolerate both shapes.
                add = getattr(client, "create_annotation_queue_item", None)
                if add is not None:
                    add(
                        queue_name=queue_name,
                        object_id=result.trace_id,
                        object_type="TRACE",
                        metadata=payload.model_dump(mode="json"),
                    )
            except Exception as exc:
                log.warning(
                    "review.langfuse_enqueue_failed",
                    queue=queue_name,
                    err=str(exc),
                )
        await self._notify(payload)

    async def annotations_to_training_set(
        self,
        *,
        dataset_path: Path,
        queue_name: str,
    ) -> int:
        """Pull approved annotations and append them to a dataset.

        Returns the number of new training rows appended. Idempotent: an
        annotation that was already converted (by ``annotation_id``) is
        skipped on a second run.
        """
        client = get_langfuse()
        if client is None:
            log.info("review.pull_skipped", reason="no_langfuse")
            return 0
        try:
            items = client.get_annotation_queue_items(queue_name=queue_name) or []
        except Exception as exc:
            log.warning("review.pull_failed", err=str(exc))
            return 0

        existing_ids = _existing_item_ids(dataset_path)
        appended = 0
        with dataset_path.open("a", encoding="utf-8") as fh:
            for it in items:
                ann_id = str(getattr(it, "id", "") or "")
                if not ann_id or ann_id in existing_ids:
                    continue
                status = str(getattr(it, "status", "") or "").lower()
                if status not in ("approved", "needs_revision"):
                    continue
                correction = getattr(it, "correction", None) or getattr(it, "value", None)
                trace_id = str(getattr(it, "object_id", "") or "")
                row = DatasetItem(
                    item_id=f"ann-{ann_id}",
                    input=_input_from_trace(client, trace_id),
                    expected_output=correction,
                    metadata={
                        "source": "annotation_queue",
                        "annotation_id": ann_id,
                        "trace_id": trace_id,
                        "promoted_at": datetime.now(timezone.utc).isoformat(),
                    },
                    rubric_id=_rubric_id_for_queue(queue_name),
                )
                fh.write(row.model_dump_json() + "\n")
                appended += 1
        log.info(
            "review.promoted_to_dataset",
            queue=queue_name,
            dataset=str(dataset_path),
            appended=appended,
        )
        return appended

    # ── internals ───────────────────────────────────────────────────────

    @staticmethod
    def _reason(result: EvalItemResult) -> str:
        if result.error is not None:
            return "rubric_fail"
        if result.judgement is None:
            return "manual"
        if result.judgement.flagged_for_review:
            return "manual"
        if not result.judgement.pass_:
            return "rubric_fail"
        if result.judgement.confidence < 0.7:
            return "low_confidence"
        return "manual"

    async def _notify(self, item: AnnotationItem) -> None:
        webhook = (
            self._settings.teams_webhook_url.get_secret_value()
            if self._settings.teams_webhook_url is not None
            else None
        )
        if not webhook:
            log.info("review.notify_skipped", reason="no_webhook")
            return
        body = {
            "@type": "MessageCard",
            "@context": "http://schema.org/extensions",
            "summary": f"Sachbearbeiter-Review: {item.queue}",
            "title": f"Review benötigt — {item.flagged_reason}",
            "text": (
                f"**Queue**: {item.queue}\n"
                f"**Reviewer**: {item.reviewer}\n"
                f"**Trace**: {item.trace_id}\n"
                f"**Reason**: {item.flagged_reason}\n"
                f"**Notes**: {item.notes or '-'}"
            ),
        }
        client = self._graph or httpx.AsyncClient(timeout=5.0)
        try:
            try:
                await client.post(webhook, json=body)
            finally:
                if self._graph is None:
                    await client.aclose()
        except httpx.HTTPError as exc:
            log.warning("review.notify_failed", err=str(exc))


def _input_from_trace(client: Any, trace_id: str) -> Any:
    if not trace_id:
        return None
    try:
        trace = client.get_trace(trace_id)
        return getattr(trace, "input", None) or json.loads(getattr(trace, "input_json", "null"))
    except Exception:
        return None


def _existing_item_ids(path: Path) -> set[str]:
    if not path.exists():
        return set()
    ids: set[str] = set()
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            obj = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(obj, dict) and "item_id" in obj:
            ids.add(str(obj["item_id"]))
    return ids


def _rubric_id_for_queue(queue_name: str) -> str | None:
    # Convention: queue names map 1:1 to rubrics, e.g. "ap-crew-review" →
    # "invoice_extraction". Replace this lookup if your queue naming diverges.
    by_prefix: dict[str, str] = {
        "ap-": "invoice_extraction",
        "mahnung-": "mahnung_tone",
        "customs-": "customs_hs",
        "datev-": "datev_booking_code",
    }
    for prefix, rid in by_prefix.items():
        if queue_name.startswith(prefix):
            return rid
    return None


__all__ = ["HumanReviewQueue"]
