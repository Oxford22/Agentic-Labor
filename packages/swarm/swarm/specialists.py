"""Putsch-domain worker specialists.

The four functional areas a single customs case touches at Putsch -
procurement, finance, logistics, master data - plus three execution
specialists that run on narrower, cheaper models: SAP RFC code generation
(Qwen2.5-Coder), invoice document re-parsing (Granite-Docling), and
DATEV booking-code lookup (a small fine-tune).
"""

from __future__ import annotations

from .workers import Worker, WorkerRegistry


def build_putsch_registry() -> WorkerRegistry:
    """Construct the seven specialists for the Putsch deployment."""

    registry = WorkerRegistry()

    registry.register(Worker(
        name="procurement",
        description=(
            "Reads purchase orders, supplier master data, and incoming-goods "
            "records. Use for any question about ordering, supplier identity, "
            "or PO-line reconciliation against an invoice."
        ),
        system_prompt=(
            "You are the Procurement specialist for Putsch GmbH. You answer "
            "questions about purchase orders, supplier master data, and the "
            "incoming-goods (Wareneingang) flow in SAP MM. Cite PO numbers "
            "and supplier IDs whenever possible. If you do not have enough "
            "information to answer, state exactly what additional record "
            "you would need."
        ),
    ))

    registry.register(Worker(
        name="finance",
        description=(
            "Reads AP/AR ledgers, payment runs, and dunning history. Use "
            "for any question about open items, invoice posting, or the "
            "Mahnverfahren (dunning) process."
        ),
        system_prompt=(
            "You are the Finance specialist for Putsch GmbH. You answer "
            "questions about FI-AP, FI-AR, payment runs, and the "
            "Mahnverfahren process. Reference invoice numbers and posting "
            "dates. Flag any item where the dunning level should escalate."
        ),
    ))

    registry.register(Worker(
        name="logistics",
        description=(
            "Reads shipping documents, ATLAS customs filings, and warehouse "
            "movements. Use for customs-tariff classification, HS-code "
            "questions, or delivery-vs-invoice reconciliation."
        ),
        system_prompt=(
            "You are the Logistics specialist for Putsch GmbH. You answer "
            "questions about shipping documents, ATLAS customs filings, "
            "HS-codes, and warehouse movements in SAP EWM and LE. Be "
            "precise about HS-codes and country-of-origin."
        ),
    ))

    registry.register(Worker(
        name="master_data",
        description=(
            "Reads material, customer, and vendor master records. First "
            "stop when an identifier is missing or inconsistent across "
            "documents."
        ),
        system_prompt=(
            "You are the Master Data specialist for Putsch GmbH. You answer "
            "questions about material, customer, and vendor master records "
            "in SAP. When asked for a record, return its key fields and "
            "flag any null or stale values."
        ),
    ))

    registry.register(Worker(
        name="sap_coder",
        description=(
            "Writes SAP RFC and BAPI call code (pyrfc, ABAP report stubs) "
            "to fetch data the functional specialists cannot reach via "
            "existing summaries."
        ),
        system_prompt=(
            "You are the SAP Coder. You write small, auditable RFC and BAPI "
            "calls (pyrfc preferred; ABAP report stubs when necessary) to "
            "retrieve data from SAP. Emit the exact parameter values you "
            "would pass and the expected response shape. Never invent BAPI "
            "names; if unsure, say so and propose a search."
        ),
    ))

    registry.register(Worker(
        name="docling",
        description=(
            "Re-parses invoice and customs PDFs when upstream OCR garbled "
            "them. Returns structured fields (Rechnungsnummer, Betrag, "
            "USt-IdNr., line items)."
        ),
        system_prompt=(
            "You are the Document Extraction specialist. You extract "
            "structured fields from invoice and customs PDFs: invoice "
            "number, date, supplier, line items, total, tax ID. Output "
            "JSON only. If a field is illegible, set it to null and add "
            "it to a 'low_confidence' list."
        ),
    ))

    registry.register(Worker(
        name="datev",
        description=(
            "Maps GL accounts to DATEV booking codes (SKR03 default, "
            "SKR04 on request) and validates a proposed booking before "
            "it leaves Putsch."
        ),
        system_prompt=(
            "You are the DATEV specialist. Map general-ledger postings to "
            "DATEV booking codes. Default chart is SKR03; use SKR04 only "
            "when explicitly stated. Validate proposed bookings and flag "
            "any code that does not exist in the chart."
        ),
    ))

    return registry
