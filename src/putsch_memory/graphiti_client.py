"""Async wrapper around Graphiti's engine.

Responsibilities, in order of importance:

1. **Enforce provenance.** Anonymous writes are rejected at the SDK boundary.
2. **Enforce bounded queries.** Every search has `max_depth` and `max_results`.
3. **Idempotency.** Episode writes carry an `idempotency_key`; duplicates
   are no-ops.
4. **Async.** Graphiti's sync API is wrapped via `asyncio.to_thread`.
5. **Retry.** Transient driver errors retried with tenacity (capped).
6. **Circuit breaker.** After N failures the breaker opens; reads serve
   from the local read-only cache with an explicit `memory_degraded`
   trace attribute; writes raise `MemoryDegraded`.
7. **Correlation.** Every Neo4j transaction carries the Langfuse trace id
   in `dbms.metadata`. Every fact in the graph is traceable back to the
   run that wrote it.

Public surface is small on purpose: `MemoryClient` and `ProvenanceContext`.
The `tools/` and `writers/` modules sit on top.
"""

from __future__ import annotations

import asyncio
import contextlib
import time
from collections.abc import AsyncIterator, Mapping
from contextvars import ContextVar
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Self

import cachetools
import structlog
from tenacity import (
    AsyncRetrying,
    RetryError,
    retry_if_exception_type,
    stop_after_attempt,
    wait_exponential_jitter,
)

from putsch_memory.config import Settings, settings as default_settings
from putsch_memory.exceptions import (
    BoundedQueryExceeded,
    IdempotencyViolation,
    MemoryDegraded,
    MissingProvenance,
)
from putsch_memory.logging import bind_correlation_id, get_logger
from putsch_memory.ontology import (
    BUSINESS_GRAPH,
    Fact,
    Provenance,
    SourceSystem,
    ValidityWindow,
    make_idempotency_key,
)

logger = get_logger(__name__)

# ---------------------------------------------------------------------------
# Provenance context — every write must execute inside one of these
# ---------------------------------------------------------------------------

_current_provenance: ContextVar["ProvenanceContext | None"] = ContextVar(
    "putsch_memory.provenance", default=None
)


@dataclass(slots=True, frozen=True)
class ProvenanceContext:
    """Context manager that binds the provenance scope for a unit of work.

    Use as:

        async with ProvenanceContext(
            source_system=SourceSystem.AGENT_AP,
            written_by_agent="ap_crew/v3",
            trace_id="lf-trace-7c2a...",
        ):
            await client.add_episode(...)

    All writes inside this block carry the bound provenance. Any write
    outside a ProvenanceContext is rejected with `MissingProvenance`.
    """

    source_system: SourceSystem
    written_by_agent: str
    trace_id: str
    default_confidence: float = 1.0

    @contextlib.asynccontextmanager
    async def __call__(self) -> AsyncIterator[Self]:
        token = _current_provenance.set(self)
        bind_correlation_id(self.trace_id)
        try:
            yield self
        finally:
            _current_provenance.reset(token)

    # Allow `async with ProvenanceContext(...)` without calling `()`
    async def __aenter__(self) -> Self:
        self._token = _current_provenance.set(self)  # type: ignore[attr-defined]
        bind_correlation_id(self.trace_id)
        return self

    async def __aexit__(self, *exc: object) -> None:
        token: Any = getattr(self, "_token", None)
        if token is not None:
            _current_provenance.reset(token)


def current_provenance() -> ProvenanceContext:
    p = _current_provenance.get()
    if p is None:
        raise MissingProvenance(
            "No ProvenanceContext bound. Wrap writes in `async with "
            "ProvenanceContext(source_system=..., written_by_agent=..., trace_id=...)`."
        )
    return p


# ---------------------------------------------------------------------------
# Circuit breaker
# ---------------------------------------------------------------------------


class _CircuitBreaker:
    """Three-state breaker: CLOSED → OPEN → HALF-OPEN → CLOSED.

    Reads in OPEN state degrade to cache; writes raise MemoryDegraded.
    A successful probe in HALF-OPEN closes the breaker.
    """

    __slots__ = ("_settings", "_state", "_failures", "_opened_at", "_probes_left", "_lock")

    def __init__(self, settings: Settings) -> None:
        self._settings = settings
        self._state: str = "closed"
        self._failures: int = 0
        self._opened_at: float = 0.0
        self._probes_left: int = 0
        self._lock = asyncio.Lock()

    @property
    def state(self) -> str:
        return self._state

    async def on_success(self) -> None:
        async with self._lock:
            if self._state in ("open", "half-open"):
                logger.info("circuit_breaker_closed", prior_state=self._state)
            self._state = "closed"
            self._failures = 0
            self._probes_left = 0

    async def on_failure(self, exc: BaseException) -> None:
        async with self._lock:
            self._failures += 1
            if self._state == "closed" and self._failures >= self._settings.breaker_failure_threshold:
                self._state = "open"
                self._opened_at = time.monotonic()
                self._probes_left = self._settings.breaker_half_open_probes
                logger.error(
                    "circuit_breaker_open",
                    failures=self._failures,
                    error=type(exc).__name__,
                )

    async def admit(self, *, write: bool) -> None:
        """Raise MemoryDegraded if the breaker is open. Allow probes in half-open."""
        async with self._lock:
            if self._state == "open":
                elapsed = time.monotonic() - self._opened_at
                if elapsed >= self._settings.breaker_recovery_seconds:
                    self._state = "half-open"
                    self._probes_left = self._settings.breaker_half_open_probes
                    logger.warning("circuit_breaker_half_open", elapsed=elapsed)
                else:
                    if write:
                        raise MemoryDegraded("circuit breaker open; writes rejected")
                    raise MemoryDegraded("circuit breaker open; reads must fall back to cache")
            if self._state == "half-open":
                if self._probes_left <= 0:
                    if write:
                        raise MemoryDegraded("circuit breaker half-open; probe budget exhausted")
                    raise MemoryDegraded(
                        "circuit breaker half-open; probe budget exhausted; serve from cache"
                    )
                self._probes_left -= 1


# ---------------------------------------------------------------------------
# Read-only cache for degraded mode
# ---------------------------------------------------------------------------


@dataclass(slots=True, frozen=True)
class CachedResult:
    payload: Any
    fetched_at: datetime
    is_stale: bool


class _ReadCache:
    """Bounded TTL cache used as a fallback when the breaker is open.

    The cache is *only* a fallback. The hot path goes to the graph and
    cache hits do not short-circuit fresh reads — staleness is tolerable
    only when the graph is unreachable, and even then it carries a
    `memory_degraded` trace attribute.
    """

    __slots__ = ("_cache",)

    def __init__(self, settings: Settings) -> None:
        self._cache: cachetools.TTLCache[str, CachedResult] = cachetools.TTLCache(
            maxsize=settings.read_only_cache_max_items,
            ttl=settings.read_only_cache_ttl_seconds,
        )

    def get(self, key: str) -> CachedResult | None:
        return self._cache.get(key)

    def put(self, key: str, payload: Any) -> None:
        self._cache[key] = CachedResult(
            payload=payload,
            fetched_at=datetime.now(tz=timezone.utc),
            is_stale=False,
        )


# ---------------------------------------------------------------------------
# MemoryClient
# ---------------------------------------------------------------------------


_TRANSIENT_ERRORS: tuple[type[BaseException], ...] = ()
"""Filled by `_init_transient_errors()` at first call to avoid importing the
Neo4j driver at module load (so unit tests can run without it)."""


def _init_transient_errors() -> tuple[type[BaseException], ...]:
    global _TRANSIENT_ERRORS
    if _TRANSIENT_ERRORS:
        return _TRANSIENT_ERRORS
    candidates: list[type[BaseException]] = [TimeoutError, ConnectionError, OSError]
    try:
        from neo4j.exceptions import (  # type: ignore[import-not-found]
            ServiceUnavailable,
            TransientError,
        )

        candidates.extend([ServiceUnavailable, TransientError])
    except ImportError:
        pass
    _TRANSIENT_ERRORS = tuple(candidates)
    return _TRANSIENT_ERRORS


class MemoryClient:
    """Wrapped Graphiti client.

    Construct via `MemoryClient.from_env()` for the standard config, or
    pass explicit settings + driver for tests.
    """

    def __init__(
        self,
        *,
        settings: Settings | None = None,
        graphiti_engine: Any | None = None,
        neo4j_driver: Any | None = None,
    ) -> None:
        self._settings = settings or default_settings
        self._engine = graphiti_engine          # graphiti_core.Graphiti
        self._driver = neo4j_driver             # neo4j.AsyncDriver
        self._breaker = _CircuitBreaker(self._settings)
        self._cache = _ReadCache(self._settings)
        self._closed = False
        self.tools = _ToolNamespace(self)
        self.personnel = _PersonnelMemoryAdapter(self)

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    @classmethod
    async def from_env(cls, settings: Settings | None = None) -> Self:
        s = settings or default_settings
        engine = await cls._make_engine(s)
        driver = await cls._make_driver(s)
        return cls(settings=s, graphiti_engine=engine, neo4j_driver=driver)

    @staticmethod
    async def _make_engine(s: Settings) -> Any:
        """Instantiate the Graphiti engine. Kept tiny so it's mockable."""
        # The graphiti_core import is intentionally local so test code
        # that does not have graphiti installed can construct a
        # MemoryClient with `graphiti_engine=Mock()`.
        from graphiti_core import Graphiti  # type: ignore[import-not-found]

        engine = await asyncio.to_thread(
            Graphiti,
            uri=str(s.neo4j_uri),
            user=s.neo4j_user,
            password=s.neo4j_password.get_secret_value(),
        )
        return engine

    @staticmethod
    async def _make_driver(s: Settings) -> Any:
        from neo4j import AsyncGraphDatabase  # type: ignore[import-not-found]

        return AsyncGraphDatabase.driver(
            str(s.neo4j_uri),
            auth=(s.neo4j_user, s.neo4j_password.get_secret_value()),
            connection_timeout=s.graphiti_timeout_seconds,
            max_connection_lifetime=600,
        )

    async def __aenter__(self) -> Self:
        return self

    async def __aexit__(self, *exc: object) -> None:
        await self.close()

    async def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        if self._driver is not None:
            await self._driver.close()
        if self._engine is not None and hasattr(self._engine, "close"):
            close = self._engine.close
            if asyncio.iscoroutinefunction(close):
                await close()
            else:
                await asyncio.to_thread(close)

    # ------------------------------------------------------------------
    # Write path — episodes
    # ------------------------------------------------------------------

    async def add_episode(
        self,
        *,
        name: str,
        body: str,
        episode_type: str,
        reference_time: datetime,
        attributes: Mapping[str, Any] | None = None,
        group_id: str | None = None,
    ) -> str:
        """Append an episode. Returns the idempotency key.

        Re-submitting the same episode is a no-op. Submitting a different
        payload under the same key raises `IdempotencyViolation`.
        """
        prov = current_provenance()
        if reference_time.tzinfo is None:
            raise ValueError("reference_time must be timezone-aware (UTC).")
        if len(body.encode("utf-8")) > self._settings.max_episode_payload_bytes:
            raise ValueError(
                f"episode body exceeds max_episode_payload_bytes "
                f"({self._settings.max_episode_payload_bytes}); split into multiple episodes."
            )

        idem_key = make_idempotency_key(
            source_system=prov.source_system,
            source_id=f"{episode_type}:{name}",
            event_time=reference_time,
        )

        await self._breaker.admit(write=True)
        log = logger.bind(
            op="add_episode",
            episode_type=episode_type,
            name=name,
            idempotency_key=idem_key,
        )

        async def _do() -> None:
            tx_meta = {
                "putsch.trace_id": prov.trace_id,
                "putsch.written_by": prov.written_by_agent,
                "putsch.source_system": prov.source_system.value,
                "putsch.episode_type": episode_type,
                "putsch.idempotency_key": idem_key,
            }
            # If the engine exposes an async add_episode use it directly;
            # otherwise wrap the sync API in a thread.
            add_fn = getattr(self._engine, "add_episode", None)
            if add_fn is None:
                raise RuntimeError("Graphiti engine has no add_episode")
            kwargs = {
                "name": name,
                "episode_body": body,
                "source_description": prov.source_system.value,
                "reference_time": reference_time,
                "group_id": group_id or self._settings.environment,
                "tx_metadata": tx_meta,
            }
            if asyncio.iscoroutinefunction(add_fn):
                await add_fn(**kwargs)
            else:
                await asyncio.to_thread(lambda: add_fn(**kwargs))

        try:
            await self._with_retry(_do)
            await self._breaker.on_success()
            log.info("episode_written")
            return idem_key
        except IdempotencyViolation:
            raise
        except RetryError as exc:
            await self._breaker.on_failure(exc)
            log.error("episode_write_failed", error=str(exc))
            raise MemoryDegraded("episode write failed after retries") from exc

    # ------------------------------------------------------------------
    # Read path — search
    # ------------------------------------------------------------------

    async def search(
        self,
        query: str,
        *,
        as_of: datetime | None = None,
        max_depth: int | None = None,
        max_results: int | None = None,
        labels: tuple[str, ...] | None = None,
        group_id: str | None = None,
    ) -> list[dict[str, Any]]:
        """Hybrid graph + semantic search, temporally filtered.

        Returns at most `max_results` records, capped by `max_query_results`.
        Walking deeper than `max_depth` returns truncated results plus a
        `BoundedQueryExceeded` log warning.
        """
        depth = self._clamp_depth(max_depth)
        results = self._clamp_results(max_results)
        cache_key = f"search|{query}|{as_of}|{depth}|{results}|{labels}|{group_id}"

        try:
            await self._breaker.admit(write=False)
        except MemoryDegraded:
            cached = self._cache.get(cache_key)
            if cached is not None:
                logger.warning("memory_degraded_serving_cache", key=cache_key)
                return list(cached.payload)
            raise

        log = logger.bind(op="search", q_len=len(query), as_of=str(as_of), depth=depth)

        async def _do() -> list[dict[str, Any]]:
            search_fn = getattr(self._engine, "search", None)
            if search_fn is None:
                raise RuntimeError("Graphiti engine has no search")
            kwargs: dict[str, Any] = {
                "query": query,
                "num_results": results,
                "group_ids": [group_id] if group_id else None,
            }
            if as_of is not None:
                kwargs["valid_at"] = as_of
            raw = (
                await search_fn(**kwargs)
                if asyncio.iscoroutinefunction(search_fn)
                else await asyncio.to_thread(lambda: search_fn(**kwargs))
            )
            return [_record_to_dict(r) for r in raw]

        try:
            out = await self._with_retry(_do)
            await self._breaker.on_success()
            if labels:
                out = [r for r in out if r.get("label") in labels]
            self._cache.put(cache_key, out)
            log.info("search_ok", n=len(out))
            return out
        except RetryError as exc:
            await self._breaker.on_failure(exc)
            cached = self._cache.get(cache_key)
            if cached is not None:
                log.warning("search_failed_serving_cache", error=str(exc))
                return list(cached.payload)
            raise MemoryDegraded("search failed and no cached result available") from exc

    # ------------------------------------------------------------------
    # Read path — entity history
    # ------------------------------------------------------------------

    async def get_entity_history(self, entity_id: str) -> list[dict[str, Any]]:
        """Full validity-window reconstruction. Sorted by business_time_from."""
        cypher = """
            MATCH (e {id: $id})
            OPTIONAL MATCH (e)-[r:SUPERSEDED_BY|CORRECTION_OF*0..]->(v)
            WITH coalesce(v, e) AS v
            RETURN DISTINCT v {
                .*,
                labels: labels(v)
            } AS fact
            ORDER BY v.business_time_from ASC
        """
        return await self._run_cypher(cypher, {"id": entity_id})

    # ------------------------------------------------------------------
    # Read path — temporal point-query
    # ------------------------------------------------------------------

    async def as_of(self, entity_id: str, *, business_time: datetime) -> dict[str, Any] | None:
        """The fact that was in force for `entity_id` at `business_time`.

        Uses business_time (not system_time) — i.e. the world state, not
        the system state. For audit replay use `as_of_with_system_time`.
        """
        cypher = """
            MATCH (e {id: $id})
            WHERE e.business_time_from <= datetime($t)
              AND (e.business_time_to IS NULL OR e.business_time_to > datetime($t))
            RETURN e { .*, labels: labels(e) } AS fact
            LIMIT 1
        """
        rows = await self._run_cypher(cypher, {"id": entity_id, "t": business_time.isoformat()})
        return rows[0] if rows else None

    async def as_of_with_system_time(
        self,
        entity_id: str,
        *,
        business_time: datetime,
        system_time: datetime,
    ) -> dict[str, Any] | None:
        """Bitemporal point-query for audit replay.

        Answers: "What did the system *believe* on `system_time` was true
        on `business_time`?" — the EU AI Act Art. 12 question.
        """
        cypher = """
            MATCH (e {id: $id})
            WHERE e.business_time_from <= datetime($bt)
              AND (e.business_time_to IS NULL OR e.business_time_to > datetime($bt))
              AND e.system_time_from <= datetime($st)
              AND (e.system_time_to IS NULL OR e.system_time_to > datetime($st))
            RETURN e { .*, labels: labels(e) } AS fact
            LIMIT 1
        """
        rows = await self._run_cypher(
            cypher,
            {"id": entity_id, "bt": business_time.isoformat(), "st": system_time.isoformat()},
        )
        return rows[0] if rows else None

    # ------------------------------------------------------------------
    # Write path — bulk ingest
    # ------------------------------------------------------------------

    async def bulk_ingest(
        self,
        facts: list[Fact],
        *,
        batch_size: int = 200,
    ) -> int:
        """Batch upsert with provenance + idempotency. Returns count written."""
        prov = current_provenance()
        written = 0
        for i in range(0, len(facts), batch_size):
            batch = facts[i : i + batch_size]
            await self._upsert_batch(batch, prov)
            written += len(batch)
            logger.info("bulk_ingest_batch_ok", batch_size=len(batch), total=written)
        return written

    async def _upsert_batch(self, facts: list[Fact], prov: ProvenanceContext) -> None:
        """Idempotent UPSERT keyed on (label, id). Supersedes prior current fact."""
        await self._breaker.admit(write=True)
        params: list[dict[str, Any]] = []
        for f in facts:
            params.append(
                {
                    "label": f.__class__.__entity_label__,
                    "id": f.id,
                    "props": _fact_to_props(f),
                    "valid": _validity_to_props(f.validity),
                    "prov": _provenance_to_props(f.provenance),
                    "idempotency_key": make_idempotency_key(
                        source_system=prov.source_system,
                        source_id=f.id,
                        event_time=f.validity.system_time_from,
                    ),
                }
            )

        async def _do() -> None:
            cypher = """
                UNWIND $rows AS row
                CALL {
                  WITH row
                  MATCH (existing {id: row.id})
                  WHERE existing.business_time_to IS NULL
                    AND existing.idempotency_key <> row.idempotency_key
                  SET existing.business_time_to = datetime(row.valid.business_time_from),
                      existing.system_time_to   = datetime(row.valid.system_time_from)
                  WITH row, existing
                  CREATE (existing)-[:SUPERSEDED_BY]->(new)
                  RETURN count(*) AS superseded
                }
                MERGE (n {id: row.id, idempotency_key: row.idempotency_key})
                ON CREATE SET n += row.props, n += row.valid, n += row.prov,
                              n:Fact
                WITH n, row
                CALL apoc.create.addLabels(n, [row.label]) YIELD node
                RETURN count(node)
            """
            await self._run_cypher(cypher, {"rows": params}, fetch=False)

        try:
            await self._with_retry(_do)
            await self._breaker.on_success()
        except RetryError as exc:
            await self._breaker.on_failure(exc)
            raise MemoryDegraded("bulk_ingest failed after retries") from exc

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------

    async def _with_retry(self, fn: Any) -> Any:
        transient = _init_transient_errors()
        async for attempt in AsyncRetrying(
            stop=stop_after_attempt(4),
            wait=wait_exponential_jitter(initial=0.25, max=5.0),
            retry=retry_if_exception_type(transient),
            reraise=True,
        ):
            with attempt:
                return await fn()
        raise RuntimeError("unreachable")  # pragma: no cover

    async def _run_cypher(
        self,
        cypher: str,
        params: Mapping[str, Any],
        *,
        fetch: bool = True,
        database: str | None = None,
    ) -> list[dict[str, Any]]:
        await self._breaker.admit(write=False)
        prov = _current_provenance.get()
        meta: dict[str, Any] = {"putsch.region": self._settings.region}
        if prov is not None:
            meta["putsch.trace_id"] = prov.trace_id
            meta["putsch.written_by"] = prov.written_by_agent

        async def _do() -> list[dict[str, Any]]:
            async with self._driver.session(
                database=database or self._settings.neo4j_database,
                impersonated_user=None,
                bookmarks=None,
            ) as session:
                # tx_metadata propagates to dbms.metadata so every fact is auditable.
                async def _work(tx: Any) -> list[dict[str, Any]]:
                    result = await tx.run(cypher, dict(params))
                    if not fetch:
                        await result.consume()
                        return []
                    return [dict(record) async for record in result]

                return await session.execute_read(_work, metadata=meta) if cypher.strip().upper().startswith(
                    "MATCH"
                ) else await session.execute_write(_work, metadata=meta)

        try:
            return await self._with_retry(_do)
        except RetryError as exc:
            await self._breaker.on_failure(exc)
            raise

    def _clamp_depth(self, requested: int | None) -> int:
        max_d = self._settings.max_query_depth
        if requested is None:
            return max_d
        if requested > max_d:
            logger.warning("max_depth_clamped", requested=requested, max=max_d)
            raise BoundedQueryExceeded(f"max_depth={requested} exceeds limit {max_d}")
        return requested

    def _clamp_results(self, requested: int | None) -> int:
        max_r = self._settings.max_query_results
        if requested is None:
            return max_r
        if requested > max_r:
            logger.warning("max_results_clamped", requested=requested, max=max_r)
            return max_r
        return requested


# ---------------------------------------------------------------------------
# Tool namespace — thin facade so `client.tools.lookup_vendor(...)` works
# from agent code without each crew importing every tool module.
# ---------------------------------------------------------------------------


class _ToolNamespace:
    def __init__(self, client: MemoryClient) -> None:
        self._client = client

    async def lookup_vendor(self, **kwargs: Any) -> Any:
        from putsch_memory.tools.lookup_vendor import lookup_vendor

        return await lookup_vendor(self._client, **kwargs)

    async def lookup_customer(self, **kwargs: Any) -> Any:
        from putsch_memory.tools.lookup_customer import lookup_customer

        return await lookup_customer(self._client, **kwargs)

    async def lookup_account_routing(self, **kwargs: Any) -> Any:
        from putsch_memory.tools.lookup_account_routing import lookup_account_routing

        return await lookup_account_routing(self._client, **kwargs)

    async def reconcile_master_data(self, **kwargs: Any) -> Any:
        from putsch_memory.tools.reconcile_master_data import reconcile_master_data

        return await reconcile_master_data(self._client, **kwargs)

    async def temporal_query(self, **kwargs: Any) -> Any:
        from putsch_memory.tools.temporal_query import temporal_query

        return await temporal_query(self._client, **kwargs)


# ---------------------------------------------------------------------------
# Personnel adapter — routes all Mitarbeiter operations to the isolated DB
# ---------------------------------------------------------------------------


class _PersonnelMemoryAdapter:
    """Mirror of the read/write API, but pinned to the personnel database
    and with a mandatory caller-role claim.

    See gdpr.py for the full RBAC + audit story.
    """

    def __init__(self, client: MemoryClient) -> None:
        self._client = client

    async def as_of(
        self,
        personnel_id: str,
        *,
        business_time: datetime,
        caller_role: str,
        caller_id: str,
        purpose: str,
    ) -> dict[str, Any] | None:
        from putsch_memory.gdpr import (
            audit_personnel_read,
            ensure_personnel_role,
        )

        ensure_personnel_role(caller_role, self._client._settings)
        await audit_personnel_read(
            self._client,
            personnel_id=personnel_id,
            caller_id=caller_id,
            caller_role=caller_role,
            purpose=purpose,
            business_time=business_time,
        )
        rows = await self._client._run_cypher(
            """
            MATCH (m:Mitarbeiter {id: $id})
            WHERE m.business_time_from <= datetime($t)
              AND (m.business_time_to IS NULL OR m.business_time_to > datetime($t))
            RETURN m { .*, labels: labels(m) } AS fact
            LIMIT 1
            """,
            {"id": f"mitarbeiter:{personnel_id}", "t": business_time.isoformat()},
            database=self._client._settings.personnel_neo4j_database,
        )
        return rows[0] if rows else None


# ---------------------------------------------------------------------------
# Helpers — serialise Pydantic facts into property maps
# ---------------------------------------------------------------------------


def _fact_to_props(fact: Fact) -> dict[str, Any]:
    data = fact.model_dump(mode="json")
    # validity + provenance are flattened separately so we can index them.
    data.pop("validity", None)
    data.pop("provenance", None)
    data.pop("tags", None)
    return data


def _validity_to_props(v: ValidityWindow) -> dict[str, Any]:
    return {
        "business_time_from": v.business_time_from.isoformat(),
        "business_time_to": v.business_time_to.isoformat() if v.business_time_to else None,
        "system_time_from": v.system_time_from.isoformat(),
        "system_time_to": v.system_time_to.isoformat() if v.system_time_to else None,
        "superseded_by": v.superseded_by,
    }


def _provenance_to_props(p: Provenance) -> dict[str, Any]:
    return {
        "source_system": p.source_system.value,
        "source_id": p.source_id,
        "written_by_agent": p.written_by_agent,
        "written_at_trace_id": p.written_at_trace_id,
        "confidence": p.confidence,
        "justification": p.justification,
    }


def _record_to_dict(r: Any) -> dict[str, Any]:
    if isinstance(r, dict):
        return r
    if hasattr(r, "data"):
        return dict(r.data())
    if hasattr(r, "_asdict"):
        return dict(r._asdict())
    return {"value": r}


# ---------------------------------------------------------------------------
# Re-exports
# ---------------------------------------------------------------------------

# Re-export for ergonomics; agents typically import from the package root.
_ = BUSINESS_GRAPH  # silence unused-import linter — used by migrations.py

__all__ = [
    "MemoryClient",
    "ProvenanceContext",
    "current_provenance",
]
