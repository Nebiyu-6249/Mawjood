"""Startup database preparation.

Both behaviours here are off by default and switched on by docker-compose, so
``docker compose up`` yields a working stack from one command while a deployment
still runs migrations as a deliberate, reviewable step.
"""

from __future__ import annotations

import asyncio

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from mawjood.config import Settings
from mawjood.db.repositories.core import TenantRepository
from mawjood.observability.logging import get_logger

log = get_logger(__name__)


def _run_alembic_upgrade(database_url: str) -> None:
    """Apply migrations synchronously, in a worker thread.

    Alembic drives its own event loop for the async engine, so it cannot run on
    the loop already serving the application.
    """
    from alembic import command
    from alembic.config import Config

    from mawjood.db import migrations

    config = Config()
    config.set_main_option("script_location", str(migrations.__path__[0]))
    config.set_main_option("sqlalchemy.url", database_url)
    command.upgrade(config, "head")


async def prepare_database(
    settings: Settings, session_factory: async_sessionmaker[AsyncSession]
) -> None:
    if settings.run_migrations_on_start:
        log.info("db.migrations.applying")
        await asyncio.to_thread(_run_alembic_upgrade, str(settings.database_url))
        log.info("db.migrations.applied")

    if settings.seed_dev_tenant_on_start:
        async with session_factory() as session:
            tenant = await TenantRepository(session).ensure(
                settings.default_tenant_slug, settings.default_tenant_name
            )
            await session.commit()
        log.info("db.seeded_tenant", slug=tenant.slug)


__all__ = ["prepare_database"]
