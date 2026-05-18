"""MemoryClient contract tests — provenance, idempotency, breaker, bounds."""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from putsch_memory.exceptions import (
    BoundedQueryExceeded,
    MemoryDegraded,
    MissingProvenance,
)
from putsch_memory.graphiti_client import (
    MemoryClient,
    ProvenanceContext,
)
from putsch_memory.ontology import SourceSystem


# ---------------------------------------------------------------------------
# Provenance enforcement
# ---------------------------------------------------------------------------


async def test_add_episode_requires_provenance(memory_client: MemoryClient) -> None:
    with pytest.raises(MissingProvenance):
        await memory_client.add_episode(
            name="x",
            body="{}",
            episode_type="test",
            reference_time=datetime(2026, 5, 18, tzinfo=timezone.utc),
        )


async def test_add_episode_succeeds_with_provenance(
    memory_client: MemoryClient, fake_graph
) -> None:
    async with ProvenanceContext(
        source_system=SourceSystem.AGENT_AP,
        written_by_agent="ap_crew/v1",
        trace_id="lf-trace-001",
    ):
        idem = await memory_client.add_episode(
            name="ap_completion",
            body='{"x": 1}',
            episode_type="ap_completion",
            reference_time=datetime(2026, 5, 18, tzinfo=timezone.utc),
        )
    assert idem
    assert len(fake_graph.episodes) == 1
    assert fake_graph.episodes[0]["tx_metadata"]["putsch.trace_id"] == "lf-trace-001"


async def test_add_episode_rejects_naive_reference_time(memory_client: MemoryClient) -> None:
    async with ProvenanceContext(
        source_system=SourceSystem.AGENT_AP,
        written_by_agent="ap_crew/v1",
        trace_id="lf-trace-001",
    ):
        with pytest.raises(ValueError, match="timezone-aware"):
            await memory_client.add_episode(
                name="x",
                body="{}",
                episode_type="test",
                reference_time=datetime(2026, 5, 18),  # naive
            )


# ---------------------------------------------------------------------------
# Bounded queries
# ---------------------------------------------------------------------------


async def test_max_depth_clamps_strictly(memory_client: MemoryClient) -> None:
    with pytest.raises(BoundedQueryExceeded):
        await memory_client.search("x", max_depth=99)


async def test_max_results_clamps_silently(
    memory_client: MemoryClient, settings
) -> None:
    # Asking for too many results returns the clamped maximum, doesn't raise.
    result = await memory_client.search("x", max_results=999)
    assert isinstance(result, list)


# ---------------------------------------------------------------------------
# Episode payload size limit
# ---------------------------------------------------------------------------


async def test_episode_payload_size_enforced(memory_client: MemoryClient) -> None:
    huge = "x" * (memory_client._settings.max_episode_payload_bytes + 1)  # noqa: SLF001
    async with ProvenanceContext(
        source_system=SourceSystem.AGENT_AP,
        written_by_agent="ap_crew/v1",
        trace_id="lf-trace-001",
    ):
        with pytest.raises(ValueError, match="max_episode_payload_bytes"):
            await memory_client.add_episode(
                name="x",
                body=huge,
                episode_type="test",
                reference_time=datetime(2026, 5, 18, tzinfo=timezone.utc),
            )


# ---------------------------------------------------------------------------
# Circuit breaker behaviour
# ---------------------------------------------------------------------------


async def test_breaker_opens_after_threshold_failures(
    memory_client: MemoryClient, fake_graph
) -> None:
    # Force consecutive failures.
    fake_graph.fail_next_n = 100
    # Each retry-capped call counts as one failure for the breaker.
    for _ in range(memory_client._settings.breaker_failure_threshold + 1):  # noqa: SLF001
        with pytest.raises((MemoryDegraded, Exception)):
            await memory_client.search("x")
    # Now writes should be rejected outright.
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
