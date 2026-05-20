"""Configuration via pydantic-settings. 12-factor; env-driven; no hard-coded secrets.

All Settings instances are immutable; mutate via env, not at runtime.
Frankfurt-only endpoints by default — EU data residency is a precondition,
not a runtime choice.
"""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path
from typing import Literal

from pydantic import Field, HttpUrl, SecretStr, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

DeviceLiteral = Literal["cuda", "cpu", "auto", "mps"]


class DoclingSettings(BaseSettings):
    """Granite-Docling 258M + DocumentConverter knobs."""

    model_config = SettingsConfigDict(env_prefix="PUTSCH_DOCS_DOCLING_", extra="ignore")

    model_id: str = Field(
        default="ibm-granite/granite-docling-258M",
        description="HuggingFace model id. MIT-licensed Granite-Docling.",
    )
    device: DeviceLiteral = Field(default="auto")
    num_threads: int = Field(default=4, ge=1, le=64)
    do_ocr: bool = Field(
        default=True,
        description="Run OCR step. Granite VLM sidesteps it on clean PDFs; keep on for scans.",
    )
    do_table_structure: bool = Field(
        default=True,
        description="TableFormer enabled. Required for line items on multi-page invoices.",
    )
    max_pages: int = Field(default=200, ge=1, le=2000)
    page_image_resolution_dpi: int = Field(default=144, ge=72, le=600)
    artifacts_path: Path | None = Field(
        default=None,
        description="Local model cache. Required for air-gapped Frankfurt deploy.",
    )
    timeout_seconds: float = Field(default=60.0, gt=0)


class FallbackSettings(BaseSettings):
    """Qwen2.5-VL-72B via vLLM. Self-hosted in Frankfurt, no external traffic."""

    model_config = SettingsConfigDict(env_prefix="PUTSCH_DOCS_FALLBACK_", extra="ignore")

    enabled: bool = Field(default=True)
    model_id: str = Field(default="Qwen/Qwen2.5-VL-72B-Instruct")
    endpoint: HttpUrl = Field(default="http://vllm.frankfurt.putsch.internal:8000/v1")
    api_key: SecretStr = Field(
        default=SecretStr("vllm-not-required"),
        description="vLLM accepts any value; kept for LiteLLM contract compatibility.",
    )
    timeout_seconds: float = Field(default=120.0, gt=0)
    max_retries: int = Field(default=3, ge=0, le=10)
    breaker_fail_max: int = Field(default=5, ge=1, le=100)
    breaker_reset_seconds: int = Field(default=60, ge=1, le=3600)
    # 72B params at 144 DPI page rasters needs guardrails
    max_pages_per_request: int = Field(default=4, ge=1, le=20)
    image_jpeg_quality: int = Field(default=85, ge=50, le=100)


class ConfidenceSettings(BaseSettings):
    """Confidence thresholds — the most strategic knobs in this module.

    Conservative defaults: a value below threshold triggers either fallback
    or an unrecoverable ConfidenceError. We do not silently downgrade.
    """

    model_config = SettingsConfigDict(env_prefix="PUTSCH_DOCS_CONFIDENCE_", extra="ignore")

    # Per-field minimum confidence below which we re-extract via VLM fallback
    fallback_threshold: float = Field(default=0.85, ge=0.0, le=1.0)

    # Critical fields fail-hard if both Docling and fallback are below this
    critical_field_threshold: float = Field(default=0.90, ge=0.0, le=1.0)

    # Arithmetic tolerance: netto + mwst == brutto within €0.02
    arithmetic_tolerance_cents: int = Field(default=2, ge=0, le=100)

    # Fields treated as critical for fallback / error escalation
    critical_fields: frozenset[str] = Field(
        default=frozenset(
            {
                "rechnungsnummer",
                "rechnungsdatum",
                "lieferant_ustid",
                "kunde_ustid",
                "iban",
                "netto_betrag",
                "mwst_betrag",
                "brutto_betrag",
            }
        )
    )

    # If true, run LLM-as-judge on critical fields even when Docling is confident.
    # Cost trade-off: higher accuracy at small per-invoice token cost. Default on.
    judge_critical_fields_always: bool = Field(default=True)


class ExtractionLLMSettings(BaseSettings):
    """LLM used to coerce DoclingDocument markdown into typed InvoiceFields.

    Routed through LiteLLM so model swap is a config change.
    Default: Mistral Large via Mistral La Plateforme (Paris, EU).
    """

    model_config = SettingsConfigDict(env_prefix="PUTSCH_DOCS_LLM_", extra="ignore")

    model: str = Field(default="mistral/mistral-large-latest")
    api_base: HttpUrl = Field(default="https://api.mistral.ai/v1")
    api_key: SecretStr = Field(default=SecretStr(""))
    temperature: float = Field(default=0.0, ge=0.0, le=2.0)
    max_tokens: int = Field(default=4096, ge=128, le=32768)
    timeout_seconds: float = Field(default=30.0, gt=0)
    # Judge model — separate so we can use a cheaper model for verification
    judge_model: str = Field(default="mistral/mistral-medium-latest")


class ObservabilitySettings(BaseSettings):
    """Langfuse self-hosted + structlog JSON. PII redacted at the boundary."""

    model_config = SettingsConfigDict(env_prefix="PUTSCH_DOCS_OBS_", extra="ignore")

    langfuse_enabled: bool = Field(default=True)
    langfuse_public_key: SecretStr = Field(default=SecretStr(""))
    langfuse_secret_key: SecretStr = Field(default=SecretStr(""))
    langfuse_host: HttpUrl = Field(default="https://langfuse.frankfurt.putsch.internal")
    redact_pii_in_logs: bool = Field(default=True)
    log_level: Literal["DEBUG", "INFO", "WARNING", "ERROR"] = Field(default="INFO")
    service_name: str = Field(default="putsch-docs")
    environment: Literal["dev", "staging", "prod"] = Field(default="dev")


class Settings(BaseSettings):
    """Root settings — composed sub-models. Cached singleton via get_settings()."""

    model_config = SettingsConfigDict(
        env_prefix="PUTSCH_DOCS_",
        env_nested_delimiter="__",
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    docling: DoclingSettings = Field(default_factory=DoclingSettings)
    fallback: FallbackSettings = Field(default_factory=FallbackSettings)
    confidence: ConfidenceSettings = Field(default_factory=ConfidenceSettings)
    llm: ExtractionLLMSettings = Field(default_factory=ExtractionLLMSettings)
    obs: ObservabilitySettings = Field(default_factory=ObservabilitySettings)

    @field_validator("confidence")
    @classmethod
    def _critical_threshold_above_fallback(cls, v: ConfidenceSettings) -> ConfidenceSettings:
        if v.critical_field_threshold < v.fallback_threshold:
            msg = (
                f"critical_field_threshold ({v.critical_field_threshold}) must be >= "
                f"fallback_threshold ({v.fallback_threshold}); "
                "fallback should fire before we hard-fail."
            )
            raise ValueError(msg)
        return v


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Return the process-singleton Settings. Cached for the lifetime of the process."""
    return Settings()
