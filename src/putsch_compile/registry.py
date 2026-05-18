"""Postgres registry of compiled artifacts. The lookup path for production.

The registry has two tables:

* ``compiled_artifacts`` — one row per produced compilation. Append-only. Holds the immutable
  metadata + the MinIO content hash. Rolling back means flipping which row is *active*, never
  deleting.

* ``registry_entries`` — for each (signature_name, environment), one row that points to the
  currently active artifact. The unique constraint guarantees a single active artifact per
  signature per environment. Promotions are transactional; rollback is an UPDATE of one row.

Every promotion logs an audit entry with the actor and the prior artifact, so the trail back to
"why did production change last Tuesday?" is one query.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from typing import Final

from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import (
    DateTime,
    Float,
    ForeignKey,
    Index,
    String,
    UniqueConstraint,
    select,
)
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship

from putsch_compile.artifacts import ArtifactStore, CompiledArtifact
from putsch_compile.config import get_settings
from putsch_compile.exceptions import RegistryError
from putsch_compile.logging import get_logger

_log = get_logger(__name__)


class _Base(DeclarativeBase):
    pass


class CompiledArtifactRow(_Base):
    """Immutable row per compilation run. The MinIO blob is the body."""

    __tablename__ = "compiled_artifacts"

    id: Mapped[str] = mapped_column(String(40), primary_key=True)  # ULID
    signature_name: Mapped[str] = mapped_column(String(64), index=True)
    signature_version: Mapped[str] = mapped_column(String(20))
    signature_version_hash: Mapped[str] = mapped_column(String(16), index=True)
    model: Mapped[str] = mapped_column(String(120))
    content_hash: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    dataset_hash: Mapped[str] = mapped_column(String(64))
    seed: Mapped[int] = mapped_column()
    holdout_accuracy: Mapped[float] = mapped_column(Float)
    cost_eur_per_call: Mapped[float] = mapped_column(Float)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    optimizer: Mapped[str] = mapped_column(String(32), default="GEPA")
    actor: Mapped[str] = mapped_column(String(120))

    __table_args__ = (
        Index("ix_artifacts_signature_version_hash", "signature_name", "signature_version_hash"),
    )


class RegistryEntryRow(_Base):
    """Pointer to the currently active artifact per (signature, environment)."""

    __tablename__ = "registry_entries"

    id: Mapped[str] = mapped_column(String(40), primary_key=True)
    signature_name: Mapped[str] = mapped_column(String(64))
    environment: Mapped[str] = mapped_column(String(16))
    artifact_id: Mapped[str] = mapped_column(ForeignKey("compiled_artifacts.id"))
    promoted_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    promoted_by: Mapped[str] = mapped_column(String(120))
    previous_artifact_id: Mapped[str | None] = mapped_column(
        ForeignKey("compiled_artifacts.id"), nullable=True
    )

    artifact: Mapped[CompiledArtifactRow] = relationship(foreign_keys=[artifact_id])

    __table_args__ = (
        UniqueConstraint("signature_name", "environment", name="uq_active_per_signature_env"),
    )


# -----------------------------------------------------------------------------
# Public records (Pydantic projection — what the rest of the system sees)
# -----------------------------------------------------------------------------


class CompiledArtifactRecord(BaseModel):
    """Read-only projection of ``CompiledArtifactRow`` for use outside the DAL."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    id: str
    signature_name: str
    signature_version: str
    signature_version_hash: str
    model: str
    content_hash: str
    dataset_hash: str
    seed: int
    holdout_accuracy: float
    cost_eur_per_call: float
    created_at: datetime
    optimizer: str = "GEPA"
    actor: str

    @classmethod
    def from_row(cls, row: CompiledArtifactRow) -> "CompiledArtifactRecord":
        return cls(
            id=row.id,
            signature_name=row.signature_name,
            signature_version=row.signature_version,
            signature_version_hash=row.signature_version_hash,
            model=row.model,
            content_hash=row.content_hash,
            dataset_hash=row.dataset_hash,
            seed=row.seed,
            holdout_accuracy=row.holdout_accuracy,
            cost_eur_per_call=row.cost_eur_per_call,
            created_at=row.created_at,
            optimizer=row.optimizer,
            actor=row.actor,
        )


class RegistryEntryRecord(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    id: str
    signature_name: str
    environment: str
    artifact_id: str
    promoted_at: datetime
    promoted_by: str
    previous_artifact_id: str | None = None


VALID_ENVS: Final[frozenset[str]] = frozenset({"dev", "staging", "prod"})


def _new_id() -> str:
    from ulid import ULID

    return str(ULID())


class Registry:
    """Async DAL. One instance per process. Pass a SQLAlchemy session factory in tests."""

    def __init__(self, sessionmaker: async_sessionmaker[AsyncSession] | None = None) -> None:
        if sessionmaker is None:
            settings = get_settings().registry_db
            engine = create_async_engine(
                settings.dsn.get_secret_value(),
                pool_size=settings.pool_size,
                max_overflow=settings.pool_max_overflow,
                pool_pre_ping=True,
            )
            sessionmaker = async_sessionmaker(engine, expire_on_commit=False)
        self._sessionmaker = sessionmaker
        self._artifacts = ArtifactStore()

    @asynccontextmanager
    async def session(self) -> AsyncIterator[AsyncSession]:
        async with self._sessionmaker() as sess:
            yield sess

    async def record(
        self,
        artifact: CompiledArtifact,
        *,
        actor: str,
    ) -> CompiledArtifactRecord:
        """Persist artifact JSON to MinIO + a row to Postgres. Idempotent on (content_hash)."""

        content_hash = await self._artifacts.put(artifact)
        async with self.session() as sess:
            existing = (
                await sess.execute(
                    select(CompiledArtifactRow).where(
                        CompiledArtifactRow.content_hash == content_hash
                    )
                )
            ).scalar_one_or_none()
            if existing is not None:
                _log.info(
                    "registry.dedup",
                    signature=artifact.signature_name,
                    content_hash=content_hash,
                    existing_id=existing.id,
                )
                return CompiledArtifactRecord.from_row(existing)

            row = CompiledArtifactRow(
                id=_new_id(),
                signature_name=artifact.signature_name,
                signature_version=artifact.signature_version,
                signature_version_hash=artifact.signature_version_hash,
                model=artifact.model,
                content_hash=content_hash,
                dataset_hash=artifact.dataset_hash,
                seed=artifact.seed,
                holdout_accuracy=artifact.holdout_accuracy,
                cost_eur_per_call=artifact.cost_eur_per_call,
                created_at=artifact.created_at,
                optimizer=artifact.optimizer,
                actor=actor,
            )
            sess.add(row)
            await sess.commit()
            return CompiledArtifactRecord.from_row(row)

    async def get(self, artifact_id: str) -> CompiledArtifactRecord:
        async with self.session() as sess:
            row = await sess.get(CompiledArtifactRow, artifact_id)
            if row is None:
                raise RegistryError(
                    f"artifact {artifact_id!r} not found",
                    context={"artifact_id": artifact_id},
                )
            return CompiledArtifactRecord.from_row(row)

    async def load_payload(self, artifact_id: str) -> CompiledArtifact:
        """Fetch the actual compiled-prompt JSON from MinIO."""

        record = await self.get(artifact_id)
        return await self._artifacts.get(record.content_hash)

    async def get_active(
        self, signature_name: str, environment: str = "prod"
    ) -> CompiledArtifactRecord:
        if environment not in VALID_ENVS:
            raise RegistryError(
                f"unknown environment {environment!r}",
                context={"valid": sorted(VALID_ENVS)},
            )
        async with self.session() as sess:
            entry = (
                await sess.execute(
                    select(RegistryEntryRow).where(
                        RegistryEntryRow.signature_name == signature_name,
                        RegistryEntryRow.environment == environment,
                    )
                )
            ).scalar_one_or_none()
            if entry is None:
                raise RegistryError(
                    f"no active artifact for {signature_name}/{environment}",
                    context={"signature": signature_name, "env": environment},
                )
            row = await sess.get(CompiledArtifactRow, entry.artifact_id)
            assert row is not None  # FK invariant
            return CompiledArtifactRecord.from_row(row)

    async def promote(
        self,
        artifact_id: str,
        *,
        environment: str,
        promoted_by: str,
    ) -> RegistryEntryRecord:
        """Atomic promote: deactivate previous, activate this. One row UPDATE per signature/env."""

        if environment not in VALID_ENVS:
            raise RegistryError(f"unknown environment {environment!r}")

        async with self.session() as sess:
            artifact = await sess.get(CompiledArtifactRow, artifact_id)
            if artifact is None:
                raise RegistryError(
                    f"cannot promote unknown artifact {artifact_id!r}",
                    context={"artifact_id": artifact_id},
                )
            existing = (
                await sess.execute(
                    select(RegistryEntryRow).where(
                        RegistryEntryRow.signature_name == artifact.signature_name,
                        RegistryEntryRow.environment == environment,
                    )
                )
            ).scalar_one_or_none()

            now = datetime.now(UTC)
            previous_artifact_id = existing.artifact_id if existing else None
            if existing is None:
                entry = RegistryEntryRow(
                    id=_new_id(),
                    signature_name=artifact.signature_name,
                    environment=environment,
                    artifact_id=artifact_id,
                    promoted_at=now,
                    promoted_by=promoted_by,
                    previous_artifact_id=None,
                )
                sess.add(entry)
            else:
                existing.previous_artifact_id = existing.artifact_id
                existing.artifact_id = artifact_id
                existing.promoted_at = now
                existing.promoted_by = promoted_by
                entry = existing

            await sess.commit()
            _log.info(
                "registry.promoted",
                signature=artifact.signature_name,
                environment=environment,
                artifact_id=artifact_id,
                previous_artifact_id=previous_artifact_id,
                promoted_by=promoted_by,
            )
            return RegistryEntryRecord(
                id=entry.id,
                signature_name=entry.signature_name,
                environment=entry.environment,
                artifact_id=entry.artifact_id,
                promoted_at=entry.promoted_at,
                promoted_by=entry.promoted_by,
                previous_artifact_id=entry.previous_artifact_id,
            )

    async def rollback(
        self,
        signature_name: str,
        *,
        environment: str,
        promoted_by: str,
    ) -> RegistryEntryRecord:
        """Roll back to ``previous_artifact_id``. One UPDATE, no MinIO write, no recompilation."""

        async with self.session() as sess:
            entry = (
                await sess.execute(
                    select(RegistryEntryRow).where(
                        RegistryEntryRow.signature_name == signature_name,
                        RegistryEntryRow.environment == environment,
                    )
                )
            ).scalar_one_or_none()
            if entry is None or entry.previous_artifact_id is None:
                raise RegistryError(
                    "no previous artifact to roll back to",
                    context={"signature": signature_name, "env": environment},
                )
            rolled = entry.previous_artifact_id
            entry.previous_artifact_id = entry.artifact_id
            entry.artifact_id = rolled
            entry.promoted_at = datetime.now(UTC)
            entry.promoted_by = promoted_by
            await sess.commit()
            _log.warning(
                "registry.rolled_back",
                signature=signature_name,
                environment=environment,
                rolled_to=rolled,
                rolled_by=promoted_by,
            )
            return RegistryEntryRecord(
                id=entry.id,
                signature_name=entry.signature_name,
                environment=entry.environment,
                artifact_id=entry.artifact_id,
                promoted_at=entry.promoted_at,
                promoted_by=entry.promoted_by,
                previous_artifact_id=entry.previous_artifact_id,
            )

    async def history(
        self,
        signature_name: str,
        *,
        limit: int = 50,
    ) -> list[CompiledArtifactRecord]:
        async with self.session() as sess:
            rows = (
                await sess.execute(
                    select(CompiledArtifactRow)
                    .where(CompiledArtifactRow.signature_name == signature_name)
                    .order_by(CompiledArtifactRow.created_at.desc())
                    .limit(limit)
                )
            ).scalars().all()
            return [CompiledArtifactRecord.from_row(r) for r in rows]

    async def init_schema(self) -> None:
        """Create tables. Production runs alembic; tests call this directly."""

        async with self._sessionmaker() as sess:
            engine = sess.bind
            assert engine is not None
            async with engine.begin() as conn:  # type: ignore[union-attr]
                await conn.run_sync(_Base.metadata.create_all)
