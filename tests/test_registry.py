"""Registry contract: record, promote, rollback are atomic and audited."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from putsch_compile.artifacts import CompiledArtifact
from putsch_compile.exceptions import RegistryError
from putsch_compile.registry import Registry


def _make_artifact(*, model: str, accuracy: float, signature: str = "classify_hs_code") -> CompiledArtifact:
    return CompiledArtifact(
        signature_name=signature,
        signature_version="1.0.0",
        signature_version_hash="abc123def4567890",
        model=model,
        compiled_instruction="STUB",
        compiled_demos=(),
        dataset_hash="0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef",
        seed=42,
        holdout_accuracy=accuracy,
        cost_eur_per_call=0.0001,
        created_at=datetime.now(UTC),
    )


@pytest.mark.asyncio
async def test_record_is_idempotent_on_content_hash(registry: Registry) -> None:
    art = _make_artifact(model="qwen/qwen3-14b-instruct", accuracy=0.9)
    a = await registry.record(art, actor="ci@putsch.example")
    b = await registry.record(art, actor="ci@putsch.example")
    assert a.id == b.id, "same content hash → same record id"


@pytest.mark.asyncio
async def test_promote_and_rollback_round_trip(registry: Registry) -> None:
    a = await registry.record(
        _make_artifact(model="qwen/qwen3-14b-instruct", accuracy=0.90),
        actor="ci@putsch.example",
    )
    b = await registry.record(
        _make_artifact(model="mistral/open-mistral-nemo", accuracy=0.93),
        actor="ci@putsch.example",
    )
    entry1 = await registry.promote(a.id, environment="prod", promoted_by="ops@putsch.example")
    assert entry1.artifact_id == a.id
    entry2 = await registry.promote(b.id, environment="prod", promoted_by="ops@putsch.example")
    assert entry2.artifact_id == b.id
    assert entry2.previous_artifact_id == a.id

    rolled = await registry.rollback(
        "classify_hs_code", environment="prod", promoted_by="oncall@putsch.example"
    )
    assert rolled.artifact_id == a.id


@pytest.mark.asyncio
async def test_rollback_with_no_previous_fails(registry: Registry) -> None:
    art = await registry.record(
        _make_artifact(model="qwen/qwen3-14b-instruct", accuracy=0.9),
        actor="ci@putsch.example",
    )
    await registry.promote(art.id, environment="prod", promoted_by="ops@putsch.example")
    with pytest.raises(RegistryError):
        await registry.rollback("classify_hs_code", environment="prod", promoted_by="oncall")


@pytest.mark.asyncio
async def test_get_active_returns_currently_promoted(registry: Registry) -> None:
    art = await registry.record(
        _make_artifact(model="qwen/qwen3-14b-instruct", accuracy=0.91),
        actor="ci@putsch.example",
    )
    with pytest.raises(RegistryError):
        await registry.get_active("classify_hs_code", "prod")
    await registry.promote(art.id, environment="prod", promoted_by="ops@putsch.example")
    active = await registry.get_active("classify_hs_code", "prod")
    assert active.id == art.id


@pytest.mark.asyncio
async def test_unknown_environment_rejected(registry: Registry) -> None:
    art = await registry.record(
        _make_artifact(model="qwen/qwen3-14b-instruct", accuracy=0.9),
        actor="ci@putsch.example",
    )
    with pytest.raises(RegistryError):
        await registry.promote(art.id, environment="not-an-env", promoted_by="ops")


@pytest.mark.asyncio
async def test_history_returns_recent_artifacts(registry: Registry) -> None:
    ids = []
    for acc in (0.85, 0.88, 0.91, 0.93):
        rec = await registry.record(
            _make_artifact(model="qwen/qwen3-14b-instruct", accuracy=acc),
            actor="ci@putsch.example",
        )
        ids.append(rec.id)
    history = await registry.history("classify_hs_code", limit=10)
    assert {r.id for r in history} == set(ids)
