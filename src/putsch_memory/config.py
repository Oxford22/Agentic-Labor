"""Runtime configuration.

All knobs that can drift between environments live here. Defaults are
chosen for the Frankfurt production VPC; the dev defaults override them
via environment variables, never via code edits.
"""

from __future__ import annotations

from functools import lru_cache
from typing import Literal

from pydantic import AnyUrl, Field, SecretStr, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

Environment = Literal["prod", "stage", "dev", "test"]


class Settings(BaseSettings):
    """putsch_memory runtime settings.

    Read from env vars prefixed `PUTSCH_MEMORY_`. Loaded once at process
    start; in tests, override by passing kwargs to `Settings(...)` directly.
    """

    model_config = SettingsConfigDict(
        env_prefix="PUTSCH_MEMORY_",
        env_file=".env",
        env_file_encoding="utf-8",
        extra="forbid",
        frozen=True,
    )

    environment: Environment = Field(default="dev")
    region: str = Field(default="eu-central-frankfurt", description="Hard-coded; do not change without ADR.")
    data_residency: str = Field(default="DE")

    # --- Graphiti / Neo4j wiring ---------------------------------------
    neo4j_uri: AnyUrl = Field(default=AnyUrl("bolt://localhost:7687"))
    neo4j_user: str = Field(default="neo4j")
    neo4j_password: SecretStr = Field(default=SecretStr("changeme"))
    neo4j_database: str = Field(default="neo4j")
    personnel_neo4j_database: str = Field(
        default="personnel",
        description="Separate database name for Mitarbeiter facts. See gdpr.py.",
    )

    graphiti_base_url: AnyUrl = Field(default=AnyUrl("http://localhost:8765"))
    graphiti_timeout_seconds: float = Field(default=15.0, ge=0.5, le=120.0)

    # --- LLM (Mistral La Plateforme only) ------------------------------
    mistral_api_key: SecretStr | None = Field(default=None)
    mistral_base_url: AnyUrl = Field(default=AnyUrl("https://api.mistral.ai/v1"))
    mistral_model: str = Field(default="mistral-large-latest")
    mistral_embedding_model: str = Field(default="mistral-embed")

    # --- Bounded query enforcement -------------------------------------
    max_query_depth: int = Field(default=4, ge=1, le=10)
    max_query_results: int = Field(default=50, ge=1, le=500)
    max_episode_payload_bytes: int = Field(default=128 * 1024)

    # --- Circuit breaker ----------------------------------------------
    breaker_failure_threshold: int = Field(default=5, ge=1, le=100)
    breaker_recovery_seconds: float = Field(default=30.0, ge=1.0, le=600.0)
    breaker_half_open_probes: int = Field(default=2, ge=1, le=10)

    # --- Cache --------------------------------------------------------
    read_only_cache_ttl_seconds: int = Field(default=300, ge=10, le=3600)
    read_only_cache_max_items: int = Field(default=10_000, ge=100, le=1_000_000)

    # --- Confidence ---------------------------------------------------
    low_confidence_threshold: float = Field(default=0.7, ge=0.0, le=1.0)

    # --- Observability -----------------------------------------------
    langfuse_public_key: SecretStr | None = Field(default=None)
    langfuse_secret_key: SecretStr | None = Field(default=None)
    langfuse_host: AnyUrl | None = Field(default=None)

    log_level: Literal["DEBUG", "INFO", "WARNING", "ERROR"] = Field(default="INFO")
    log_format: Literal["json", "console"] = Field(default="json")

    # --- GDPR ---------------------------------------------------------
    personnel_namespace_required_role: str = Field(
        default="role:personnel-reader",
        description="Caller must claim this role to read Mitarbeiter facts.",
    )
    rtbf_audit_retention_days: int = Field(default=365 * 10, ge=365)

    # ------------------------------------------------------------------
    @field_validator("region", mode="after")
    @classmethod
    def _enforce_region(cls, v: str) -> str:
        if not v.startswith("eu-central"):
            raise ValueError(
                f"region {v!r} is outside the EU-Central data residency boundary; "
                "change requires ADR amendment and DPIA review."
            )
        return v

    @field_validator("data_residency", mode="after")
    @classmethod
    def _enforce_residency(cls, v: str) -> str:
        if v != "DE":
            raise ValueError(
                f"data_residency must be 'DE' for putsch-memory; got {v!r}. "
                "Cross-border replication is out of scope."
            )
        return v

    @property
    def is_prod(self) -> bool:
        return self.environment == "prod"


@lru_cache(maxsize=1)
def _cached_settings() -> Settings:
    return Settings()


settings: Settings = _cached_settings()
"""Module-level Settings instance for convenience. In tests, prefer
constructing `Settings(...)` explicitly so each test gets a fresh copy."""
