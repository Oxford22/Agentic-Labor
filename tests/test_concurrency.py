"""Concurrency tests.

Fifty agents writing episodes simultaneously must produce a consistent
graph: every episode visible, no duplicates beyond idempotency, every
fact carrying provenance.

This runs against the FakeGraph fixture; the integration test in
`tests/test_concurrency_integration.py` (not bundled) runs against
real Neo4j.
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone

import pytest

from putsch_memory.graphiti_client import MemoryClient, ProvenanceContext
from putsch_memory.ontology import SourceSystem


async def test_50_agents_write_concurrently(
    memory_client: MemoryClient, fake_graph
) -> None:
    base = datetime(2026, 5, 18, 10, 0, 0, tzinfo=timezone.utc)

    async def _one(i: int) -> None:
        async with ProvenanceContext(
            source_system=SourceSystem.AGENT_AP,
            written_by_agent=f"ap_crew/{i}",
            trace_id=f"lf-trace-{i:03d}",
        ):
            await memory_client.add_episode(
                name=f"ap_completion-{i}",
                body=f'{{"i": {i}}}',
                episode_type="ap_completion",
                reference_time=base + timedelta(seconds=i),
            )

    await asyncio.gather(*[_one(i) for i in range(50)])
    assert len(fake_graph.episodes) == 50


async def test_idempotent_replay_is_noop(
    memory_client: MemoryClient, fake_graph
) -> None:
    """Replaying the same episode N times produces a stable set of writes
    against the fake graph. The deduplication check happens server-side
    in real Graphiti; here we verify that our SDK consistently produces
    the same idempotency key for the same input."""
    when = datetime(2026, 5, 18, 10, 0, 0, tzinfo=timezone.utc)

    async with ProvenanceContext(
        source_system=SourceSystem.AGENT_AP,
        written_by_agent="ap_crew/v1",
        trace_id="lf-trace-001",
    ):
        keys = set()
        for _ in range(10):
            k = await memory_client.add_episode(
                name="ap_completion-replay",
                body='{"x": 1}',
                episode_type="ap_completion",
                reference_time=when,
            )
            keys.add(k)
    assert len(keys) == 1


async def test_provenance_context_isolation_across_tasks(
    memory_client: MemoryClient, fake_graph
) -> None:
    """Two concurrent tasks must not leak each other's provenance."""

    seen: list[str] = []

    async def _writer(trace_id: str) -> None:
        async with ProvenanceContext(
            source_system=SourceSystem.AGENT_AP,
            written_by_agent="ap_crew/v1",
            trace_id=trace_id,
        ):
            await memory_client.add_episode(
                name=f"e-{trace_id}",
                body="{}",
                episode_type="test",
                reference_time=datetime.now(tz=timezone.utc),
            )
            seen.append(trace_id)

    await asyncio.gather(_writer("trace-A"), _writer("trace-B"))
    trace_ids = {ep["tx_metadata"]["putsch.trace_id"] for ep in fake_graph.episodes}
    assert trace_ids == {"trace-A", "trace-B"}
