"""Exception hierarchy for putsch_memory.

The contract:
* Everything raised by this package is a subclass of `PutschMemoryError`.
* Errors that callers MAY want to handle (degraded mode, conflicts) are
  distinct types so they can be caught surgically.
* Errors that indicate a programming mistake (missing provenance, schema
  violation) are intentionally NOT caught anywhere inside this package —
  they propagate to the agent runtime and fail loudly.

Never widen this hierarchy with "ConvenienceError" or wrap third-party
exceptions just to rebrand them. Wrap only when the wrapping adds
information the caller needs to recover.
"""

from __future__ import annotations


class PutschMemoryError(Exception):
    """Base for every exception raised by putsch_memory."""


class MissingProvenance(PutschMemoryError):
    """Raised when an attempt is made to write a fact without provenance.

    Provenance is mandatory at the SDK boundary; this never escapes the
    write path. If you see this in production, find the caller and fix it
    — do not catch it.
    """


class TemporalIntegrityError(PutschMemoryError):
    """Raised when a write would violate validity-window invariants.

    Examples:
    * Two facts with the same (entity_id, predicate) and overlapping
      validity windows that are not in a supersede relationship.
    * `valid_to` precedes `valid_from`.
    * `system_time_from` precedes `business_time_from` for a backdated
      correction without an explicit `as_correction_of` reference.
    """


class ConflictDetected(PutschMemoryError):
    """Raised when two sources of truth disagree on a fact.

    Both facts are stored; this exception is informational and surfaces
    the disagreement to the Stammdaten crew for human reconciliation.
    """

    def __init__(self, entity_id: str, predicate: str, sources: list[str], detail: str) -> None:
        self.entity_id = entity_id
        self.predicate = predicate
        self.sources = sources
        super().__init__(
            f"Conflict on {entity_id}.{predicate} across {sources}: {detail}"
        )


class MemoryDegraded(PutschMemoryError):
    """Raised by the read path when the circuit breaker is open.

    Agent code is expected to catch this and decide:
    * fall back to the cached recent context, with a `memory_degraded`
      trace attribute, or
    * abort the task if reading fresh memory is required.

    Never catch this and silently proceed — the trace attribute is what
    keeps the audit story honest.
    """


class BoundedQueryExceeded(PutschMemoryError):
    """Raised when a query would exceed `max_depth` or `max_results`.

    The query is truncated; this is *also* logged as a warning. Catching
    this means you intentionally want the partial result; otherwise let
    it propagate.
    """


class IdempotencyViolation(PutschMemoryError):
    """Raised when a writer detects an idempotency-key collision with a
    *different* payload. Identical replays are no-ops, not errors.
    """
