"""Alembic environment.

The DSN is sourced from ``Settings.registry_db.dsn`` so the same migration set runs against any
environment without editing alembic.ini. Async engine; uses ``asyncio.run`` to bridge into the
synchronous alembic context.
"""

from __future__ import annotations

import asyncio
from logging.config import fileConfig

from alembic import context
from sqlalchemy.engine import Connection
from sqlalchemy.ext.asyncio import async_engine_from_config

from putsch_compile.config import get_settings
from putsch_compile.registry import _Base

config = context.config

if config.config_file_name is not None:
    fileConfig(config.config_file_name)

target_metadata = _Base.metadata


def _dsn() -> str:
    # The DSN may be async (...+asyncpg://) — alembic happily runs against that with
    # ``async_engine_from_config``. For sqlite-in-memory in tests we still pass through.
    return get_settings().registry_db.dsn.get_secret_value()


def run_migrations_offline() -> None:
    """Emit SQL to stdout without a live DB connection. Useful for code review."""

    context.configure(
        url=_dsn(),
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
    )
    with context.begin_transaction():
        context.run_migrations()


def _run_sync(connection: Connection) -> None:
    context.configure(connection=connection, target_metadata=target_metadata)
    with context.begin_transaction():
        context.run_migrations()


async def run_migrations_online() -> None:
    overrides = {**config.get_section(config.config_ini_section, {}), "sqlalchemy.url": _dsn()}
    engine = async_engine_from_config(overrides, prefix="sqlalchemy.")
    async with engine.connect() as connection:
        await connection.run_sync(_run_sync)
    await engine.dispose()


if context.is_offline_mode():
    run_migrations_offline()
else:
    asyncio.run(run_migrations_online())
