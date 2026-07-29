"""Readiness against a live PostgreSQL.

The unit tests stub the probe. These run it for real, both ways, so we know
``/readyz`` reflects an actual database rather than a mock of one.

Skipped unless ``MAWJOOD_TEST_DATABASE_URL`` points at a reachable database.
``make test-integration`` sets it up.
"""

from __future__ import annotations

import os

import pytest
from starlette.testclient import TestClient

from mawjood.config import Settings
from mawjood.main import create_app

LIVE_DATABASE_URL = os.environ.get("MAWJOOD_TEST_DATABASE_URL")

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(
        not LIVE_DATABASE_URL,
        reason="needs a live PostgreSQL via MAWJOOD_TEST_DATABASE_URL",
    ),
]


@pytest.mark.usefixtures("socket_enabled")
def test_readyz_is_green_against_a_live_database() -> None:
    settings = Settings(database_url=LIVE_DATABASE_URL)
    with TestClient(create_app(settings)) as client:
        response = client.get("/readyz")

    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "ready"
    assert body["checks"]["database"]["healthy"] is True
    assert body["checks"]["database"]["error"] is None
    assert body["checks"]["database"]["latency_ms"] > 0


@pytest.mark.usefixtures("socket_enabled")
def test_readyz_goes_red_when_the_database_is_gone() -> None:
    """Same code path, a port nothing is listening on. Red must mean red."""
    dead = "postgresql+asyncpg://mawjood:mawjood@127.0.0.1:59999/mawjood"
    settings = Settings(database_url=dead)
    with TestClient(create_app(settings)) as client:
        response = client.get("/readyz")

    assert response.status_code == 503
    body = response.json()
    assert body["status"] == "not_ready"
    assert body["checks"]["database"]["healthy"] is False
    assert body["checks"]["database"]["error"]


@pytest.mark.usefixtures("socket_enabled")
def test_healthz_is_green_regardless_of_the_database() -> None:
    dead = "postgresql+asyncpg://mawjood:mawjood@127.0.0.1:59999/mawjood"
    settings = Settings(database_url=dead)
    with TestClient(create_app(settings)) as client:
        assert client.get("/healthz").status_code == 200
