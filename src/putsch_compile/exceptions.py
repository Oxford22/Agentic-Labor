"""Typed exception hierarchy. Every error is one of these — no bare ``Exception`` from this package."""

from __future__ import annotations

from typing import Any


class CompilationError(Exception):
    """Base class for every error raised by ``putsch_compile``.

    Carries a stable ``code`` so log handlers can route without parsing messages, and a structured
    ``context`` dict so the Langfuse trace and structlog record can show what was happening when
    things broke. Avoid logging secrets here — the trace exporter does not redact ``context``.
    """

    code: str = "compile.error"

    def __init__(self, message: str, *, context: dict[str, Any] | None = None) -> None:
        super().__init__(message)
        self.context: dict[str, Any] = dict(context or {})

    def __str__(self) -> str:  # pragma: no cover - trivial
        if self.context:
            kv = ", ".join(f"{k}={v!r}" for k, v in self.context.items())
            return f"{super().__str__()} ({kv})"
        return super().__str__()


class DatasetError(CompilationError):
    """Dataset shape, schema, or provenance failure. Examples:

    * Missing ``labeled_by`` (anonymous label).
    * Pydantic validation failure on a row.
    * Empty dataset.
    * Drift detected vs the snapshot in the active artifact metadata.
    """

    code = "compile.dataset"


class OptimizerError(CompilationError):
    """GEPA itself failed: no candidate met threshold, optimizer crashed, budget exhausted."""

    code = "compile.optimizer"


class AdapterError(CompilationError):
    """BAML/structured-output adapter parse failure. The model returned something we cannot bind."""

    code = "compile.adapter"


class RegistryError(CompilationError):
    """Postgres / MinIO registry failure: missing artifact, invalid promotion, rollback to unknown."""

    code = "compile.registry"


class RoutingError(CompilationError):
    """No tier resolves for a signature, or LiteLLM has no available model in the configured ladder."""

    code = "compile.routing"


class RegressionError(CompilationError):
    """A new compiled artifact would regress holdout accuracy beyond tolerance.

    This is a *halt* signal, not an error to swallow — the previous artifact stays active and the
    on-call playbook in ``docs/runbook.md`` kicks in.
    """

    code = "compile.regression"
