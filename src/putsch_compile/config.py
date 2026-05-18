"""12-factor settings. Every secret comes from env or vault, never source.

Reads ``PUTSCH_COMPILE_*`` env vars. Cached singleton via ``get_settings()``.
"""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path
from typing import Literal

from pydantic import Field, SecretStr, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

EnvName = Literal["dev", "staging", "prod"]


class LiteLLMSettings(BaseSettings):
    """LiteLLM proxy + provider keys. La Plateforme is the only external API in prod."""

    model_config = SettingsConfigDict(
        env_prefix="PUTSCH_COMPILE_LITELLM_",
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    proxy_base_url: str = Field(
        default="https://litellm-proxy.frankfurt.putsch.internal/v1",
        description="LiteLLM proxy URL — all model calls go through here for audit + budget control.",
    )
    proxy_api_key: SecretStr = Field(
        default=SecretStr(""),
        description="Proxy-issued key. Per-environment, rotated quarterly.",
    )
    mistral_api_key: SecretStr = Field(
        default=SecretStr(""),
        description="Used only if proxy is bypassed (local dev). Prod always uses the proxy.",
    )
    request_timeout_s: float = Field(default=60.0, ge=1.0, le=600.0)
    max_retries: int = Field(default=2, ge=0, le=8)


class LangfuseSettings(BaseSettings):
    """Self-hosted Langfuse in Frankfurt."""

    model_config = SettingsConfigDict(
        env_prefix="PUTSCH_COMPILE_LANGFUSE_",
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    host: str = Field(default="https://langfuse.frankfurt.putsch.internal")
    public_key: SecretStr = Field(default=SecretStr(""))
    secret_key: SecretStr = Field(default=SecretStr(""))
    project_id: str = Field(default="putsch-platform-prod")
    flush_at: int = Field(default=20, ge=1, le=200)
    enabled: bool = True


class ArtifactStoreSettings(BaseSettings):
    """MinIO blob store for compiled artifacts. S3-compatible."""

    model_config = SettingsConfigDict(
        env_prefix="PUTSCH_COMPILE_MINIO_",
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    endpoint_url: str = Field(default="https://minio.frankfurt.putsch.internal")
    access_key: SecretStr = Field(default=SecretStr(""))
    secret_key: SecretStr = Field(default=SecretStr(""))
    bucket: str = Field(default="putsch-compiled-artifacts")
    region: str = Field(default="eu-central-1")


class RegistryDBSettings(BaseSettings):
    """Postgres for the (signature, model, version) → artifact registry."""

    model_config = SettingsConfigDict(
        env_prefix="PUTSCH_COMPILE_DB_",
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    dsn: SecretStr = Field(
        default=SecretStr("postgresql+asyncpg://putsch:putsch@localhost:5432/putsch_compile"),
        description="Async SQLAlchemy DSN.",
    )
    pool_size: int = Field(default=5, ge=1, le=50)
    pool_max_overflow: int = Field(default=10, ge=0, le=100)
    statement_timeout_ms: int = Field(default=30_000, ge=100)


class CompilationSettings(BaseSettings):
    """Compilation knobs. Determinism settings live here."""

    model_config = SettingsConfigDict(
        env_prefix="PUTSCH_COMPILE_OPTIMIZER_",
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    seed: int = Field(
        default=42,
        description="Bumping this is a breaking change — invalidates all golden artifacts.",
    )
    holdout_fraction: float = Field(default=0.2, gt=0.0, lt=0.5)
    regression_tolerance: float = Field(
        default=0.02,
        ge=0.0,
        le=0.5,
        description="A new artifact >2% worse than the active one on holdout halts the pipeline.",
    )
    num_threads: int = Field(default=8, ge=1, le=64)
    max_compilation_seconds: int = Field(default=900, ge=30, le=7200)
    cheapest_model_first: bool = True


class GitFeedbackSettings(BaseSettings):
    """Service-account git config for the annotation → dataset commit loop."""

    model_config = SettingsConfigDict(
        env_prefix="PUTSCH_COMPILE_GIT_",
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    service_account_email: str = "agentic-platform-bot@putsch.example"
    service_account_name: str = "agentic-platform-bot"
    repo_url: str = "ssh://git@gitlab.frankfurt.putsch.internal/agentic/agentic-platform.git"
    branch_prefix: str = "auto/feedback/"
    push_token: SecretStr = Field(default=SecretStr(""))


class Settings(BaseSettings):
    """Top-level. Everything is a sub-settings model."""

    model_config = SettingsConfigDict(
        env_prefix="PUTSCH_COMPILE_",
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    env: EnvName = Field(default="dev")
    repo_root: Path = Field(
        default=Path(__file__).resolve().parents[2],
        description="Resolved at import time; overridable for tests via env.",
    )
    dataset_dir: Path = Field(default=Path("evals/datasets"))

    litellm: LiteLLMSettings = Field(default_factory=LiteLLMSettings)
    langfuse: LangfuseSettings = Field(default_factory=LangfuseSettings)
    artifacts: ArtifactStoreSettings = Field(default_factory=ArtifactStoreSettings)
    registry_db: RegistryDBSettings = Field(default_factory=RegistryDBSettings)
    compilation: CompilationSettings = Field(default_factory=CompilationSettings)
    git_feedback: GitFeedbackSettings = Field(default_factory=GitFeedbackSettings)

    @field_validator("dataset_dir")
    @classmethod
    def _normalize_dataset_dir(cls, value: Path) -> Path:
        return value if value.is_absolute() else value

    @property
    def absolute_dataset_dir(self) -> Path:
        return (
            self.dataset_dir
            if self.dataset_dir.is_absolute()
            else (self.repo_root / self.dataset_dir).resolve()
        )


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Cached singleton. Tests can clear via ``get_settings.cache_clear()``."""

    return Settings()
