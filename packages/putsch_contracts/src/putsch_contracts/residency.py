"""EU residency primitives.

The Putsch deployment is Frankfurt-pinned per ``ARCHITECTURE.md``. This
module gives every other package a single import path for region checks,
so the rule is enforced as Python rather than as a tribal convention.
"""

from __future__ import annotations

from enum import StrEnum
from typing import Final

ALLOWED_REGIONS: Final[frozenset[str]] = frozenset(
    {
        "eu-central-1",
        "eu-central-2",
        "eu-west-1",
        "eu-west-3",
        "eu-north-1",
        "eu-south-1",
        "eu-south-2",
        "hetzner-fsn1",
        "hetzner-nbg1",
        "hetzner-hel1",
        "mistral-paris",
    }
)

FORBIDDEN_REGION_PREFIXES: Final[tuple[str, ...]] = (
    "us-east-",
    "us-west-",
    "us-gov-",
    "ap-",
    "sa-",
    "af-",
    "ca-",
    "cn-",
    "me-",
)


class ResidencyError(ValueError):
    """Raised when a non-EU region is configured anywhere in production paths."""


class DataClassification(StrEnum):
    """GDPR-aligned classifications used to tag payloads at boundaries.

    Modules tag every payload they emit so ``putsch_obs`` can apply the
    appropriate redaction policy without inspecting content.
    """

    PUBLIC = "public"
    INTERNAL = "internal"
    PII_DIRECT = "pii_direct"
    PII_DERIVED = "pii_derived"
    PERSONNEL = "personnel"
    FINANCIAL = "financial"
    CONFIDENTIAL = "confidential"


def validate_region(region: str) -> str:
    """Return ``region`` unchanged if it is allowed.

    Raises:
        ResidencyError: when ``region`` is empty, on the forbidden list,
            or not on the allowed list.
    """
    if not region:
        raise ResidencyError("region must be non-empty")
    normalized = region.strip().lower()
    if any(normalized.startswith(prefix) for prefix in FORBIDDEN_REGION_PREFIXES):
        raise ResidencyError(
            f"region {region!r} is outside the EU jurisdiction allowed by "
            "ARCHITECTURE.md; configure eu-central-1, Hetzner FSN1/NBG1, "
            "or mistral-paris"
        )
    if normalized not in ALLOWED_REGIONS:
        raise ResidencyError(
            f"region {region!r} is not on the explicit allow-list. Add to "
            "ALLOWED_REGIONS only after legal review."
        )
    return normalized
