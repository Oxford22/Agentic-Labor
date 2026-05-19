"""Customer / vendor email thread ingestion.

Threads come through Docling + the email gateway; we receive structured
JSON (sender, recipients, subject, body in plain text, attachments
extracted). This pipeline parses the thread into communication episodes
and links them to the relevant Lieferant / Kunde via the
sender / recipient mapping.

PII redaction is mandatory and happens at the writer layer. We never
persist raw bodies; we persist a redacted summary + structured facts
extracted by Granite-Docling.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from datetime import datetime
from typing import TYPE_CHECKING, Any

from pydantic import BaseModel, ConfigDict, Field

from putsch_memory.graphiti_client import ProvenanceContext
from putsch_memory.logging import get_logger
from putsch_memory.ontology import SourceSystem

if TYPE_CHECKING:
    from putsch_memory.graphiti_client import MemoryClient

logger = get_logger(__name__)


class EmailMessage(BaseModel):
    """One message in a thread."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    message_id: str
    from_addr: str = Field(min_length=3, max_length=320)
    to_addrs: tuple[str, ...]
    cc_addrs: tuple[str, ...] = ()
    subject: str = Field(default="", max_length=512)
    body_plain: str = Field(default="", max_length=64 * 1024)
    received_at: datetime
    direction: str = Field(description="'inbound' or 'outbound'")


@dataclass(slots=True, frozen=True)
class EmailThread:
    thread_id: str
    counterparty_entity_id: str | None
    counterparty_entity_label: str | None
    messages: tuple[EmailMessage, ...]
    trace_id: str


async def ingest_email_thread(
    client: MemoryClient, thread: EmailThread
) -> int:
    """Persist a thread as a chain of communication episodes."""
    log = logger.bind(op="ingest_email_thread", thread_id=thread.thread_id)
    if not thread.messages:
        return 0

    async with ProvenanceContext(
        source_system=SourceSystem.EMAIL,
        written_by_agent="email_ingestion",
        trace_id=thread.trace_id,
    ):
        n = 0
        for msg in thread.messages:
            summary = _redacted_summary(msg)
            body = summary.model_dump_json()
            await client.add_episode(
                name=f"Email {msg.direction} {msg.message_id}",
                body=body,
                episode_type="communication_email",
                reference_time=msg.received_at,
                attributes={
                    "thread_id": thread.thread_id,
                    "counterparty_entity_id": thread.counterparty_entity_id,
                    "counterparty_entity_label": thread.counterparty_entity_label,
                    "direction": msg.direction,
                },
            )
            n += 1

    log.info("email_thread_ingested", messages=n)
    return n


class _RedactedSummary(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    message_id: str
    from_hash: str
    to_count: int
    cc_count: int
    subject: str
    body_excerpt: str
    received_at: datetime
    direction: str


def _redacted_summary(msg: EmailMessage) -> _RedactedSummary:
    """Hash the sender and truncate the body. The full content goes to
    the document store with stricter access controls, not the graph."""
    from_hash = hashlib.sha256(msg.from_addr.lower().encode()).hexdigest()[:16]
    excerpt = msg.body_plain.strip().replace("\n", " ")[:280]
    return _RedactedSummary(
        message_id=msg.message_id,
        from_hash=from_hash,
        to_count=len(msg.to_addrs),
        cc_count=len(msg.cc_addrs),
        subject=msg.subject[:200],
        body_excerpt=excerpt,
        received_at=msg.received_at,
        direction=msg.direction,
    )


def _ensure_summary_shape(s: Any) -> _RedactedSummary:
    return s if isinstance(s, _RedactedSummary) else _RedactedSummary.model_validate(s)
