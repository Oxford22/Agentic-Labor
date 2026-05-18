"""Langfuse instrumentation.

Two roles:

1. Every signature call in production opens a Langfuse span tagged with the active
   ``compiled_artifact_id`` and ``compiled_artifact_version``. Rollback investigation starts with a
   Langfuse query by these tags.

2. Every compilation run opens a Langfuse "compilation report" trace, with the dataset hash,
   candidate models tried, scores, and the diff vs the previous artifact attached as observation
   metadata. The Sachbearbeiter UI links into these reports.

Both flows tolerate Langfuse being unreachable — observability degrades to structlog. We do not
fail customer requests because Langfuse is down.
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from functools import lru_cache
from typing import Any

from putsch_compile.config import get_settings
from putsch_compile.logging import get_correlation_id, get_logger

_log = get_logger(__name__)


@lru_cache(maxsize=1)
def get_langfuse() -> Any:
    """Lazy. Returns a Langfuse client or ``None`` if disabled / unreachable."""

    settings = get_settings().langfuse
    if not settings.enabled:
        return None
    try:
        from langfuse import Langfuse

        return Langfuse(
            host=settings.host,
            public_key=settings.public_key.get_secret_value(),
            secret_key=settings.secret_key.get_secret_value(),
            flush_at=settings.flush_at,
        )
    except Exception as exc:  # pragma: no cover - import / network
        _log.warning("langfuse.init_failed", error=str(exc))
        return None


@contextmanager
def signature_call_span(
    *,
    signature_name: str,
    artifact_id: str,
    artifact_version: str,
    model: str,
    inputs: dict[str, Any] | None = None,
) -> Iterator[Any]:
    """Open a span for a single signature call in production.

    The span carries the artifact id + version — these are the join key for rollback.
    """

    client = get_langfuse()
    if client is None:
        yield None
        return

    correlation = get_correlation_id()
    span = client.trace(
        name=f"signature.{signature_name}",
        metadata={
            "compiled_artifact_id": artifact_id,
            "compiled_artifact_version": artifact_version,
            "model": model,
            "correlation_id": correlation,
        },
        input=inputs,
        tags=["signature", signature_name, f"artifact:{artifact_id}"],
    )
    try:
        yield span
    finally:
        try:
            client.flush()
        except Exception:  # pragma: no cover - network
            pass


@contextmanager
def compilation_report(
    *,
    signature_name: str,
    dataset_hash: str,
    seed: int,
    metadata: dict[str, Any] | None = None,
) -> Iterator[Any]:
    """Open a compilation report trace.

    Compilation reports are what Wirtschaftsprüfer auditors see — every artifact promoted to prod
    has a trace link with dataset hash, candidate models, scores, and diff vs prior.
    """

    client = get_langfuse()
    if client is None:
        yield None
        return

    span = client.trace(
        name=f"compile.{signature_name}",
        metadata={"dataset_hash": dataset_hash, "seed": seed, **(metadata or {})},
        tags=["compilation", signature_name],
    )
    try:
        yield span
    finally:
        try:
            client.flush()
        except Exception:  # pragma: no cover - network
            pass
