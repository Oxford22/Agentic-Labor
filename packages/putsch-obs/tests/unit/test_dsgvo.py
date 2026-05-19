"""DSGVO registry + generator tests."""

from __future__ import annotations

import pytest
import yaml

from putsch_obs.dsgvo import (
    DataCategory,
    LegalBasis,
    ProcessingActivity,
    generate_verzeichnis,
    generate_yaml,
    register_service,
    registered_activities,
)
from putsch_obs.dsgvo.registry import DataSubject, reset_registry_for_test


@pytest.fixture(autouse=True)
def _clear_registry() -> None:
    reset_registry_for_test()
    yield
    reset_registry_for_test()


def _make() -> ProcessingActivity:
    return ProcessingActivity(
        service_name="ap-crew",
        bezeichnung="KI-Rechnungsverarbeitung",
        zweck="Extraktion und DATEV-Buchung",
        rechtsgrundlage=LegalBasis.CONTRACT,
        betroffene_personen=(DataSubject.SUPPLIERS,),
        datenkategorien=(DataCategory.TAX_IDS, DataCategory.INVOICE_LINE_ITEMS),
    )


def test_register_and_emit_yaml() -> None:
    register_service(_make())
    data = yaml.safe_load(generate_yaml())
    assert data["verantwortlicher"] == "Putsch GmbH & Co. KG"
    assert len(data["aktivitaeten"]) == 1
    assert data["aktivitaeten"][0]["service_name"] == "ap-crew"


def test_markdown_contains_required_sections() -> None:
    register_service(_make())
    md = generate_verzeichnis()
    for line in (
        "Zweck",
        "Rechtsgrundlage",
        "Betroffene Personen",
        "Empfänger",
        "Aufbewahrungsfrist",
        "ap-crew",
    ):
        assert line in md


def test_third_country_transfer_rejected() -> None:
    with pytest.raises(Exception):
        ProcessingActivity(
            service_name="forbidden-service",
            bezeichnung="x",
            zweck="x",
            rechtsgrundlage=LegalBasis.CONTRACT,
            drittland_transfers=("US",),
        )


def test_registry_overwrites_by_service_name() -> None:
    register_service(_make())
    register_service(_make())  # same service_name
    assert len(list(registered_activities())) == 1
