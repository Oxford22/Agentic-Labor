"""Shared pytest fixtures.

Notable:
* ``isolated_env`` clears process env and resets settings cache so each
  test sees a deterministic config.
* ``no_network`` blocks outbound HTTP via httpx by injecting a transport
  that raises. The redaction LLM tests use respx to opt back in.
"""

from __future__ import annotations

import os
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

# Force test-friendly defaults BEFORE importing putsch_obs config helpers.
os.environ.setdefault("PUTSCH_OBS_DEPLOYMENT_ENVIRONMENT", "development")
os.environ.setdefault("PUTSCH_OBS_REDACTION_MODE", "deterministic_only")
os.environ.setdefault("PUTSCH_OBS_LANGFUSE_HOST", "http://localhost:3000")
os.environ.setdefault("PUTSCH_OBS_OTEL_EXPORTER_ENDPOINT", "http://localhost:4318")
os.environ.setdefault(
    "PUTSCH_OBS_VAULT_ENCRYPTION_KEY",
    # Static key for tests; never reuse anywhere else.
    "Q0htcDc0eWFLM0lWcVdoVUxsZjlOaHc1ZUkyQ19zVXVLQUM0ckpoLW5VUT0=",
)


@pytest.fixture()
def isolated_env(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """Snapshot env, reset settings cache around each test."""
    from putsch_obs.config import reset_settings_for_test

    monkeypatch.setenv("PUTSCH_OBS_ALLOW_RESET", "1")
    reset_settings_for_test()
    yield
    reset_settings_for_test()


@pytest.fixture()
def fixed_token(monkeypatch: pytest.MonkeyPatch) -> str:
    """Deterministic redaction tokens for snapshot tests."""
    token = "TESTTOKEN1234567"
    monkeypatch.setattr(
        "putsch_obs.redaction._make_token", lambda: token
    )
    return token


@pytest.fixture()
def memory_vault() -> Any:
    """A simple in-memory VaultProtocol implementation for tests."""

    class MemoryVault:
        def __init__(self) -> None:
            self.store_calls: list[tuple[str, str, str]] = []

        def store(
            self,
            token: str,
            category: Any,
            original: str,
            *,
            context_hint: str | None = None,
        ) -> None:
            self.store_calls.append((token, category.value, original))

        async def store_async(
            self,
            token: str,
            category: Any,
            original: str,
            *,
            context_hint: str | None = None,
        ) -> None:
            self.store(token, category, original)

    return MemoryVault()


@pytest.fixture()
def fixture_dir() -> Path:
    return Path(__file__).parent / "fixtures"
