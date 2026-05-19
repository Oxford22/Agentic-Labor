"""Dataset loading and Langfuse sync.

Datasets live in ``evals/datasets/*.jsonl``. Each file is a single dataset,
and the file's mtime + content hash is the dataset's version. We never
edit datasets only in the Langfuse UI: that path leads to silent drift
between what production trained on and what we think it trained on. The
canonical artefact is the JSONL on disk.

Sync semantics
--------------
``sync_to_langfuse`` is idempotent:

* It creates the dataset object if absent.
* It upserts each item by ``item_id``.
* If an item is present in Langfuse but absent in the file, it is
  *archived* (not deleted) so historical eval runs still resolve.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from putsch_obs.eval.schemas import DatasetItem
from putsch_obs.exceptions import DatasetError
from putsch_obs.instrumentation import get_langfuse
from putsch_obs.logging import get_logger

log = get_logger(__name__)


DEFAULT_DATASET_ROOT = Path("evals/datasets")


@dataclass(slots=True, frozen=True)
class EvalDataset:
    """A loaded dataset, ready to feed the runner."""

    name: str
    version: str
    items: tuple[DatasetItem, ...]
    source_path: Path

    def __iter__(self) -> Iterator[DatasetItem]:
        return iter(self.items)

    def __len__(self) -> int:
        return len(self.items)


def load_dataset(name: str, *, root: Path | None = None) -> EvalDataset:
    """Load ``evals/datasets/{name}.jsonl``.

    The version is ``sha256(file_bytes)[:12]`` so any edit produces a new
    version automatically. This is what Langfuse will see; CI uses it to
    detect dataset churn.
    """
    base = Path(root or DEFAULT_DATASET_ROOT)
    path = base / f"{name}.jsonl"
    if not path.exists():
        raise DatasetError(f"dataset not found at {path}")
    raw = path.read_bytes()
    version = hashlib.sha256(raw).hexdigest()[:12]
    items: list[DatasetItem] = []
    for line_no, line in enumerate(raw.decode("utf-8").splitlines(), start=1):
        line = line.strip()
        if not line:
            continue
        try:
            obj = json.loads(line)
        except json.JSONDecodeError as exc:
            raise DatasetError(
                f"{path}:{line_no} — invalid JSON: {exc}"
            ) from exc
        try:
            items.append(DatasetItem.model_validate(obj))
        except Exception as exc:
            raise DatasetError(f"{path}:{line_no} — invalid item: {exc}") from exc
    if not items:
        raise DatasetError(f"{path} is empty")
    # ID uniqueness invariant.
    ids = [i.item_id for i in items]
    if len(set(ids)) != len(ids):
        dupes = sorted({i for i in ids if ids.count(i) > 1})
        raise DatasetError(f"{path} contains duplicate item_ids: {dupes}")
    return EvalDataset(name=name, version=version, items=tuple(items), source_path=path)


def sync_to_langfuse(dataset: EvalDataset) -> dict[str, Any]:
    """Mirror the local dataset into Langfuse.

    Returns a summary dict: ``{"created": int, "updated": int, "archived": int}``.
    If Langfuse is unconfigured (no creds), returns ``{"skipped": True}`` and
    logs at INFO — useful for local development.
    """
    client = get_langfuse()
    if client is None:
        log.info("dataset.sync_skipped", reason="no_langfuse", name=dataset.name)
        return {"skipped": True}

    created, updated, archived = 0, 0, 0
    dataset_label = f"{dataset.name}@{dataset.version}"
    try:
        client.create_dataset(
            name=dataset.name,
            description=f"git-versioned dataset (version {dataset.version})",
            metadata={"version": dataset.version, "source": str(dataset.source_path)},
        )
    except Exception as exc:
        # Most likely "already exists" — tolerate. Other errors will surface
        # on the upsert path.
        log.info("dataset.create_idempotent", name=dataset.name, err=str(exc))

    existing_ids: set[str] = set()
    try:
        existing = client.get_dataset_items(dataset_name=dataset.name)
        for it in existing or []:
            ext = getattr(it, "metadata", None) or {}
            ext_id = (ext or {}).get("item_id") or getattr(it, "id", None)
            if ext_id:
                existing_ids.add(str(ext_id))
    except Exception as exc:
        log.warning("dataset.list_failed", err=str(exc))

    seen: set[str] = set()
    for item in dataset.items:
        seen.add(item.item_id)
        try:
            client.create_dataset_item(
                dataset_name=dataset.name,
                input=item.input,
                expected_output=item.expected_output,
                metadata={
                    **item.metadata,
                    "item_id": item.item_id,
                    "rubric_id": item.rubric_id,
                    "dataset_version": dataset.version,
                },
                id=item.item_id,  # stable: upserts in Langfuse
            )
            if item.item_id in existing_ids:
                updated += 1
            else:
                created += 1
        except Exception as exc:
            log.warning(
                "dataset.upsert_failed",
                item_id=item.item_id,
                err=str(exc),
            )

    for stale in existing_ids - seen:
        try:
            # Langfuse SDKs have evolved; tolerate both `archive` and
            # `update_dataset_item(status='ARCHIVED')`.
            archive = getattr(client, "archive_dataset_item", None)
            if archive is not None:
                archive(dataset_name=dataset.name, id=stale)
            else:
                client.update_dataset_item(
                    dataset_name=dataset.name, id=stale, status="ARCHIVED"
                )
            archived += 1
        except Exception as exc:
            log.warning("dataset.archive_failed", item_id=stale, err=str(exc))

    log.info(
        "dataset.synced",
        name=dataset.name,
        version=dataset.version,
        created=created,
        updated=updated,
        archived=archived,
    )
    return {
        "label": dataset_label,
        "created": created,
        "updated": updated,
        "archived": archived,
    }


__all__ = [
    "DEFAULT_DATASET_ROOT",
    "DatasetItem",
    "EvalDataset",
    "load_dataset",
    "sync_to_langfuse",
]
