"""Liveness and readiness.

The point of these tests is that ``/readyz`` is *honest*: green means the
database answered, red means it did not. A readiness probe that always returns
200 is worse than no probe at all.
"""

from __future__ import annotations

import pytest
from fastapi import FastAPI
from starlette.testclient import TestClient

from mawjood.db.engine import DatabaseHealth


def _stub_database(monkeypatch: pytest.MonkeyPatch, health: DatabaseHealth) -> None:
    async def fake_check(*_args: object, **_kwargs: object) -> DatabaseHealth:
        return health

    monkeypatch.setattr("mawjood.api.health.check_database", fake_check)


class TestLiveness:
    def test_healthz_is_ok(self, client: TestClient) -> None:
        response = client.get("/healthz")
        assert response.status_code == 200
        assert response.json()["status"] == "ok"
        assert response.json()["service"] == "mawjood"

    def test_healthz_stays_up_when_the_database_is_down(
        self, client: TestClient, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Liveness must not depend on the database, or an outage kills the pods."""
        _stub_database(monkeypatch, DatabaseHealth(healthy=False, latency_ms=1.0, error="down"))
        assert client.get("/healthz").status_code == 200

    def test_request_id_is_echoed(self, client: TestClient) -> None:
        response = client.get("/healthz", headers={"x-request-id": "abc123"})
        assert response.headers["x-request-id"] == "abc123"

    def test_request_id_is_generated_when_absent(self, client: TestClient) -> None:
        assert client.get("/healthz").headers["x-request-id"]


class TestReadiness:
    def test_ready_when_the_database_answers(
        self, client: TestClient, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _stub_database(monkeypatch, DatabaseHealth(healthy=True, latency_ms=1.5))
        response = client.get("/readyz")
        assert response.status_code == 200
        body = response.json()
        assert body["status"] == "ready"
        assert body["checks"]["database"]["healthy"] is True

    def test_not_ready_when_the_database_is_unreachable(
        self, client: TestClient, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _stub_database(
            monkeypatch,
            DatabaseHealth(healthy=False, latency_ms=5000.0, error="ConnectionRefusedError"),
        )
        response = client.get("/readyz")
        assert response.status_code == 503
        body = response.json()
        assert body["status"] == "not_ready"
        assert body["checks"]["database"]["healthy"] is False
        assert body["checks"]["database"]["error"] == "ConnectionRefusedError"

    def test_unreachable_database_fails_closed_for_real(self, client: TestClient) -> None:
        """No stub: sockets are blocked, so the genuine probe path runs and fails.

        This exercises check_database's real exception handling rather than a
        mock of it, and proves the probe never raises out of the endpoint.
        """
        response = client.get("/readyz")
        assert response.status_code == 503
        assert response.json()["status"] == "not_ready"

    def test_readiness_reports_residency(
        self, client: TestClient, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _stub_database(monkeypatch, DatabaseHealth(healthy=True, latency_ms=1.0))
        body = client.get("/readyz").json()
        assert body["data_residency"] in {"gcc", "eu", "india"}
        assert not body["region"].lower().startswith("us")

    def test_readyz_never_leaks_the_database_password(self, client: TestClient) -> None:
        assert "mawjood:mawjood" not in client.get("/readyz").text


class TestAppWiring:
    def test_lifespan_builds_the_engine_and_session_factory(self, app: FastAPI) -> None:
        with TestClient(app):
            assert app.state.engine is not None
            assert app.state.session_factory is not None

    def test_injected_settings_are_honoured(self, app: FastAPI) -> None:
        """create_app(settings) must not be quietly overridden by the singleton."""
        with TestClient(app):
            assert app.state.settings.timezone == "Asia/Dubai"
