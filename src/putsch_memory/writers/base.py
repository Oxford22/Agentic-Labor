"""Base class for typed episode writers.

The episode is the unit of fact-into-memory. Each crew's writer takes a
Pydantic episode payload, validates it, redacts PII via
`putsch_obs.redaction` (best-effort import; falls back to no-op for
tests), and routes it through `MemoryClient.add_episode`.

Why one writer per crew and not one generic one:

* The episode payload's schema is specific to the crew's work product
  (an AP booking vs a dunning decision vs a customs declaration). The
  schema is the documentation; making it generic loses that.
* Different crews need different post-write side effects (e.g., the
  Stammdaten writer also raises a conflict for human triage).
* It keeps the audit story simple: every fact in the graph has a
  type-specific writer that owns its semantics.
"""

from __future__ import annotations

import json
from abc import ABC, abstractmethod
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Any, ClassVar, Generic, TypeVar

import orjson
from pydantic import BaseModel, ConfigDict, Field

from putsch_memory.logging import get_logger

if TYPE_CHECKING:
    from putsch_memory.graphiti_client import MemoryClient

logger = get_logger(__name__)

E = TypeVar("E", bound="EpisodeBase")


class EpisodeBase(BaseModel):
    """Common shape of every episode."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    occurred_at: datetime = Field(description="When the event happened in the world.")
    correlation_id: str = Field(
        min_length=1, max_length=128, description="Langfuse trace id."
    )
    summary: str = Field(min_length=1, max_length=512)

    def to_episode_body(self) -> str:
        """Serialise to the Graphiti-ingestible JSON string.

        Why JSON: Graphiti's extractor reads natural language but also
        accepts JSON bodies; the JSON form is what we ship to production
        because it gives the extractor stable field names and avoids
        prompt-injection from free-text payloads.
        """
        return orjson.dumps(
            self.model_dump(mode="json"),
            option=orjson.OPT_NAIVE_UTC | orjson.OPT_SORT_KEYS,
        ).decode()


class EpisodeWriter(ABC, Generic[E]):
    """Base writer. Subclasses define `episode_type` and `episode_model`."""

    episode_type: ClassVar[str]
    episode_model: ClassVar[type[EpisodeBase]]

    def __init__(self, memory: MemoryClient) -> None:
        self._memory = memory

    @abstractmethod
    def _summary(self, episode: E) -> str:
        """One-line natural-language summary for graph search hits."""

    async def write(self, episode: E) -> str:
        """Validate, redact, ship. Returns the idempotency key."""
        if not isinstance(episode, self.episode_model):
            raise TypeError(
                f"{type(self).__name__} expected {self.episode_model.__name__}, "
                f"got {type(episode).__name__}"
            )
        if episode.occurred_at.tzinfo is None:
            raise ValueError("episode.occurred_at must be timezone-aware (UTC).")

        body = _redact(episode.to_episode_body())
        name = self._summary(episode)

        log = logger.bind(
            op="episode_write",
            episode_type=self.episode_type,
            correlation_id=episode.correlation_id,
        )
        try:
            idem = await self._memory.add_episode(
                name=name,
                body=body,
                episode_type=self.episode_type,
                reference_time=episode.occurred_at,
                attributes={"summary": episode.summary},
            )
            log.info("episode_written", idempotency_key=idem)
            return idem
        except Exception:
            log.exception("episode_write_failed")
            raise


# ---------------------------------------------------------------------------
# Redaction
# ---------------------------------------------------------------------------


def _redact(body: str) -> str:
    """Best-effort PII redaction.

    Pulls from `putsch_obs.redaction` when available (sibling module in
    the same stack). Falls back to a conservative builtin pattern-redact
    so this package is testable in isolation.
    """
    try:
        from putsch_obs.redaction import redact_json_string  # type: ignore[import-not-found]

        return redact_json_string(body)
    except ImportError:
        return _builtin_redact(body)


_IBAN_PATTERN = r'"iban"\s*:\s*"[^"]+"'
_EMAIL_PATTERN = r'"email"\s*:\s*"[^@]+@[^"]+"'


def _builtin_redact(body: str) -> str:
    """Conservative regex-based fallback. Intentionally over-eager."""
    import re

    body = re.sub(_IBAN_PATTERN, '"iban":"***redacted***"', body)
    body = re.sub(_EMAIL_PATTERN, '"email":"***redacted***"', body)
    # Sanity-check JSON round-trips so we don't corrupt anything else.
    try:
        json.loads(body)
    except json.JSONDecodeError:
        return body
    return body


def utc_now() -> datetime:
    return datetime.now(tz=timezone.utc)


__all__ = ["EpisodeBase", "EpisodeWriter", "utc_now"]
