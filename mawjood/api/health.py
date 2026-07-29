"""Liveness and readiness endpoints.

``/healthz`` answers "is this process alive" and touches nothing else, so a
database outage never causes an orchestrator to kill healthy pods.

``/readyz`` answers "should this instance receive traffic" and actually probes
PostgreSQL. It returns 503 when the database is unreachable — an honest readiness
probe is the one useful place in the system where saying "not ready" is correct.
The no-empty-shelves invariant governs consumer-facing copy; this is an operator
endpoint and it tells operators the truth.
"""

from __future__ import annotations

from typing import Literal

from fastapi import APIRouter, Request, Response, status
from pydantic import BaseModel

from mawjood.config import Settings
from mawjood.db.engine import check_database

router = APIRouter(tags=["health"])


class LivenessResponse(BaseModel):
    status: Literal["ok"]
    service: str
    environment: str


class ReadinessCheck(BaseModel):
    healthy: bool
    latency_ms: float
    error: str | None = None


class ReadinessResponse(BaseModel):
    status: Literal["ready", "not_ready"]
    service: str
    environment: str
    region: str
    data_residency: str
    checks: dict[str, ReadinessCheck]


@router.get("/healthz", response_model=LivenessResponse)
async def healthz(request: Request) -> LivenessResponse:
    settings: Settings = request.app.state.settings
    return LivenessResponse(
        status="ok",
        service=settings.service_name,
        environment=str(settings.environment),
    )


@router.get("/readyz", response_model=ReadinessResponse)
async def readyz(request: Request, response: Response) -> ReadinessResponse:
    settings: Settings = request.app.state.settings
    engine = request.app.state.engine

    database = await check_database(engine, timeout_s=settings.database_connect_timeout_s)
    checks = {
        "database": ReadinessCheck(
            healthy=database.healthy,
            latency_ms=database.latency_ms,
            error=database.error,
        )
    }

    ready = all(check.healthy for check in checks.values())
    if not ready:
        response.status_code = status.HTTP_503_SERVICE_UNAVAILABLE

    return ReadinessResponse(
        status="ready" if ready else "not_ready",
        service=settings.service_name,
        environment=str(settings.environment),
        region=settings.region,
        data_residency=settings.data_residency,
        checks=checks,
    )


__all__ = ["router"]
