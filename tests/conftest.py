"""Pytest fixtures.

Three goals:

* Tests never reach an external network. ``configure_dspy`` is replaced with a no-op so signature
  tests can run without an LLM. The optimizer integration test uses a deterministic dummy module.

* Registry tests run against in-process SQLite (aiosqlite) for speed. The ArtifactStore is replaced
  with an in-memory dict; we test the registry/store contract, not S3 itself.

* Settings always point at temp dirs. ``get_settings`` is reset between tests.
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Iterator
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import pytest
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from putsch_compile import config as cfg_mod
from putsch_compile.artifacts import CompiledArtifact
from putsch_compile.registry import Registry, _Base
from putsch_compile.routing import MODEL_CATALOG, Router


@pytest.fixture(autouse=True)
def _reset_settings(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Iterator[None]:
    """Each test gets a clean Settings cache pointed at a temp repo root."""

    monkeypatch.setenv("PUTSCH_COMPILE_LANGFUSE_ENABLED", "false")
    monkeypatch.setenv("PUTSCH_COMPILE_LITELLM_PROXY_BASE_URL", "http://localhost:0")
    cfg_mod.get_settings.cache_clear()
    yield
    cfg_mod.get_settings.cache_clear()


@pytest.fixture
def patched_artifact_store(monkeypatch: pytest.MonkeyPatch) -> dict[str, CompiledArtifact]:
    """Replace MinIO with a dict-backed store for the duration of a test."""

    store: dict[str, CompiledArtifact] = {}

    class _Fake:
        async def put(self, artifact: CompiledArtifact) -> str:
            h = artifact.content_hash()
            store[h] = artifact
            return h

        async def get(self, content_hash: str) -> CompiledArtifact:
            return store[content_hash]

        async def exists(self, content_hash: str) -> bool:
            return content_hash in store

    monkeypatch.setattr("putsch_compile.artifacts.ArtifactStore", _Fake)
    monkeypatch.setattr("putsch_compile.registry.ArtifactStore", _Fake)
    return store


@pytest.fixture
async def registry(patched_artifact_store: dict[str, CompiledArtifact]) -> AsyncIterator[Registry]:
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.run_sync(_Base.metadata.create_all)
    sessionmaker = async_sessionmaker(engine, expire_on_commit=False)
    reg = Registry(sessionmaker=sessionmaker)
    yield reg
    await engine.dispose()


@pytest.fixture
def router() -> Router:
    return Router(catalog=MODEL_CATALOG)


@pytest.fixture
def patched_dspy(monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    """Replace ``configure_dspy``, ``dspy.ChainOfThought``, ``GEPA``, and ``get_metric`` with
    deterministic stubs.

    The replacement metric scores 1.0 unconditionally so the harness's accept-the-cheapest-tier
    behaviour is what we exercise — not the metric. Metric correctness is tested separately.
    """

    from putsch_compile.metrics import MetricResult

    calls: dict[str, Any] = {"configure": [], "compiled_modules": []}

    def fake_configure(*, model: str, api_base: str | None = None, **_: Any) -> None:
        calls["configure"].append({"model": model, "api_base": api_base})

    class _FakeCompiled:
        def __init__(self, signature: Any) -> None:
            self.signature = signature
            self.predict = MagicMock()
            self.predict.signature = MagicMock(instructions="STUB-INSTRUCTION-v1")
            self.predict.demos = []

        def __call__(self, **_inputs: Any) -> Any:
            return MagicMock()

    def fake_chain_of_thought(signature: Any) -> _FakeCompiled:
        return _FakeCompiled(signature)

    class _FakeGEPA:
        def __init__(self, *, metric: Any, **_: Any) -> None:
            self.metric = metric

        def compile(self, student: _FakeCompiled, *, trainset: list[Any]) -> _FakeCompiled:
            calls["compiled_modules"].append(student)
            return student

    def fake_get_metric(_name: str) -> Any:
        return lambda _ex, _pr: MetricResult(score=1.0, breakdown={"stub": 1.0})

    monkeypatch.setattr("putsch_compile.optimize.configure_dspy", fake_configure)
    monkeypatch.setattr("putsch_compile.adapters.configure_dspy", fake_configure)
    monkeypatch.setattr("putsch_compile.optimize._gepa_cls", lambda: _FakeGEPA)
    monkeypatch.setattr("putsch_compile.optimize.dspy.ChainOfThought", fake_chain_of_thought)
    monkeypatch.setattr("putsch_compile.optimize.get_metric", fake_get_metric)
    return calls
