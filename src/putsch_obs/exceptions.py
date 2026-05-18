"""Exception hierarchy.

Two rules govern this module:

1. **Instrumentation failures never propagate** to the application. The SDK
   catches every internal error and logs at ``WARN``. The exceptions defined
   here exist for test assertions and for explicit operator-tooling paths
   (e.g. the dashboards CLI, the DSGVO generator) where the caller *wants*
   to see the failure.
2. **Redaction failures are fail-closed**. ``RedactionError`` is the one
   exception we will let bubble up rather than risk leaking PII into a
   trace export. Callers in the instrumentation hot path translate it into
   a dropped span, not a failed application call.
"""

from __future__ import annotations


class PutschObsError(Exception):
    """Base exception. All other exceptions in this package inherit from this."""


class ConfigurationError(PutschObsError):
    """Raised when settings or environment are inconsistent at startup."""


class RedactionError(PutschObsError):
    """Raised when a redactor cannot guarantee PII has been removed.

    This is fail-closed: the caller MUST drop the payload rather than export
    it. The instrumentation hot path translates this to a dropped-span
    metric increment.
    """


class VaultError(PutschObsError):
    """Raised by the reversible-tokenization vault when integrity is at risk.

    Includes hash-chain breaks, missing token mappings, and unauthorized
    un-redaction attempts.
    """


class DatasetError(PutschObsError):
    """Raised by the eval harness when a dataset is malformed or out of sync."""


class JudgeError(PutschObsError):
    """Raised by the LLM-as-judge harness when scoring cannot complete."""


class DashboardApplyError(PutschObsError):
    """Raised by the dashboard apply tool. Operator-visible by design."""
