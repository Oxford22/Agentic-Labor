"""Putsch Group observability + evaluation backbone.

Public surface:

    from putsch_obs import init, shutdown, get_tracer, span, redact

Everything else is internal and may change without notice.
"""

from __future__ import annotations

from putsch_obs.config import PutschObsSettings, get_settings
from putsch_obs.instrumentation import (
    get_tracer,
    init,
    is_initialized,
    shutdown,
    span,
)
from putsch_obs.redaction import RedactionEngine, redact

__all__ = [
    "PutschObsSettings",
    "RedactionEngine",
    "get_settings",
    "get_tracer",
    "init",
    "is_initialized",
    "redact",
    "shutdown",
    "span",
]

__version__ = "0.1.0"
