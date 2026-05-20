"""Shared pytest fixtures.

We deliberately do NOT spin up Neo4j here — the unit tests use the
in-memory fake `FakeGraph` defined below. The integration tests live
behind `@pytest.mark.integration` and pull a real Neo4j via docker
compose when run with `-m integration`.
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from typing import Any
from unittest.mock import MagicMock

import pytest
import pytest_asyncio

from putsch_memory.config import Settings
from putsch_memory.graphiti_client import MemoryClient


@pytest.fixture
def settings() -> Settings:
    # Note on bounds: `breaker_recovery_seconds` is constrained `ge=1.0`
    # in the Settings model (see putsch_memory/config.py). Use the smallest
    # legal value so unit tests still exercise the recovery path quickly
    # without violating the production-safe schema bound.
    return Settings(
        environment="test",
        neo4j_uri="bolt://localhost:7687",
        neo4j_user="neo4j",
        neo4j_password="test",  # type: ignore[arg-type]
        mistral_api_key="test",  # type: ignore[arg-type]
        max_query_depth=4,
        max_query_results=50,
        breaker_failure_threshold=3,
        breaker_recovery_seconds=1.0,
        breaker_half_open_probes=1,
        log_format="console",
    )


class FakeGraph:
    """In-memory graph that mimics the slice of Cypher we use.

    Not a real graph DB. Just enough to test the SDK contracts without
    standing up Neo4j in unit tests. Integration tests run against the
    real thing.
    """

    def __init__(self) -> None:
        self.nodes: dict[str, dict[str, Any]] = {}
        self.episodes: list[dict[str, Any]] = []
        self.cypher_log: list[tuple[str, dict[str, Any]]] = []
        self.lock = asyncio.Lock()
        self.fail_next_n: int = 0

    async def run(self, cypher: str, params: dict[str, Any]) -> list[dict[str, Any]]:
        self.cypher_log.append((cypher, params))
        if self.fail_next_n > 0:
            self.fail_next_n -= 1
            raise ConnectionError("fake driver: simulated transient failure")
        return []

    async def add_episode(self, **kwargs: Any) -> None:
        self.episodes.append(kwargs)

    async def search(self, **kwargs: Any) -> list[dict[str, Any]]:
        return []


@pytest.fixture
def fake_graph() -> FakeGraph:
    return FakeGraph()


@pytest_asyncio.fixture
async def memory_client(settings: Settings, fake_graph: FakeGraph) -> MemoryClient:
    fake_driver = MagicMock()

    # Build a session/run shim that records the cypher calls and
    # routes them to fake_graph.run().
    class _Result:
        def __init__(self, rows: list[dict[str, Any]]) -> None:
            self._rows = rows

        def __aiter__(self) -> "_Result":
            self._iter = iter(self._rows)
            return self

        async def __anext__(self) -> dict[str, Any]:
            try:
                return next(self._iter)
            except StopIteration as e:
                raise StopAsyncIteration from e

        async def consume(self) -> None:
            return None

    class _Tx:
        async def run(self, cypher: str, params: dict[str, Any]) -> _Result:
            rows = await fake_graph.run(cypher, params)
            return _Result(rows)

    class _Session:
        async def __aenter__(self) -> "_Session":
            return self

        async def __aexit__(self, *a: Any) -> None:
            return None

        async def execute_read(self, work: Any, **kwargs: Any) -> list[dict[str, Any]]:
            return await work(_Tx())

        async def execute_write(self, work: Any, **kwargs: Any) -> list[dict[str, Any]]:
            return await work(_Tx())

    def _session(*args: Any, **kwargs: Any) -> _Session:
        return _Session()

    fake_driver.session = _session
    fake_driver.close = MagicMock(return_value=asyncio.sleep(0))

    client = MemoryClient(settings=settings, graphiti_engine=fake_graph, neo4j_driver=fake_driver)
    try:
        yield client
    finally:
        await client.close()


@pytest.fixture
def utc_now_frozen() -> datetime:
    return datetime(2026, 5, 18, 10, 0, 0, tzinfo=timezone.utc)


@pytest.fixture
def correlation_id() -> str:
    return "lf-trace-test-0001"
"""Shared pytest fixtures.

We deliberately do NOT spin up Neo4j here — the unit tests use the
in-memory fake `FakeGraph` defined below. The integration tests live
behind `@pytest.mark.integration` and pull a real Neo4j via docker
compose when run with `-m integration`.
"""

from __future__ import annotations

import asyncio
from collections import defaultdict
from datetime import datetime, timezone
from typing import Any
from unittest.mock import MagicMock

import pytest

from putsch_memory.config import Settings
from putsch_memory.graphiti_client import MemoryClient


@pytest.fixture
def settings() -> Settings:
    return Settings(
        environment="test",
        neo4j_uri="bolt://localhost:7687",
        neo4j_user="neo4j",
        neo4j_password="test",  # type: ignore[arg-type]
        mistral_api_key="test",  # type: ignore[arg-type]
        max_query_depth=4,
        max_query_results=50,
        breaker_failure_threshold=3,
        breaker_recovery_seconds=0.5,
        breaker_half_open_probes=1,
        log_format="console",
    )


class FakeGraph:
    """In-memory graph that mimics the slice of Cypher we use.

    Not a real graph DB. Just enough to test the SDK contracts without
    standing up Neo4j in unit tests. Integration tests run against the
    real thing.
    """

    def __init__(self) -> None:
        self.nodes: dict[str, dict[str, Any]] = {}
        self.episodes: list[dict[str, Any]] = []
        self.cypher_log: list[tuple[str, dict[str, Any]]] = []
        self.lock = asyncio.Lock()
        self.fail_next_n: int = 0

    async def run(self, cypher: str, params: dict[str, Any]) -> list[dict[str, Any]]:
        self.cypher_log.append((cypher, params))
        if self.fail_next_n > 0:
            self.fail_next_n -= 1
            raise ConnectionError("fake driver: simulated transient failure")
        return []

    async def add_episode(self, **kwargs: Any) -> None:
        self.episodes.append(kwargs)

    async def search(self, **kwargs: Any) -> list[dict[str, Any]]:
        return []


@pytest.fixture
def fake_graph() -> FakeGraph:
    return FakeGraph()


@pytest.fixture
async def memory_client(settings: Settings, fake_graph: FakeGraph) -> MemoryClient:
    fake_driver = MagicMock()

    # Build a session/run shim that records the cypher calls and
    # routes them to fake_graph.run().
    class _Result:
        def __init__(self, rows: list[dict[str, Any]]) -> None:
            self._rows = rows

        def __aiter__(self) -> "_Result":
            self._iter = iter(self._rows)
            return self

        async def __anext__(self) -> dict[str, Any]:
            try:
                return next(self._iter)
            except StopIteration as e:
                raise StopAsyncIteration from e

        async def consume(self) -> None:
            return None

    class _Tx:
        async def run(self, cypher: str, params: dict[str, Any]) -> _Result:
            rows = await fake_graph.run(cypher, params)
            return _Result(rows)

    class _Session:
        async def __aenter__(self) -> "_Session":
            return self

        async def __aexit__(self, *a: Any) -> None:
            return None

        async def execute_read(self, work: Any, **kwargs: Any) -> list[dict[str, Any]]:
            return await work(_Tx())

        async def execute_write(self, work: Any, **kwargs: Any) -> list[dict[str, Any]]:
            return await work(_Tx())

    def _session(*args: Any, **kwargs: Any) -> _Session:
        return _Session()

    fake_driver.session = _session
    fake_driver.close = MagicMock(return_value=asyncio.sleep(0))

    client = MemoryClient(settings=settings, graphiti_engine=fake_graph, neo4j_driver=fake_driver)
    try:
        yield client
    finally:
        await client.close()


@pytest.fixture
def utc_now_frozen() -> datetime:
    return datetime(2026, 5, 18, 10, 0, 0, tzinfo=timezone.utc)


@pytest.fixture
def correlation_id() -> str:
    return "lf-trace-test-0001"
