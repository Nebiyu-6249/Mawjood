"""Async database engine and the readiness probe.

Scaffold only. Models, repositories and migrations arrive in Phase 1; this module
exists so ``/readyz`` can tell the truth about the database from day one.
"""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass

from sqlalchemy import text
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from mawjood.config import Settings


@dataclass(frozen=True, slots=True)
class DatabaseHealth:
    """Outcome of a readiness probe against PostgreSQL."""

    healthy: bool
    latency_ms: float
    error: str | None = None


def create_engine(settings: Settings) -> AsyncEngine:
    """Build the async engine. Connection details come from settings only."""
    return create_async_engine(
        str(settings.database_url),
        echo=settings.database_echo,
        pool_size=settings.database_pool_size,
        max_overflow=settings.database_max_overflow,
        pool_pre_ping=True,
        connect_args={"timeout": settings.database_connect_timeout_s},
    )


def create_session_factory(engine: AsyncEngine) -> async_sessionmaker[AsyncSession]:
    return async_sessionmaker(engine, expire_on_commit=False, class_=AsyncSession)


async def check_database(engine: AsyncEngine, timeout_s: float = 5.0) -> DatabaseHealth:
    """Probe the database with ``SELECT 1``.

    Never raises: readiness is a report, not an exception. The returned ``error``
    carries the exception type only — details go to the log, which redacts
    credentials, so the probe response cannot leak a DSN.
    """
    started = time.perf_counter()
    try:
        async with asyncio.timeout(timeout_s):
            async with engine.connect() as conn:
                await conn.execute(text("SELECT 1"))
    except TimeoutError:
        return DatabaseHealth(
            healthy=False,
            latency_ms=round((time.perf_counter() - started) * 1000, 2),
            error="timeout",
        )
    # Deliberately broad: any driver failure whatsoever means "not ready", and a
    # readiness probe that raises is a readiness probe that lies.
    except Exception as exc:
        return DatabaseHealth(
            healthy=False,
            latency_ms=round((time.perf_counter() - started) * 1000, 2),
            error=type(exc).__name__,
        )
    return DatabaseHealth(
        healthy=True,
        latency_ms=round((time.perf_counter() - started) * 1000, 2),
    )


__all__ = [
    "DatabaseHealth",
    "check_database",
    "create_engine",
    "create_session_factory",
]
