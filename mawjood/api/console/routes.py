"""The ops console.

Jinja2 + HTMX, no build step and no npm. Read-only everywhere **except the handoff
reply path** — decision 7 knowingly stretches "read-only ops console", because a
queue nobody can answer from is not a handoff, it is a waiting room.

What it shows:

* the handoff queue, oldest first, so the longest wait is at the top;
* a conversation transcript, operator replies visibly distinct from Mawjood's;
* the routing audit trail for that conversation — every platform tried, its
  outcome, its latency, and why the cascade advanced.

That last view is the reason the console exists. When someone asks "why did this
consumer end up with a human?", the answer is on one screen rather than in a log
search.

Authentication is HTTP Basic against a configured operator credential. Thin on
purpose: this is an internal tool behind whatever ingress the deployment puts in
front of it, and inventing a session system for it would be scope nobody asked
for. It refuses to serve at all when no credential is configured.
"""

from __future__ import annotations

import secrets
import uuid
from pathlib import Path
from typing import Annotated, Any

from fastapi import APIRouter, Depends, Form, HTTPException, Request, status
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.security import HTTPBasic, HTTPBasicCredentials
from fastapi.templating import Jinja2Templates
from sqlalchemy import func, select

from mawjood.config import Settings
from mawjood.core.enums import Direction
from mawjood.core.handoff import (
    HandoffAlreadyClaimed,
    HandoffNotFound,
    claim,
    open_queue,
    release,
    reply,
)
from mawjood.db.models import AuditLog, Booking, Conversation, HandoffQueue, Lead, Message
from mawjood.db.repositories.core import TenantRepository
from mawjood.observability.logging import get_logger

router = APIRouter(prefix="/console", tags=["console"])
log = get_logger(__name__)
security = HTTPBasic(auto_error=False)

TEMPLATES = Jinja2Templates(directory=str(Path(__file__).parent / "templates"))


async def operator(
    request: Request,
    credentials: Annotated[HTTPBasicCredentials | None, Depends(security)],
) -> str:
    """Authenticate an operator, or refuse to serve.

    An unauthenticated console exposes every consumer's transcript, so a missing
    credential is a 503 rather than an open door.
    """
    settings: Settings = request.app.state.settings
    if settings.console_password is None:
        log.error("console.no_credential_configured")
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="console credentials are not configured",
        )
    if credentials is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="authentication required",
            headers={"WWW-Authenticate": "Basic"},
        )

    # Constant-time on both halves: a username check that short-circuits leaks
    # which half was wrong.
    expected_user = settings.console_username
    expected_password = settings.console_password.get_secret_value()
    user_ok = secrets.compare_digest(credentials.username, expected_user)
    password_ok = secrets.compare_digest(credentials.password, expected_password)
    if not (user_ok and password_ok):
        log.warning("console.authentication_failed", username=credentials.username)
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="invalid credentials",
            headers={"WWW-Authenticate": "Basic"},
        )
    return credentials.username


async def _tenant_id(request: Request, session: Any) -> uuid.UUID:
    settings: Settings = request.app.state.settings
    tenant = await TenantRepository(session).get_by_slug(settings.default_tenant_slug)
    if tenant is None:
        raise HTTPException(status_code=404, detail="tenant not configured")
    return tenant.id


@router.get("", response_class=HTMLResponse)
async def dashboard(request: Request, who: Annotated[str, Depends(operator)]) -> HTMLResponse:
    """Counts, and the queue that needs attention."""
    factory = request.app.state.session_factory
    async with factory() as session:
        tenant_id = await _tenant_id(request, session)

        async def count(model: Any, *filters: Any) -> int:
            statement = select(func.count()).select_from(model).where(model.tenant_id == tenant_id)
            for condition in filters:
                statement = statement.where(condition)
            return int((await session.execute(statement)).scalar_one())

        stats = {
            "leads": await count(Lead),
            "conversations": await count(Conversation),
            "bookings": await count(Booking),
            "waiting": await count(HandoffQueue, HandoffQueue.status != "resolved"),
        }
        queue = await open_queue(session, tenant_id)
        leads = {
            row.id: row
            for row in (
                await session.execute(select(Lead).where(Lead.tenant_id == tenant_id))
            ).scalars()
        }

    return TEMPLATES.TemplateResponse(
        request,
        "dashboard.html",
        {"stats": stats, "queue": queue, "leads": leads, "operator": who},
    )


@router.get("/conversations/{conversation_id}", response_class=HTMLResponse)
async def conversation_detail(
    request: Request, conversation_id: uuid.UUID, who: Annotated[str, Depends(operator)]
) -> HTMLResponse:
    """Transcript plus the routing audit trail that produced it."""
    factory = request.app.state.session_factory
    async with factory() as session:
        tenant_id = await _tenant_id(request, session)

        conversation = (
            await session.execute(
                select(Conversation)
                .where(Conversation.tenant_id == tenant_id)
                .where(Conversation.id == conversation_id)
            )
        ).scalar_one_or_none()
        if conversation is None:
            raise HTTPException(status_code=404, detail="conversation not found")

        lead = (
            await session.execute(select(Lead).where(Lead.id == conversation.lead_id))
        ).scalar_one_or_none()

        messages = list(
            (
                await session.execute(
                    select(Message)
                    .where(Message.conversation_id == conversation_id)
                    .order_by(Message.seq)
                )
            ).scalars()
        )
        trail = list(
            (
                await session.execute(
                    select(AuditLog)
                    .where(AuditLog.conversation_id == conversation_id)
                    .order_by(AuditLog.seq)
                )
            ).scalars()
        )
        handoff = (
            await session.execute(
                select(HandoffQueue)
                .where(HandoffQueue.conversation_id == conversation_id)
                .where(HandoffQueue.status != "resolved")
                .limit(1)
            )
        ).scalar_one_or_none()

    return TEMPLATES.TemplateResponse(
        request,
        "conversation.html",
        {
            "conversation": conversation,
            "lead": lead,
            "messages": messages,
            "trail": trail,
            "handoff": handoff,
            "operator": who,
            "Direction": Direction,
        },
    )


@router.post("/handoffs/{handoff_id}/claim")
async def claim_handoff(
    request: Request, handoff_id: uuid.UUID, who: Annotated[str, Depends(operator)]
) -> RedirectResponse:
    factory = request.app.state.session_factory
    async with factory() as session:
        tenant_id = await _tenant_id(request, session)
        try:
            view = await claim(session, tenant_id=tenant_id, handoff_id=handoff_id, operator=who)
        except HandoffNotFound as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        except HandoffAlreadyClaimed as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        await session.commit()
        target = view.conversation.id
    return RedirectResponse(f"/console/conversations/{target}", status_code=303)


@router.post("/handoffs/{handoff_id}/reply")
async def reply_to_consumer(
    request: Request,
    handoff_id: uuid.UUID,
    who: Annotated[str, Depends(operator)],
    text: Annotated[str, Form()],
) -> RedirectResponse:
    """The one write path a consumer ever sees."""
    factory = request.app.state.session_factory
    async with factory() as session:
        tenant_id = await _tenant_id(request, session)
        try:
            message = await reply(
                session, tenant_id=tenant_id, handoff_id=handoff_id, operator=who, text=text
            )
        except HandoffNotFound as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        except HandoffAlreadyClaimed as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        await session.commit()
        target = message.conversation_id
    return RedirectResponse(f"/console/conversations/{target}", status_code=303)


@router.post("/handoffs/{handoff_id}/release")
async def release_handoff(
    request: Request,
    handoff_id: uuid.UUID,
    who: Annotated[str, Depends(operator)],
    notes: Annotated[str | None, Form()] = None,
) -> RedirectResponse:
    """Give the conversation back to Mawjood and unmute it."""
    factory = request.app.state.session_factory
    async with factory() as session:
        tenant_id = await _tenant_id(request, session)
        try:
            view = await release(
                session,
                tenant_id=tenant_id,
                handoff_id=handoff_id,
                operator=who,
                notes=notes,
            )
        except HandoffNotFound as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        await session.commit()
        target = view.conversation.id
    return RedirectResponse(f"/console/conversations/{target}", status_code=303)


__all__ = ["router"]
