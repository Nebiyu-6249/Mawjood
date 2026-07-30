"""Shared test fixtures.

Settings are injected explicitly rather than read from a developer's environment,
so the suite behaves identically on a laptop and in CI.

Two tiers of test:

* **Unit** — pure logic (phrasebank, consent, signatures, the blocklist scan).
  Sockets are blocked; these never touch a database.
* **Integration** — anything that needs PostgreSQL. Marked ``integration``,
  skipped unless ``MAWJOOD_TEST_DATABASE_URL`` points at a live database.
  The schema is built by running the real Alembic migration, so the migration is
  exercised on every run rather than trusted.

Isolation is by TRUNCATE rather than a rolled-back transaction: the pipeline
commits internally, which is the behaviour under test, so a wrapping transaction
would be testing something else. TRUNCATE also sidesteps the audit_log
append-only trigger, which fires on DELETE and not on TRUNCATE.
"""

from __future__ import annotations

import os
import uuid
from collections.abc import AsyncIterator, Awaitable, Callable, Iterator
from contextlib import contextmanager

import pytest
import pytest_socket
from fastapi import FastAPI
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker
from starlette.testclient import TestClient

from mawjood.config import Settings
from mawjood.core.enums import Category
from mawjood.db.base import Base
from mawjood.db.engine import create_engine, create_session_factory
from mawjood.db.models import MerchantCredential, RoutingConfig, Tenant
from mawjood.main import create_app

# ``await routed(Category.SALON, "fake_empty", "fake_auth")``
RoutingSeeder = Callable[..., Awaitable[None]]

# Not a credential: an unreachable placeholder DSN. Unit tests never connect —
# sockets are blocked — and the integration suite supplies a real one via
# MAWJOOD_TEST_DATABASE_URL.
PLACEHOLDER_DATABASE_URL = "postgresql+asyncpg://mawjood:mawjood@127.0.0.1:5432/mawjood_test"

LIVE_DATABASE_URL = os.environ.get("MAWJOOD_TEST_DATABASE_URL") or None

# Belt and braces for anything that reaches for the singleton. Tests inject
# Settings explicitly, so nothing here depends on the ambient environment.
os.environ.setdefault("MAWJOOD_DATABASE_URL", PLACEHOLDER_DATABASE_URL)

TEST_TENANT_SLUG = "test-tenant"

needs_database = pytest.mark.skipif(
    LIVE_DATABASE_URL is None,
    reason="needs a live PostgreSQL via MAWJOOD_TEST_DATABASE_URL",
)

# Every table, in an order safe to truncate. Asserted complete against the
# metadata below — a table added to the schema but forgotten here would leak rows
# between tests, and the failure would look like a flaky test rather than a gap.
_ALL_TABLES = (
    "audit_log",
    "handoff_queue",
    "feedback",
    "attribution",
    "consents",
    "messages",
    "bookings",
    "conversations",
    "leads",
    "merchant_credentials",
    "routing_config",
    "tenants",
)

_MISSING_FROM_TRUNCATE = set(Base.metadata.tables) - set(_ALL_TABLES) - {"alembic_version"}
assert not _MISSING_FROM_TRUNCATE, (
    f"tables missing from the truncate list, so rows will leak between tests: "
    f"{sorted(_MISSING_FROM_TRUNCATE)}"
)


# ---------------------------------------------------------------------------
# Unit-tier fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def settings() -> Settings:
    return Settings(database_url=PLACEHOLDER_DATABASE_URL)


@pytest.fixture
def app(settings: Settings) -> FastAPI:
    return create_app(settings)


@pytest.fixture
def client(app: FastAPI) -> Iterator[TestClient]:
    # The context manager runs lifespan, so app.state.engine exists.
    with TestClient(app) as test_client:
        yield test_client


# ---------------------------------------------------------------------------
# Integration-tier fixtures
# ---------------------------------------------------------------------------


_LOCALHOST = ["127.0.0.1", "localhost", "::1"]


@contextmanager
def _localhost_sockets() -> Iterator[None]:
    """Permit connections to a local database and nothing else.

    "No real network calls in tests, ever" is about reaching third parties, not
    about the test database. Rather than lifting the block wholesale, integration
    tests narrow it to loopback — so an accidental call to a live aggregator or
    an LLM provider still fails loudly inside an integration test.
    """
    # enable_socket first: disable_socket swaps out socket.getaddrinfo, and
    # socket_allow_hosts does not put it back. Without this, name resolution
    # stays blocked and even 127.0.0.1 fails to connect.
    pytest_socket.enable_socket()
    pytest_socket.socket_allow_hosts(_LOCALHOST, allow_unix_socket=True)
    try:
        yield
    finally:
        pytest_socket.disable_socket(allow_unix_socket=True)


@pytest.fixture(scope="session")
def live_database_url() -> str:
    if LIVE_DATABASE_URL is None:
        pytest.skip("MAWJOOD_TEST_DATABASE_URL is not set")
    return LIVE_DATABASE_URL


@pytest.fixture(scope="session")
def migrated_database(live_database_url: str) -> str:
    """Apply the real Alembic migration once per session.

    Running the migration rather than metadata.create_all means every
    integration run exercises the migration itself, including the audit_log
    append-only trigger, which create_all would not produce.
    """
    from alembic import command
    from alembic.config import Config

    from mawjood.db import migrations

    config = Config()
    config.set_main_option("script_location", str(migrations.__path__[0]))
    config.set_main_option("sqlalchemy.url", live_database_url)
    with _localhost_sockets():
        command.upgrade(config, "head")
    return live_database_url


CONSOLE_USERNAME = "ops"
# Not a credential: a fixture value. tests/** already exempts S105.
CONSOLE_PASSWORD = "test-console-password"


@pytest.fixture
def db_settings(migrated_database: str) -> Settings:
    return Settings(
        database_url=migrated_database,
        default_tenant_slug=TEST_TENANT_SLUG,
        default_tenant_name="Test Tenant",
        bsp_provider="360dialog",
        bsp_webhook_secret="test-webhook-secret",
        bsp_verify_token="test-verify-token",
        console_username=CONSOLE_USERNAME,
        console_password=CONSOLE_PASSWORD,
    )


@pytest.fixture
async def db_engine(db_settings: Settings) -> AsyncIterator[AsyncEngine]:
    with _localhost_sockets():
        engine = create_engine(db_settings)
        # TRUNCATE rather than a wrapping transaction: the pipeline commits
        # internally, which is the behaviour under test. TRUNCATE also bypasses
        # the audit_log append-only trigger, which fires on DELETE, not TRUNCATE.
        async with engine.begin() as conn:
            await conn.execute(text(f"TRUNCATE {', '.join(_ALL_TABLES)} RESTART IDENTITY CASCADE"))
        try:
            yield engine
        finally:
            await engine.dispose()


@pytest.fixture
def session_factory(db_engine: AsyncEngine) -> async_sessionmaker[AsyncSession]:
    return create_session_factory(db_engine)


@pytest.fixture
async def db_session(
    session_factory: async_sessionmaker[AsyncSession],
) -> AsyncIterator[AsyncSession]:
    async with session_factory() as session:
        yield session


@pytest.fixture
async def tenant_id(session_factory: async_sessionmaker[AsyncSession]) -> uuid.UUID:
    async with session_factory() as session:
        tenant = Tenant(slug=TEST_TENANT_SLUG, name="Test Tenant")
        session.add(tenant)
        await session.commit()
        return tenant.id


@pytest.fixture
async def other_tenant_id(session_factory: async_sessionmaker[AsyncSession]) -> uuid.UUID:
    """A second tenant, for proving isolation."""
    async with session_factory() as session:
        tenant = Tenant(slug="other-tenant", name="Other Tenant")
        session.add(tenant)
        await session.commit()
        return tenant.id


@pytest.fixture
async def db_app(
    db_settings: Settings, db_engine: AsyncEngine, tenant_id: uuid.UUID
) -> AsyncIterator[FastAPI]:
    """An app wired to the live test database, with the tenant already seeded."""
    application = create_app(db_settings)
    yield application


@pytest.fixture
async def routed(
    session_factory: async_sessionmaker[AsyncSession], tenant_id: uuid.UUID
) -> RoutingSeeder:
    """Configure a category's cascade order, and a merchant per platform.

    Tests that exercise the cascade end to end need routing rows, and writing
    them inline in each test buries the interesting part. ``await routed(
    Category.SALON, "fake_empty", "fake_auth")`` reads as the scenario it is.
    """

    async def seed(category: Category, *slugs: str) -> None:
        async with session_factory() as session:
            for position, slug in enumerate(slugs):
                session.add(
                    RoutingConfig(
                        tenant_id=tenant_id,
                        category=str(category),
                        platform_slug=slug,
                        position=position,
                    )
                )
                session.add(
                    MerchantCredential(
                        tenant_id=tenant_id,
                        platform_slug=slug,
                        display_name=f"{slug} merchant",
                        area="Dubai Marina",
                        external_ids={"centre_id": f"c-{position}"},
                    )
                )
            await session.commit()

    return seed


@pytest.fixture
def console_auth() -> tuple[str, str]:
    return (CONSOLE_USERNAME, CONSOLE_PASSWORD)


@pytest.fixture
async def console(db_app: FastAPI) -> AsyncIterator[TestClient]:
    """A logged-in console client against the live test database.

    The context manager runs lifespan, which is what puts ``session_factory`` on
    app.state — the console reads it from there on every request.
    """
    with TestClient(db_app) as test_client:
        test_client.auth = (CONSOLE_USERNAME, CONSOLE_PASSWORD)
        yield test_client
