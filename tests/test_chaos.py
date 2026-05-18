"""Chaos test — Neo4j unavailable → memory client degrades gracefully.

In production, "Neo4j is unavailable" is the cliff edge: agents that
trust memory must continue with explicit "memory_degraded" so the
audit trail captures the moment.

The fake fixture exposes `fail_next_n` to simulate driver outages.
"""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from putsch_memory.exceptions import MemoryDegraded
from putsch_memory.graphiti_client import MemoryClient, ProvenanceContext
from putsch_memory.ontology import SourceSystem


pytestmark = pytest.mark.chaos


async def test_search_degrades_to_cache_then_raises_after_breaker_open(
    memory_client: MemoryClient, fake_graph
) -> None:
    # 1. Warm the cache while the graph is up.
    fake_graph.fail_next_n = 0
    _ = await memory_client.search("warm-up")

    # 2. Trip the breaker: enough consecutive failures.
    fake_graph.fail_next_n = 1000
    threshold = memory_client._settings.breaker_failure_threshold  # noqa: SLF001
    for _ in range(threshold + 1):
        try:
            await memory_client.search("warm-up")
        except MemoryDegraded:
            pass
        except Exception:
            pass

    # 3. With breaker open, a search with NO cached entry must raise MemoryDegraded.
    with pytest.raises(MemoryDegraded):
        await memory_client.search("never-seen-this-query")


async def test_write_in_degraded_mode_raises(
    memory_client: MemoryClient, fake_graph
) -> None:
    fake_graph.fail_next_n = 1000
    threshold = memory_client._settings.breaker_failure_threshold  # noqa: SLF001
    for _ in range(threshold + 1):
        try:
            await memory_client.search("trigger")
        except Exception:
            pass
    async with ProvenanceContext(
        source_system=SourceSystem.AGENT_AP,
        written_by_agent="ap_crew/v1",
        trace_id="lf-trace-001",
    ):
        with pytest.raises(MemoryDegraded):
            await memory_client.add_episode(
                name="x",
                body="{}",
                episode_type="test",
                reference_time=datetime(2026, 5, 18, tzinfo=timezone.utc),
            )
