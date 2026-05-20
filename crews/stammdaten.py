"""Vendor / material / customer master-data lookup crew.

Stub implementation backed by an in-memory dict. The real Stammdaten Crew
(build-plan task 1) hits SAP via RFC. This stub exists so the harness can
demonstrate crew-to-crew composition end-to-end before task 1 lands.

Trust note: master-data field contents (name, IBAN, notes) are attacker-
influenced via onboarding submissions. They are returned as opaque strings
in `data` and wrapped as <external_content source="datev"> inside the
summary, so downstream crews and agents see them as evidence, never as
directives. IBAN mutations, in production, must route through a non-LLM
verification path - this stub does NOT model that path; it returns the
stored IBAN as-is and trusts the caller to revalidate.
"""

from __future__ import annotations

import re
from typing import Any, Dict, Optional

from trust import Source, wrap_external

from .base import Crew, CrewOutput


_VAT_ID_RE = re.compile(r"\b(DE\d{6,12})\b")


class StammdatenCrew(Crew):
    """In-memory master-data lookup, keyed by German VAT ID (USt-IdNr.)."""

    def __init__(self, vendors: Dict[str, Dict[str, Any]], name: str = "stammdaten") -> None:
        self._vendors = vendors
        self._name = name

    @property
    def name(self) -> str:
        return self._name

    def run(self, task: str, context: Optional[Dict[str, Any]] = None) -> CrewOutput:
        match = _VAT_ID_RE.search(task)
        if not match:
            return CrewOutput(
                summary="No vendor identifier (USt-IdNr.) found in the task.",
                data={"vendor_id": None, "found": False},
            )

        vendor_id = match.group(1)
        record = self._vendors.get(vendor_id)
        if not record:
            return CrewOutput(
                summary=f"Vendor {vendor_id} not found in master data.",
                data={"vendor_id": vendor_id, "found": False},
            )

        wrapped_name = wrap_external(Source.DATEV, str(record.get("name", "")))
        wrapped_iban = wrap_external(Source.DATEV, str(record.get("iban", "")))
        summary = (
            f"Vendor {vendor_id} located. "
            f"Name: {wrapped_name} "
            f"IBAN (REQUIRES non-LLM revalidation before payment): {wrapped_iban}"
        )
        return CrewOutput(
            summary=summary,
            data={"vendor_id": vendor_id, "found": True, "record": dict(record)},
        )
