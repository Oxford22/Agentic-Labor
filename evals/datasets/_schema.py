"""Dataset entry schema. CI enforces this on every JSONL line.

Every dataset row has *two* required parts:

1. **Payload** — the input + output fields matching the signature's DSPy schema.
2. **Provenance** — ``labeled_by``, ``labeled_at``, ``label_confidence``, optional
   ``source_trace_id``. No anonymous labels. No labels without a date.

The Pydantic model below validates the provenance fields. Payload validation lives in the
signature itself — we round-trip each row through ``dspy.Example`` + ``signature.fields`` for
that. Splitting the two concerns keeps the schema generic.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, field_validator


class DatasetEntry(BaseModel):
    """Provenance-bearing dataset row. Payload keys are the signature's input + output fields."""

    model_config = ConfigDict(extra="allow", frozen=True)

    labeled_by: str = Field(
        ...,
        min_length=3,
        description="LDAP / email of the labeler. Anonymous labels are rejected.",
    )
    labeled_at: datetime
    label_confidence: float = Field(..., ge=0.0, le=1.0)
    source_trace_id: str | None = Field(
        default=None,
        description=(
            "Langfuse trace id if this row was pulled from the annotation queue. "
            "Null for hand-seeded entries."
        ),
    )

    @field_validator("labeled_by")
    @classmethod
    def _no_bots(cls, value: str) -> str:
        if value.endswith("bot") and "agentic-platform-bot" not in value:
            raise ValueError("labels from auto-generated bot accounts are rejected")
        return value


def payload_keys(entry: dict[str, Any]) -> dict[str, Any]:
    """Strip provenance keys; return what the signature should see."""

    provenance = {"labeled_by", "labeled_at", "label_confidence", "source_trace_id"}
    return {k: v for k, v in entry.items() if k not in provenance}
