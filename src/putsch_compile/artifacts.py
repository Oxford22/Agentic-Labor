"""MinIO/S3-compatible artifact blob store for compiled prompts.

A *compiled artifact* is the JSON the optimizer emits when GEPA succeeds: instruction string,
optimized demos, parameters, plus the metadata necessary to load it back into a DSPy program
without re-compilation.

We store artifacts in MinIO (Frankfurt) keyed by ``content_hash``, which is the SHA-256 of the
canonical serialization. The Postgres registry then references artifacts by hash; two compilation
runs that produce the same artifact share storage. This is the trick that makes nightly
recompilation cheap.

We do not store secrets in artifacts. Any field that smells like a key is rejected at the schema
level (see ``CompiledArtifact._validate_no_secrets``).
"""

from __future__ import annotations

import asyncio
import hashlib
import re
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from typing import Any, Final

import orjson
from pydantic import BaseModel, ConfigDict, Field, field_validator

from putsch_compile.config import get_settings
from putsch_compile.exceptions import RegistryError
from putsch_compile.logging import get_logger

_log = get_logger(__name__)


_SECRET_RE: Final[re.Pattern[str]] = re.compile(
    r"(?i)(api[_-]?key|secret|token|password|bearer\s+[A-Za-z0-9._-]+)"
)


class OptimizedDemo(BaseModel):
    """A demonstration as written by GEPA — same shape as the source ``Demo`` minus provenance."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    inputs: dict[str, Any]
    outputs: dict[str, Any]


class CompiledArtifact(BaseModel):
    """The compiled-prompt payload itself. JSON-serialisable, deterministically hashed."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    signature_name: str = Field(..., min_length=1, max_length=64)
    signature_version: str = Field(..., pattern=r"^\d+\.\d+\.\d+$")
    signature_version_hash: str = Field(..., pattern=r"^[a-f0-9]{16}$")
    model: str
    compiled_instruction: str = Field(..., min_length=1)
    compiled_demos: tuple[OptimizedDemo, ...] = ()
    optimizer: str = Field(default="GEPA")
    optimizer_config: dict[str, Any] = Field(default_factory=dict)
    dataset_hash: str = Field(..., pattern=r"^[a-f0-9]{16,64}$")
    seed: int
    holdout_accuracy: float = Field(..., ge=0.0, le=1.0)
    cost_eur_per_call: float = Field(..., ge=0.0)
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    metadata: dict[str, Any] = Field(default_factory=dict)

    @field_validator("compiled_instruction", "compiled_demos", "metadata")
    @classmethod
    def _no_secrets(cls, value: Any) -> Any:
        serialized = orjson.dumps(value).decode("utf-8")
        if _SECRET_RE.search(serialized):
            raise ValueError("artifact rejected: appears to contain a secret")
        return value

    def canonical_bytes(self) -> bytes:
        """Stable, sorted-key byte representation used for hashing.

        ``created_at`` is excluded — same compilation inputs should produce the same hash even if
        compiled minutes apart.
        """

        payload = self.model_dump(mode="json", exclude={"created_at"})
        return orjson.dumps(payload, option=orjson.OPT_SORT_KEYS)

    def content_hash(self) -> str:
        return hashlib.sha256(self.canonical_bytes()).hexdigest()


class ArtifactStore:
    """Async wrapper around MinIO via aioboto3. One bucket, content-addressed keys."""

    def __init__(self) -> None:
        self._settings = get_settings().artifacts

    @asynccontextmanager
    async def _session(self) -> AsyncIterator[Any]:
        # Imported lazily so tests that do not exercise S3 don't pay the import cost.
        import aioboto3

        session = aioboto3.Session()
        async with session.client(
            "s3",
            endpoint_url=self._settings.endpoint_url,
            aws_access_key_id=self._settings.access_key.get_secret_value(),
            aws_secret_access_key=self._settings.secret_key.get_secret_value(),
            region_name=self._settings.region,
        ) as client:
            yield client

    def _key_for(self, content_hash: str) -> str:
        # Two-level fanout so the bucket listing stays sane.
        return f"{content_hash[:2]}/{content_hash[2:4]}/{content_hash}.json"

    async def put(self, artifact: CompiledArtifact) -> str:
        """Store the artifact; return the content hash (= S3 object key suffix)."""

        body = artifact.canonical_bytes()
        content_hash = hashlib.sha256(body).hexdigest()
        key = self._key_for(content_hash)
        async with self._session() as client:
            try:
                await client.put_object(
                    Bucket=self._settings.bucket,
                    Key=key,
                    Body=body,
                    ContentType="application/json",
                    Metadata={
                        "signature": artifact.signature_name,
                        "model": artifact.model.replace("/", "__"),
                        "version-hash": artifact.signature_version_hash,
                    },
                )
            except Exception as exc:  # pragma: no cover - network
                raise RegistryError(
                    "MinIO put_object failed",
                    context={"key": key, "error": str(exc)},
                ) from exc
        _log.info("artifact.persisted", key=key, signature=artifact.signature_name)
        return content_hash

    async def get(self, content_hash: str) -> CompiledArtifact:
        key = self._key_for(content_hash)
        async with self._session() as client:
            try:
                resp = await client.get_object(Bucket=self._settings.bucket, Key=key)
                body = await resp["Body"].read()
            except Exception as exc:
                raise RegistryError(
                    "MinIO get_object failed",
                    context={"key": key, "error": str(exc)},
                ) from exc
        return CompiledArtifact.model_validate_json(body)

    async def exists(self, content_hash: str) -> bool:
        key = self._key_for(content_hash)
        async with self._session() as client:
            try:
                await client.head_object(Bucket=self._settings.bucket, Key=key)
                return True
            except Exception:
                return False


def hash_dataset(rows: list[dict[str, Any]]) -> str:
    """Canonical SHA-256 hex over the sorted JSONL representation of the dataset.

    Used as part of the artifact's reproducibility metadata. Same rows in the same order → same
    hash, on every Python version.
    """

    canonical = b"\n".join(orjson.dumps(row, option=orjson.OPT_SORT_KEYS) for row in rows)
    return hashlib.sha256(canonical).hexdigest()


def run_async(coro: Any) -> Any:
    """Tiny helper so sync call sites (CLI, tests) don't carry asyncio boilerplate."""

    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        return asyncio.run(coro)
    return loop.run_until_complete(coro)
