"""Config / settings validation."""

from __future__ import annotations

import pytest

from putsch_obs.config import (
    PutschObsSettings,
    RedactionMode,
    TraceRetentionClass,
)
from putsch_obs.exceptions import ConfigurationError


def test_default_load(isolated_env: None) -> None:
    cfg = PutschObsSettings()
    assert cfg.service_name == "putsch-obs"
    assert cfg.retention_class is TraceRetentionClass.LIMITED_RISK
    assert cfg.redaction_mode is RedactionMode.DETERMINISTIC_ONLY


def test_production_requires_strict_redaction(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("PUTSCH_OBS_DEPLOYMENT_ENVIRONMENT", "production-fra")
    monkeypatch.setenv("PUTSCH_OBS_REDACTION_MODE", "off")
    with pytest.raises(ConfigurationError, match="redaction_mode"):
        PutschObsSettings()


def test_production_requires_langfuse_creds(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("PUTSCH_OBS_DEPLOYMENT_ENVIRONMENT", "production-fra")
    monkeypatch.setenv("PUTSCH_OBS_REDACTION_MODE", "strict")
    monkeypatch.delenv("PUTSCH_OBS_LANGFUSE_PUBLIC_KEY", raising=False)
    with pytest.raises(ConfigurationError):
        PutschObsSettings()


def test_endpoint_outside_frankfurt_is_rejected(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("PUTSCH_OBS_LANGFUSE_HOST", "https://app.langfuse.com")
    with pytest.raises(Exception):
        PutschObsSettings()
