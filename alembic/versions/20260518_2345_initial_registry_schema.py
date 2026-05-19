"""initial registry schema

Mirrors ``putsch_compile.registry._Base.metadata`` exactly so a fresh database can be brought
up via ``alembic upgrade head`` instead of ``Registry.init_schema()`` (which is for tests only).

Revision ID: 0001_initial
Revises:
Create Date: 2026-05-18 23:45:00
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0001_initial"
down_revision: str | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "compiled_artifacts",
        sa.Column("id", sa.String(length=40), primary_key=True),
        sa.Column("signature_name", sa.String(length=64), nullable=False, index=True),
        sa.Column("signature_version", sa.String(length=20), nullable=False),
        sa.Column("signature_version_hash", sa.String(length=16), nullable=False, index=True),
        sa.Column("model", sa.String(length=120), nullable=False),
        sa.Column("content_hash", sa.String(length=64), nullable=False, unique=True, index=True),
        sa.Column("dataset_hash", sa.String(length=64), nullable=False),
        sa.Column("seed", sa.Integer(), nullable=False),
        sa.Column("holdout_accuracy", sa.Float(), nullable=False),
        sa.Column("cost_eur_per_call", sa.Float(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("optimizer", sa.String(length=32), nullable=False, server_default="GEPA"),
        sa.Column("actor", sa.String(length=120), nullable=False),
    )
    op.create_index(
        "ix_artifacts_signature_version_hash",
        "compiled_artifacts",
        ["signature_name", "signature_version_hash"],
    )

    op.create_table(
        "registry_entries",
        sa.Column("id", sa.String(length=40), primary_key=True),
        sa.Column("signature_name", sa.String(length=64), nullable=False),
        sa.Column("environment", sa.String(length=16), nullable=False),
        sa.Column(
            "artifact_id",
            sa.String(length=40),
            sa.ForeignKey("compiled_artifacts.id"),
            nullable=False,
        ),
        sa.Column("promoted_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("promoted_by", sa.String(length=120), nullable=False),
        sa.Column(
            "previous_artifact_id",
            sa.String(length=40),
            sa.ForeignKey("compiled_artifacts.id"),
            nullable=True,
        ),
        sa.UniqueConstraint(
            "signature_name", "environment", name="uq_active_per_signature_env"
        ),
    )


def downgrade() -> None:
    op.drop_table("registry_entries")
    op.drop_index("ix_artifacts_signature_version_hash", table_name="compiled_artifacts")
    op.drop_table("compiled_artifacts")
