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


def test_empty_string_url_env_falls_back_to_default(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """CI scenario: `LANGFUSE_HOST: ${{ secrets.LANGFUSE_HOST }}` produces an
    empty string when the secret is unset. Settings must fall back to the
    declared default instead of failing AnyHttpUrl validation."""
    monkeypatch.setenv("PUTSCH_OBS_LANGFUSE_HOST", "")
    monkeypatch.setenv("PUTSCH_OBS_OTEL_EXPORTER_ENDPOINT", "   ")
    cfg = PutschObsSettings()
    assert cfg.langfuse_host is not None
    assert "langfuse" in str(cfg.langfuse_host)
    assert "otel" in str(cfg.otel_exporter_endpoint) or "4318" in str(
        cfg.otel_exporter_endpoint
    )
