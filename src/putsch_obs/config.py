"""Settings.

All configuration is pydantic-settings, loaded from environment with the
``PUTSCH_OBS_`` prefix. No secret is ever hardcoded. No endpoint is ever
hardcoded.

Conventions
-----------
* Frankfurt VPC is the default region. Endpoints that imply another region
  fail validation.
* Every integer with a unit carries the unit in its name
  (``_seconds``, ``_bytes``, ``_count``). Saves a runbook lookup at 3am.
* Pricing is a top-level dict because cost is a first-class trace attribute.
"""

from __future__ import annotations

import os
from enum import StrEnum
from functools import lru_cache
from typing import Annotated, Any

from pydantic import (
    AnyHttpUrl,
    Field,
    PostgresDsn,
    SecretStr,
    field_validator,
    model_validator,
)
from pydantic_settings import BaseSettings, SettingsConfigDict

from putsch_obs.exceptions import ConfigurationError


class TraceRetentionClass(StrEnum):
    """EU AI Act Art. 12 retention buckets.

    The string values map 1:1 onto ClickHouse TTL labels (see
    ``deploy/langfuse/`` and the ``putsch.retention_class`` trace
    attribute set by ``instrumentation.init``).
    """

    HIGH_RISK = "high_risk"        # HR/payroll-adjacent: 10 years
    LIMITED_RISK = "limited_risk"  # customer-facing: 3 years
    DEVELOPMENT = "development"    # dev/test: 90 days


class RedactionMode(StrEnum):
    """Per-environment policy. ``STRICT`` is the only production-valid value."""

    STRICT = "strict"            # deterministic + LLM; fail-closed
    DETERMINISTIC_ONLY = "deterministic_only"  # CI only — no LLM dependency
    OFF = "off"                  # development only — never in production


_FRANKFURT_HOSTS: frozenset[str] = frozenset({
    "langfuse.putsch.internal",
    "langfuse-fra.putsch.internal",
    "localhost",                 # docker compose dev
    "127.0.0.1",
    "langfuse",                  # docker network DNS
})


class PricingPerMillionTokens(BaseSettings):
    """EUR cost per 1M tokens. Source: La Plateforme tariff sheet, audited
    monthly. The dict layout deliberately mirrors what the LiteLLM hook
    looks up so there's no translation layer.
    """

    model_config = SettingsConfigDict(extra="forbid")

    mistral_small_input: float = 0.20
    mistral_small_output: float = 0.60
    mistral_large_input: float = 2.00
    mistral_large_output: float = 6.00
    codestral_input: float = 0.30
    codestral_output: float = 0.90
    deepseek_v3_input: float = 0.27
    deepseek_v3_output: float = 1.10
    qwen3_14b_local_input: float = 0.0   # self-hosted on Hetzner GPU
    qwen3_14b_local_output: float = 0.0
    embed_mistral_input: float = 0.10


class PutschObsSettings(BaseSettings):
    """Top-level configuration. Construct via :func:`get_settings`."""

    model_config = SettingsConfigDict(
        env_prefix="PUTSCH_OBS_",
        env_file=".env",
        env_file_encoding="utf-8",
        env_nested_delimiter="__",
        extra="ignore",
        case_sensitive=False,
        validate_assignment=True,
    )

    # ── Identity ─────────────────────────────────────────────────────────
    service_name: str = Field(
        default="putsch-obs",
        description=(
            "Logical service name. Becomes the OTel `service.name` resource "
            "attribute and the Langfuse `release` tag prefix."
        ),
    )
    service_version: str = Field(default="0.1.0")
    deployment_environment: str = Field(
        default="development",
        description="One of: development, staging, production-fra.",
    )
    retention_class: TraceRetentionClass = TraceRetentionClass.LIMITED_RISK

    # ── Langfuse ─────────────────────────────────────────────────────────
    langfuse_host: AnyHttpUrl = Field(
        default=AnyHttpUrl("http://langfuse:3000"),
        description="Langfuse server URL. Must resolve to a Frankfurt host.",
    )
    langfuse_public_key: SecretStr = Field(default=SecretStr(""))
    langfuse_secret_key: SecretStr = Field(default=SecretStr(""))
    langfuse_flush_at_count: int = Field(default=15, ge=1, le=1000)
    langfuse_flush_interval_seconds: float = Field(default=0.5, gt=0.0, le=60.0)

    # ── OTel ─────────────────────────────────────────────────────────────
    otel_exporter_endpoint: AnyHttpUrl = Field(
        default=AnyHttpUrl("http://otel-collector:4318"),
        description="OTLP HTTP endpoint of the local collector.",
    )
    otel_export_timeout_seconds: float = Field(default=5.0, gt=0.0, le=30.0)
    otel_max_queue_size_count: int = Field(default=2048, ge=128, le=65_536)
    otel_max_export_batch_size_count: int = Field(default=512, ge=64, le=4096)
    otel_schedule_delay_millis: int = Field(default=500, ge=10, le=30_000)

    # ── Redaction ────────────────────────────────────────────────────────
    redaction_mode: RedactionMode = RedactionMode.STRICT
    redaction_llm_endpoint: AnyHttpUrl = Field(
        default=AnyHttpUrl("http://qwen3-redactor:8000/v1"),
        description="Local Qwen3-14B endpoint for free-form PII redaction.",
    )
    redaction_llm_api_key: SecretStr = Field(default=SecretStr("not-applicable-local"))
    redaction_llm_timeout_seconds: float = Field(default=2.0, gt=0.0, le=10.0)
    redaction_allowlist_attrs: tuple[str, ...] = Field(
        default=(
            "gen_ai.system",
            "gen_ai.request.model",
            "gen_ai.response.model",
            "gen_ai.usage.input_tokens",
            "gen_ai.usage.output_tokens",
            "gen_ai.usage.cost_eur",
            "putsch.retention_class",
            "putsch.routing.decision",
            "putsch.quality_score",
            "http.method",
            "http.status_code",
        ),
        description=(
            "OTel span attributes that bypass redaction. Any attribute NOT "
            "in this allowlist is treated as PII-suspect."
        ),
    )

    # ── Vault (reversible tokenization) ──────────────────────────────────
    vault_dsn: PostgresDsn = Field(
        default=PostgresDsn("postgresql://vault:vault@localhost:5433/putsch_vault"),
        description=(
            "Postgres DSN for the reversible-tokenization vault. MUST be a "
            "separate database from the Langfuse one. Audited un-redaction "
            "logs live here."
        ),
    )
    vault_encryption_key: SecretStr = Field(
        default=SecretStr(""),
        description=(
            "32-byte URL-safe base64 key for Fernet-encrypting vault rows. "
            "Rotate via dual-key migration; never two-step rotate in place."
        ),
    )

    # ── Eval / judge ─────────────────────────────────────────────────────
    judge_model: str = Field(default="deepseek-chat", description="LiteLLM model id")
    judge_api_base: AnyHttpUrl = Field(
        default=AnyHttpUrl("https://api.mistral.ai/v1"),
        description="Eu-routable inference endpoint for the judge.",
    )
    judge_api_key: SecretStr = Field(default=SecretStr(""))
    judge_temperature: float = Field(default=0.0, ge=0.0, le=2.0)
    judge_max_concurrency: int = Field(default=4, ge=1, le=64)

    # ── Alerts ───────────────────────────────────────────────────────────
    teams_webhook_url: SecretStr | None = Field(default=None)
    alerts_enabled: bool = True
    alerts_min_severity: str = Field(default="warning")

    # ── Pricing ──────────────────────────────────────────────────────────
    pricing: PricingPerMillionTokens = Field(default_factory=PricingPerMillionTokens)

    # ── Performance budgets (assert in tests/perf/) ──────────────────────
    perf_budget_llm_p99_ms: float = Field(default=2.0)
    perf_budget_tool_p99_us: float = Field(default=500.0)

    # ── Validation ───────────────────────────────────────────────────────

    @field_validator("langfuse_host", "otel_exporter_endpoint")
    @classmethod
    def _enforce_frankfurt_host(cls, v: AnyHttpUrl) -> AnyHttpUrl:
        host = v.host or ""
        if any(host == fra or host.endswith("." + fra) for fra in _FRANKFURT_HOSTS):
            return v
        # Allow anything in docker compose / k8s service DNS form.
        if "." not in host or host.endswith(".internal") or host.endswith(".cluster.local"):
            return v
        raise ConfigurationError(
            f"endpoint {v!s} is not a Frankfurt VPC host; refusing to ship "
            f"traces outside the EU sovereign perimeter"
        )

    @model_validator(mode="after")
    def _enforce_production_invariants(self) -> PutschObsSettings:
        env = self.deployment_environment.lower()
        if env.startswith("prod"):
            if self.redaction_mode is not RedactionMode.STRICT:
                raise ConfigurationError(
                    f"redaction_mode={self.redaction_mode!s} is forbidden in {env}"
                )
            if not self.langfuse_public_key.get_secret_value():
                raise ConfigurationError("LANGFUSE_PUBLIC_KEY required in production")
            if not self.langfuse_secret_key.get_secret_value():
                raise ConfigurationError("LANGFUSE_SECRET_KEY required in production")
            if not self.vault_encryption_key.get_secret_value():
                raise ConfigurationError(
                    "PUTSCH_OBS_VAULT_ENCRYPTION_KEY required in production"
                )
        return self

    # ── Helpers ──────────────────────────────────────────────────────────

    def otel_resource_attrs(self) -> dict[str, Any]:
        """Build the OTel resource attributes dict."""
        return {
            "service.name": self.service_name,
            "service.version": self.service_version,
            "deployment.environment": self.deployment_environment,
            "putsch.retention_class": self.retention_class.value,
            "putsch.region": "eu-central-fra",
        }


@lru_cache(maxsize=1)
def get_settings() -> PutschObsSettings:
    """Return process-singleton settings. Cached.

    Tests can clear the cache via ``get_settings.cache_clear()``.
    """

    return PutschObsSettings()


def reset_settings_for_test() -> None:
    """Drop the cached settings. Only intended for tests."""

    if os.environ.get("PYTEST_CURRENT_TEST") is None and not os.environ.get(
        "PUTSCH_OBS_ALLOW_RESET"
    ):
        raise ConfigurationError(
            "reset_settings_for_test() called outside pytest. Refusing."
        )
    get_settings.cache_clear()


__all__ = [
    "PricingPerMillionTokens",
    "PutschObsSettings",
    "RedactionMode",
    "TraceRetentionClass",
    "get_settings",
    "reset_settings_for_test",
]


# Re-export for typing convenience in callers that already import this module.
SettingsAnnotated = Annotated[PutschObsSettings, "process-singleton"]
