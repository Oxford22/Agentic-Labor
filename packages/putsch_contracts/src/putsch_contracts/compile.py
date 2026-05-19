"""Compiled-signature registry contracts.

What ``putsch_compile.registry`` returns and what every consumer
(``putsch-docs``, ``putsch-swarm`` workers) reads. The actual DSPy
program is opaque to consumers — they receive a handle and a metric
threshold, then invoke through the registry, which dispatches to the
LiteLLM-fronted vLLM cluster per ARCHITECTURE.md.
"""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from enum import StrEnum
from typing import Annotated

from pydantic import BaseModel, ConfigDict, Field, StringConstraints


class ModelTier(StrEnum):
    """The cheapest-model-first ladder per ADR-006."""

    NANO = "nano"
    SMALL = "small"
    MEDIUM = "medium"
    LARGE = "large"
    FRONTIER = "frontier"


class SignatureMetric(BaseModel):
    """Threshold contract for a compiled signature.

    The registry refuses to promote an artifact unless the harness can
    show ``score >= threshold - tolerance`` on the holdout. The CI gate
    in ``compile-on-pr.yml`` reads the same thresholds.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    name: str = Field(min_length=1, max_length=64)
    threshold: float = Field(ge=0.0, le=1.0)
    tolerance: float = Field(ge=0.0, le=0.5, default=0.02)
    cost_ceiling_eur_per_1k: Decimal | None = Field(
        default=None, ge=0, max_digits=10, decimal_places=4
    )


_SignatureName = Annotated[
    str,
    StringConstraints(pattern=r"^[a-z][a-z0-9_]{1,62}$"),
]


class CompiledSignature(BaseModel):
    """Handle for a compiled DSPy signature, returned by the registry.

    Consumers should never construct one directly; they get it from
    ``CompileRegistryProtocol.get``. The opaque ``artifact_uri`` points
    at the MinIO blob the registry serves; ``program_hash`` is the git
    SHA of the signature definition plus a hash of the dataset used.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    name: _SignatureName
    version: str = Field(pattern=r"^\d+\.\d+\.\d+$")
    program_hash: Annotated[
        str,
        StringConstraints(pattern=r"^[0-9a-f]{40,64}$"),
    ]
    artifact_uri: str = Field(pattern=r"^s3://[a-z0-9\-./_]+$")
    tier: ModelTier
    metric: SignatureMetric
    owner_team: str = Field(min_length=1, max_length=64)
    promoted_at: datetime = Field(default_factory=lambda: datetime.now(UTC))


class RegistryEntry(BaseModel):
    """A row in the registry catalog ledger.

    ``status='promoted'`` is the only state consumers see by default;
    ``shadow`` is a candidate under nightly eval and ``rolled_back`` is
    history. The lookup is by (``name``, ``status='promoted'``).
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    signature: CompiledSignature
    status: str = Field(pattern=r"^(promoted|shadow|rolled_back)$")
    notes: str | None = Field(default=None, max_length=2048)
