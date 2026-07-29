"""Alembic environment.

The database URL comes from Mawjood's settings, never from alembic.ini, so a
connection string has exactly one home and that home is the environment.
alembic.ini is committed; a DSN in it would be a committed credential.

Phase 0 ships this scaffold with no revisions, so ``alembic upgrade head`` is a
successful no-op. Models and the first revision arrive in Phase 1.
"""

from __future__ import annotations

import asyncio
from logging.config import fileConfig

from alembic import context
from sqlalchemy import pool
from sqlalchemy.engine import Connection
from sqlalchemy.ext.asyncio import async_engine_from_config

from mawjood.config import get_settings

config = context.config

if config.config_file_name is not None:
    fileConfig(config.config_file_name)

# Imported here, after fileConfig, because importing the models pulls in the
# application logging configuration and alembic must own logging setup first.
from mawjood.db import models  # noqa: E402, F401  (registers every model)
from mawjood.db.base import Base  # noqa: E402

target_metadata = Base.metadata

# Honour a URL supplied programmatically (the test fixtures and the compose
# bootstrap both set one), and otherwise take it from settings. Overriding
# unconditionally would silently point a caller's migration at a different
# database than the one they asked for.
if not config.get_main_option("sqlalchemy.url", None):
    config.set_main_option("sqlalchemy.url", str(get_settings().database_url))


def run_migrations_offline() -> None:
    """Emit SQL to stdout without a live connection."""
    context.configure(
        url=config.get_main_option("sqlalchemy.url"),
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        compare_type=True,
    )
    with context.begin_transaction():
        context.run_migrations()


def do_run_migrations(connection: Connection) -> None:
    context.configure(
        connection=connection,
        target_metadata=target_metadata,
        compare_type=True,
    )
    with context.begin_transaction():
        context.run_migrations()


async def run_async_migrations() -> None:
    connectable = async_engine_from_config(
        config.get_section(config.config_ini_section, {}),
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )
    async with connectable.connect() as connection:
        await connection.run_sync(do_run_migrations)
    await connectable.dispose()


def run_migrations_online() -> None:
    asyncio.run(run_async_migrations())


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
