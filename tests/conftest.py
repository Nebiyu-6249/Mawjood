"""Shared test fixtures.

Settings are injected explicitly rather than read from a developer's environment,
so the suite behaves identically on a laptop and in CI.
"""

from __future__ import annotations

import os
from collections.abc import Iterator

import pytest
from fastapi import FastAPI
from starlette.testclient import TestClient

from mawjood.config import Settings
from mawjood.main import create_app

# Not a credential: an unreachable placeholder DSN. Unit tests never connect —
# sockets are blocked — and the integration suite supplies a real one via
# MAWJOOD_TEST_DATABASE_URL.
PLACEHOLDER_DATABASE_URL = "postgresql+asyncpg://mawjood:mawjood@127.0.0.1:5432/mawjood_test"

# Belt and braces for anything that reaches for the singleton. Tests inject
# Settings explicitly, so nothing here depends on the ambient environment.
os.environ.setdefault("MAWJOOD_DATABASE_URL", PLACEHOLDER_DATABASE_URL)


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
