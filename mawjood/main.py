"""FastAPI application factory.

Phase 0 wires configuration, structured logging, Sentry and the health endpoints.
The webhook, console and core routers land in later phases.

There is deliberately no module-level ``app``. Building one at import time would
mean that merely importing this module reconfigures global logging, initialises
Sentry and demands a valid environment — which breaks tooling and tests that only
want to introspect the package. Run it with uvicorn's factory mode instead::

    uvicorn mawjood.main:create_app --factory
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any

from fastapi import FastAPI

from mawjood.api.console.routes import router as console_router
from mawjood.api.health import router as health_router
from mawjood.api.ratelimit import RateLimiter
from mawjood.api.webhooks.whatsapp import router as whatsapp_router
from mawjood.config import Settings, get_settings
from mawjood.core.conversation.phrasebank import get_phrasebank
from mawjood.db.bootstrap import prepare_database
from mawjood.db.engine import create_engine, create_session_factory
from mawjood.observability.logging import (
    RequestContextMiddleware,
    configure_logging,
    get_logger,
)
from mawjood.services.bsp.registry import build_bsp

log = get_logger(__name__)


def _scrub_event(event: Any, hint: Any) -> Any:
    """Strip anything consumer-identifying before an event leaves the process.

    ``send_default_pii=False`` covers Sentry's own automatic capture. It does not
    cover a request body we attached ourselves, or a webhook payload sitting in a
    local variable of a frame in the stack trace. Consumer data must not leave the
    declared processors (CLAUDE.md section 11), and Sentry is not one of them for
    message content — so request bodies and local variables go.
    """
    event.pop("request", None)
    for entry in event.get("exception", {}).get("values", []):
        for frame in entry.get("stacktrace", {}).get("frames", []):
            frame.pop("vars", None)
    return event


def _init_sentry(settings: Settings) -> None:
    if settings.sentry_dsn is None:
        return
    import sentry_sdk

    sentry_sdk.init(
        dsn=settings.sentry_dsn.get_secret_value(),
        environment=str(settings.environment),
        release=f"mawjood@{settings.service_version}",
        traces_sample_rate=settings.sentry_traces_sample_rate,
        # Consumer data is never used to train models and does not leave the
        # declared providers. Do not ship request bodies or PII to Sentry.
        send_default_pii=False,
        before_send=_scrub_event,
        # Region is a tag so a multi-region deployment can alert per region
        # without a second project.
        server_name=settings.region,
    )
    sentry_sdk.set_tag("data_residency", settings.data_residency)


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    # Set by create_app, so an injected Settings is honoured rather than silently
    # replaced by the process-wide singleton.
    settings: Settings = app.state.settings

    engine = create_engine(settings)
    session_factory = create_session_factory(engine)
    app.state.engine = engine
    app.state.session_factory = session_factory
    app.state.bsp = build_bsp(settings)
    # Per-process token buckets. A multi-instance deployment multiplies the
    # effective rate; see the note in api/ratelimit.py and DEPLOY.md.
    app.state.rate_limiter = RateLimiter()

    # Loaded eagerly so a malformed or incomplete locale file fails at startup
    # rather than the first time a consumer needs a reply.
    phrasebank = get_phrasebank()
    app.state.phrasebank = phrasebank

    log.info(
        "app.startup",
        environment=str(settings.environment),
        region=settings.region,
        data_residency=settings.data_residency,
        timezone=settings.timezone,
        database=settings.database_url_safe,
        bsp_provider=settings.bsp_provider,
        locales=phrasebank.locales,
    )

    await prepare_database(settings, session_factory)

    try:
        yield
    finally:
        await engine.dispose()
        log.info("app.shutdown")


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or get_settings()

    configure_logging(
        level=settings.log_level,
        log_format=settings.log_format,
        service_name=settings.service_name,
        environment=str(settings.environment),
        region=settings.region,
    )
    _init_sentry(settings)

    app = FastAPI(
        title="Mawjood",
        description="WhatsApp AI booking assistant",
        version="0.1.0",
        lifespan=lifespan,
        # No API docs in production: this service handles consumer PII.
        docs_url=None if settings.is_production else "/docs",
        redoc_url=None,
        openapi_url=None if settings.is_production else "/openapi.json",
    )
    app.state.settings = settings
    app.add_middleware(RequestContextMiddleware)
    app.include_router(health_router)
    app.include_router(whatsapp_router)
    app.include_router(console_router)
    return app


__all__ = ["create_app", "lifespan"]
