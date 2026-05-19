"""Signature registry — the most strategic code in the platform.

Every prompt that runs in production is declared here as a ``dspy.Signature`` subclass with a
``SignatureMeta`` block. The metadata is what makes the artifact reproducible, auditable, and
rollback-safe.

This package is a *registry*: importing it has the side effect of registering every signature into
``SIGNATURE_REGISTRY``. The optimizer, routing, and feedback layers index by name from there.

Changes to this package are treated like database migrations: reviewed by the owning team,
versioned by ``SignatureMeta.version``, and gated by CI eval on the affected dataset.
"""

from __future__ import annotations

from putsch_compile.signatures._base import (
    SIGNATURE_REGISTRY,
    Demo,
    OwnerTeam,
    PutschSignature,
    SignatureMeta,
    register,
)
from putsch_compile.signatures.classify_hs_code import ClassifyHSCode
from putsch_compile.signatures.classify_invoice_exception import ClassifyInvoiceException
from putsch_compile.signatures.draft_customer_email import DraftCustomerEmail
from putsch_compile.signatures.draft_mahnung_letter import DraftMahnungLetter
from putsch_compile.signatures.extract_invoice_fields import ExtractInvoiceFields
from putsch_compile.signatures.generate_datev_booking_code import GenerateDatevBookingCode
from putsch_compile.signatures.reconcile_master_data import ReconcileMasterData
from putsch_compile.signatures.summarize_audit_trail import SummarizeAuditTrail

__all__ = [
    "SIGNATURE_REGISTRY",
    "ClassifyHSCode",
    "ClassifyInvoiceException",
    "Demo",
    "DraftCustomerEmail",
    "DraftMahnungLetter",
    "ExtractInvoiceFields",
    "GenerateDatevBookingCode",
    "OwnerTeam",
    "PutschSignature",
    "ReconcileMasterData",
    "SignatureMeta",
    "SummarizeAuditTrail",
    "register",
]
