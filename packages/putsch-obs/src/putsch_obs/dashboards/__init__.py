"""Code-defined Langfuse dashboards.

Each ``*.json`` next to this module is a dashboard spec. ``apply.py`` reads
them and upserts via the Langfuse API. The model is intentionally close to
Langfuse's import/export format so a hand-edited dashboard in the UI can
be ``export``-ed and committed verbatim.
"""

from __future__ import annotations

__all__ = ["apply"]
