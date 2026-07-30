"""The ops console. Read-only, and structurally so.

Jinja2 + HTMX, no build step and no npm. Every route below is a ``GET`` except
the login form, which sets a cookie and touches no domain data.

**There are no mutation routes.** Not "no mutation routes we currently use" —
none exist. ``tests/test_console.py`` walks the AST of this whole package and
fails on any non-GET domain route, any ``session.add``, ``commit``, ``flush``,
``delete``, or any imperative SQL construct. A read-only console that relies on
nobody adding a POST later is a convention; this is a property with a test.

The handoff reply path that Phase 2 put here has moved to ``tools/handoff.py``.
CLAUDE.md decision 7 wants "queue + console reply + bot mute"; the queue and the
mute are here, the reply is a CLI. The capability survives, the console stays
honest about what it is.

## The views

Seven, and one of them is the reason to open it at all:

* ``/console`` — headline counts, health, and what is waiting.
* ``/console/conversations`` — live, most recent first.
* ``/console/conversations/{id}`` — transcript, bookings, and a link to the trace.
* ``/console/conversations/{id}/trace`` — **the routing trace.** Every aggregator
  tried, its outcome, its latency, why the engine advanced, and exactly what the
  consumer saw at each step, on one timeline. It is the Mawjood promise made
  visible and it is the fastest way to debug a bad conversation.
* ``/console/bookings`` — by day, week or month.
* ``/console/aggregators`` — success rate, median and p95 latency, outcome mix.
* ``/console/failures`` — every attempt that did not succeed, with the reason.
* ``/console/sources`` — per source code, leads and bookings.
* ``/console/scheduled`` — reminders due, sent, and held behind template approval.

Authentication is a signed session cookie (``auth.py``). The console refuses to
serve at all when no credential is configured: an unauthenticated console
exposes every consumer's transcript, so a misconfiguration must fail closed.
"""

from __future__ import annotations

import uuid
from pathlib import Path
from typing import Annotated, Any

from fastapi import APIRouter, Depends, Form, HTTPException, Request, Response, status
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates

from mawjood.api.console import queries
from mawjood.api.console.auth import (
    COOKIE_NAME,
    DEFAULT_TTL_SECONDS,
    check_credentials,
    current_session,
    issue,
    session_secret,
)
from mawjood.config import Settings
from mawjood.core.enums import Direction
from mawjood.core.notifications import Offsets, build_template_registry
from mawjood.db.repositories.core import TenantRepository
from mawjood.observability.logging import get_logger

router = APIRouter(prefix="/console", tags=["console"])
log = get_logger(__name__)

TEMPLATES = Jinja2Templates(directory=str(Path(__file__).parent / "templates"))


async def operator(request: Request) -> str:
    """Require a live session, or refuse.

    503 when nothing is configured — an operator who sees "unavailable" goes and
    sets the password; one who sees the dashboard never learns it was public.
    303 to the login page when merely unauthenticated, because these are pages a
    person is looking at, not an API a client is calling.
    """
    settings: Settings = request.app.state.settings
    if settings.console_password is None:
        log.error("console.no_credential_configured")
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="console credentials are not configured",
        )

    session = current_session(request)
    if session is None:
        raise HTTPException(
            status_code=status.HTTP_303_SEE_OTHER,
            detail="sign in",
            headers={"Location": "/console/login"},
        )
    return session.username


Operator = Annotated[str, Depends(operator)]


async def _tenant_id(request: Request, session: Any) -> uuid.UUID:
    settings: Settings = request.app.state.settings
    tenant = await TenantRepository(session).get_by_slug(settings.default_tenant_slug)
    if tenant is None:
        raise HTTPException(status_code=404, detail="tenant not configured")
    return tenant.id


def _render(request: Request, template: str, context: dict[str, Any]) -> HTMLResponse:
    return TEMPLATES.TemplateResponse(request, template, context)


# ---------------------------------------------------------------------------
# Session
# ---------------------------------------------------------------------------


@router.get("/login", response_class=HTMLResponse)
async def login_form(request: Request) -> HTMLResponse:
    settings: Settings = request.app.state.settings
    if settings.console_password is None:
        raise HTTPException(status_code=503, detail="console credentials are not configured")
    return _render(request, "login.html", {"error": None})


@router.post("/login")
async def login(
    request: Request,
    username: Annotated[str, Form()],
    password: Annotated[str, Form()],
) -> Response:
    """The console's only POST. Sets a cookie; touches no domain data."""
    settings: Settings = request.app.state.settings
    secret = session_secret(settings)
    if secret is None:
        raise HTTPException(status_code=503, detail="console credentials are not configured")

    if not check_credentials(username, password, settings=settings):
        log.warning("console.authentication_failed", username=username)
        return _render(request, "login.html", {"error": "Those details did not match."})

    response = RedirectResponse("/console", status_code=303)
    response.set_cookie(
        COOKIE_NAME,
        issue(username, secret=secret),
        max_age=DEFAULT_TTL_SECONDS,
        httponly=True,
        samesite="lax",
        # Set only over HTTPS outside local development. A Secure cookie on a
        # plain-HTTP localhost simply never arrives, which looks like a broken
        # login rather than a security setting.
        secure=settings.is_production,
        path="/console",
    )
    log.info("console.signed_in", username=username)
    return response


@router.get("/logout")
async def logout() -> Response:
    """Clear the cookie.

    A GET on purpose: it is a link in the header, and a stateless session cannot
    be revoked server-side anyway, so there is nothing a POST would protect.
    """
    response = RedirectResponse("/console/login", status_code=303)
    response.delete_cookie(COOKIE_NAME, path="/console")
    return response


# ---------------------------------------------------------------------------
# Views
# ---------------------------------------------------------------------------


@router.get("", response_class=HTMLResponse)
async def dashboard(request: Request, who: Operator) -> HTMLResponse:
    factory = request.app.state.session_factory
    async with factory() as session:
        tenant_id = await _tenant_id(request, session)
        context = {
            "stats": await queries.headline_counts(session, tenant_id),
            "health": await queries.health(session, tenant_id),
            "queue": await queries.handoff_queue(session, tenant_id),
            "recent": (await queries.live_conversations(session, tenant_id, limit=8)),
            "aggregators": await queries.aggregator_stats(session, tenant_id),
            "satisfaction": await queries.satisfaction_summary(session, tenant_id),
            "operator": who,
        }
    return _render(request, "dashboard.html", context)


@router.get("/conversations", response_class=HTMLResponse)
async def conversations(request: Request, who: Operator) -> HTMLResponse:
    factory = request.app.state.session_factory
    async with factory() as session:
        tenant_id = await _tenant_id(request, session)
        rows = await queries.live_conversations(session, tenant_id, limit=100)
    return _render(request, "conversations.html", {"rows": rows, "operator": who})


@router.get("/conversations/{conversation_id}", response_class=HTMLResponse)
async def conversation_detail(
    request: Request, conversation_id: uuid.UUID, who: Operator
) -> HTMLResponse:
    factory = request.app.state.session_factory
    async with factory() as session:
        tenant_id = await _tenant_id(request, session)
        found = await queries.conversation_with_lead(session, tenant_id, conversation_id)
        if found is None:
            raise HTTPException(status_code=404, detail="conversation not found")
        conversation, lead = found
        context = {
            "conversation": conversation,
            "lead": lead,
            "messages": await queries.transcript(session, tenant_id, conversation_id),
            "bookings": await queries.bookings_for_conversation(
                session, tenant_id, conversation_id
            ),
            "Direction": Direction,
            "operator": who,
        }
    return _render(request, "conversation.html", context)


@router.get("/conversations/{conversation_id}/trace", response_class=HTMLResponse)
async def routing_trace(
    request: Request, conversation_id: uuid.UUID, who: Operator
) -> HTMLResponse:
    """The decision tree for one conversation. The view worth opening the app for.

    Engine steps and consumer-visible messages on one timeline, because the
    interleaving is the explanation: "three platforms refused and the consumer
    saw one holding message" only reads as a story when both are in sequence.
    """
    factory = request.app.state.session_factory
    async with factory() as session:
        tenant_id = await _tenant_id(request, session)
        found = await queries.conversation_with_lead(session, tenant_id, conversation_id)
        if found is None:
            raise HTTPException(status_code=404, detail="conversation not found")
        conversation, lead = found
        steps = await queries.routing_trace(session, tenant_id, conversation_id)

    attempts = [step for step in steps if step.is_attempt]
    seen_by_consumer = [step for step in steps if step.consumer_visible]
    return _render(
        request,
        "trace.html",
        {
            "conversation": conversation,
            "lead": lead,
            "steps": steps,
            "attempt_count": len(attempts),
            "platforms_tried": sorted({s.platform_slug for s in attempts if s.platform_slug}),
            "messages_seen": len(seen_by_consumer),
            "operator": who,
        },
    )


@router.get("/bookings", response_class=HTMLResponse)
async def bookings(request: Request, who: Operator, grain: queries.Grain = "day") -> HTMLResponse:
    if grain not in ("day", "week", "month"):
        raise HTTPException(status_code=400, detail="grain must be day, week or month")
    factory = request.app.state.session_factory
    async with factory() as session:
        tenant_id = await _tenant_id(request, session)
        rows = await queries.bookings_by_grain(session, tenant_id, grain=grain)
    return _render(request, "bookings.html", {"rows": rows, "grain": grain, "operator": who})


@router.get("/aggregators", response_class=HTMLResponse)
async def aggregators(request: Request, who: Operator) -> HTMLResponse:
    factory = request.app.state.session_factory
    async with factory() as session:
        tenant_id = await _tenant_id(request, session)
        stats = await queries.aggregator_stats(session, tenant_id)
    return _render(request, "aggregators.html", {"stats": stats, "operator": who})


@router.get("/failures", response_class=HTMLResponse)
async def failures(request: Request, who: Operator) -> HTMLResponse:
    factory = request.app.state.session_factory
    async with factory() as session:
        tenant_id = await _tenant_id(request, session)
        rows = await queries.failed_attempts(session, tenant_id)
    return _render(request, "failures.html", {"rows": rows, "operator": who})


@router.get("/sources", response_class=HTMLResponse)
async def sources(request: Request, who: Operator) -> HTMLResponse:
    factory = request.app.state.session_factory
    async with factory() as session:
        tenant_id = await _tenant_id(request, session)
        rows = await queries.source_counts(session, tenant_id)
    return _render(request, "sources.html", {"rows": rows, "operator": who})


@router.get("/scheduled", response_class=HTMLResponse)
async def scheduled(request: Request, who: Operator) -> HTMLResponse:
    settings: Settings = request.app.state.settings
    factory = request.app.state.session_factory
    async with factory() as session:
        tenant_id = await _tenant_id(request, session)
        rows = await queries.scheduled_for_tenant(session, tenant_id)
    registry = build_template_registry(settings)
    return _render(
        request,
        "scheduled.html",
        {
            "rows": rows,
            "templates": registry.declared,
            "any_approved": registry.any_approved,
            "window_hours": int(Offsets.from_settings(settings).session_window_hours),
            "operator": who,
        },
    )


@router.get("/health", response_class=HTMLResponse)
async def health_view(request: Request, who: Operator) -> HTMLResponse:
    factory = request.app.state.session_factory
    async with factory() as session:
        tenant_id = await _tenant_id(request, session)
        report = await queries.health(session, tenant_id)
    return _render(request, "health.html", {"health": report, "operator": who})


__all__ = ["router"]
