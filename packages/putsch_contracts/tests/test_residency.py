"""Residency validator covers the regions the CI guardrail checks."""

from __future__ import annotations

import pytest
from putsch_contracts.residency import (
    ALLOWED_REGIONS,
    DataClassification,
    ResidencyError,
    validate_region,
)


@pytest.mark.parametrize(
    "region",
    [
        "eu-central-1",
        "eu-west-1",
        "hetzner-fsn1",
        "hetzner-nbg1",
        "mistral-paris",
    ],
)
def test_allowed_regions_pass(region: str) -> None:
    assert validate_region(region) == region.lower()


@pytest.mark.parametrize(
    "region",
    [
        "us-east-1",
        "us-east-2",
        "us-west-1",
        "us-west-2",
        "us-gov-east-1",
        "ap-southeast-1",
        "cn-north-1",
    ],
)
def test_forbidden_regions_rejected(region: str) -> None:
    with pytest.raises(ResidencyError):
        validate_region(region)


def test_unknown_region_rejected() -> None:
    with pytest.raises(ResidencyError):
        validate_region("mars-1")


def test_empty_region_rejected() -> None:
    with pytest.raises(ResidencyError):
        validate_region("")


def test_data_classification_is_str_enum() -> None:
    assert DataClassification.PII_DIRECT == "pii_direct"
    assert DataClassification.PERSONNEL.value == "personnel"


def test_allowed_regions_immutable() -> None:
    with pytest.raises((AttributeError, TypeError)):
        ALLOWED_REGIONS.add("us-east-1")  # type: ignore[attr-defined]
