"""Ingestion pipelines for memory.

Each pipeline owns its own source-of-truth integration:

* `sap_master_data` — nightly diff from SAP MM/SD master tables
* `datev_period_close` — monthly digest of DATEV bookings
* `email_ingestion` — customer/vendor email threads via Docling + PII
* `manual_correction` — Sachbearbeiter UI corrections with justification

Pipelines never bypass the writer layer; everything ultimately calls
`MemoryClient.bulk_ingest` or a writer, so every persisted fact carries
provenance.
"""

from putsch_memory.ingestion.datev_period_close import ingest_datev_period_close
from putsch_memory.ingestion.email_ingestion import ingest_email_thread
from putsch_memory.ingestion.manual_correction import apply_manual_correction
from putsch_memory.ingestion.sap_master_data import ingest_sap_master_data

__all__ = [
    "apply_manual_correction",
    "ingest_datev_period_close",
    "ingest_email_thread",
    "ingest_sap_master_data",
]
