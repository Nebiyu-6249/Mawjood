"""Human handoff: claim, reply, release.

Decision 7: **queue + console reply + bot mute.** When the cascade is exhausted,
an action is unsupported, or a consumer asks for a person, the conversation goes
to a queue, the assistant goes silent on that thread, and an operator picks it up
from the console.

The half that Phase 2 left missing is here: **release**. A conversation that mutes
with no way to unmute is a consumer permanently talking to nobody — worse than
never having handed off, because the queue row makes it look handled.

Three operations, each one transactional and each one audited:

* :func:`claim`   — an operator takes ownership. Idempotent per operator.
* :func:`reply`   — the operator's words go out, recorded like any other message.
* :func:`release` — ownership ends, the assistant resumes, the queue row closes.

An operator reply is the one consumer-facing text that does **not** come from the
phrasebank, because a human wrote it. That is a deliberate exception and it is
narrow: it is attributable to a named operator, recorded verbatim, and the
assistant is silent while it happens. The blocklist governs what *Mawjood* says,
not what a colleague says.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from mawjood.core.enums import (
    Actor,
    ConversationStatus,
    Direction,
    HandoffStatus,
    MessageStatus,
)
from mawjood.db.models import Conversation, HandoffQueue, Message
from mawjood.observability.audit import AuditTrail
from mawjood.observability.logging import get_logger

log = get_logger(__name__)


class HandoffNotFound(LookupError):
    """No such handoff for this tenant."""


class HandoffAlreadyClaimed(RuntimeError):
    """Someone else owns this conversation.

    Refused rather than silently reassigned: two operators replying to one
    consumer is worse than one operator waiting.
    """


@dataclass(frozen=True, slots=True)
class HandoffView:
    """A queue row plus the conversation it belongs to."""

    handoff: HandoffQueue
    conversation: Conversation


async def _load(session: AsyncSession, tenant_id: uuid.UUID, handoff_id: uuid.UUID) -> HandoffView:
    result = await session.execute(
        select(HandoffQueue, Conversation)
        .join(Conversation, Conversation.id == HandoffQueue.conversation_id)
        .where(HandoffQueue.tenant_id == tenant_id)
        .where(HandoffQueue.id == handoff_id)
    )
    row = result.one_or_none()
    if row is None:
        raise HandoffNotFound(f"no handoff {handoff_id} for tenant {tenant_id}")
    return HandoffView(handoff=row[0], conversation=row[1])


async def claim(
    session: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    handoff_id: uuid.UUID,
    operator: str,
    correlation_id: str = "console",
) -> HandoffView:
    """Take ownership of a conversation."""
    view = await _load(session, tenant_id, handoff_id)
    handoff = view.handoff

    if handoff.status is HandoffStatus.RESOLVED:
        raise HandoffAlreadyClaimed("this handoff is already resolved")
    if handoff.claimed_by and handoff.claimed_by != operator:
        raise HandoffAlreadyClaimed(
            f"already claimed by {handoff.claimed_by}; two operators replying to "
            "one consumer is worse than one waiting"
        )

    handoff.status = HandoffStatus.CLAIMED
    handoff.claimed_by = operator
    handoff.claimed_at = datetime.now(UTC)
    view.conversation.status = ConversationStatus.HANDED_OFF
    # Belt and braces: enqueueing mutes, but a claim must never leave it unmuted.
    view.conversation.bot_muted = True

    audit = AuditTrail(session=session, tenant_id=tenant_id, correlation_id=correlation_id)
    audit.bind(conversation_id=view.conversation.id, lead_id=handoff.lead_id)
    await audit.record(
        "handoff.claimed",
        actor=Actor.OPERATOR,
        details={"operator": operator, "handoff_id": str(handoff_id)},
    )
    await session.flush()
    log.info("handoff.claimed", handoff_id=str(handoff_id), operator=operator)
    return view


async def reply(
    session: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    handoff_id: uuid.UUID,
    operator: str,
    text: str,
    correlation_id: str = "console",
) -> Message:
    """Send an operator's message to the consumer.

    Recorded as an outbound message like any other, so the transcript stays
    complete and the ops console shows one continuous thread rather than two
    half-conversations.

    ``phrasebank_key`` is set to a sentinel rather than a real key: a database
    CHECK requires outbound messages to name one, and this text genuinely did not
    come from the phrasebank. Naming it explicitly means an audit can tell an
    operator's words from Mawjood's at a glance.
    """
    view = await _load(session, tenant_id, handoff_id)
    if view.handoff.claimed_by != operator:
        raise HandoffAlreadyClaimed(
            "claim the conversation before replying, so the queue shows who is on it"
        )

    body = text.strip()
    if not body:
        raise ValueError("an operator reply cannot be empty")

    turn_id = uuid.uuid4()
    message = Message(
        tenant_id=tenant_id,
        conversation_id=view.conversation.id,
        lead_id=view.handoff.lead_id,
        turn_id=turn_id,
        direction=Direction.OUTBOUND,
        channel=view.conversation.channel,
        status=MessageStatus.QUEUED,
        body=body,
        locale=view.conversation.locale,
        # Not phrasebank copy. A human wrote it, and the record says so.
        phrasebank_key="operator.reply",
    )
    session.add(message)
    view.conversation.last_activity_at = datetime.now(UTC)

    audit = AuditTrail(
        session=session,
        tenant_id=tenant_id,
        correlation_id=correlation_id,
        turn_id=turn_id,
    )
    audit.bind(conversation_id=view.conversation.id, lead_id=view.handoff.lead_id)
    await audit.record(
        "handoff.operator_replied",
        actor=Actor.OPERATOR,
        consumer_visible=True,
        phrasebank_key="operator.reply",
        locale=view.conversation.locale,
        consumer_text=body,
        details={"operator": operator, "handoff_id": str(handoff_id)},
    )
    await session.flush()
    log.info("handoff.operator_replied", handoff_id=str(handoff_id), operator=operator)
    return message


async def release(
    session: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    handoff_id: uuid.UUID,
    operator: str,
    notes: str | None = None,
    correlation_id: str = "console",
) -> HandoffView:
    """Hand the conversation back to Mawjood.

    The missing half of decision 7. Clears the mute, reopens the conversation, and
    closes the queue row. Without this a handed-off consumer talks to nobody
    forever, and the queue row makes that look like a resolved case.
    """
    view = await _load(session, tenant_id, handoff_id)
    handoff = view.handoff

    handoff.status = HandoffStatus.RESOLVED
    handoff.resolved_at = datetime.now(UTC)
    if notes:
        handoff.notes = notes

    view.conversation.bot_muted = False
    view.conversation.status = ConversationStatus.ACTIVE
    view.conversation.last_activity_at = datetime.now(UTC)

    audit = AuditTrail(session=session, tenant_id=tenant_id, correlation_id=correlation_id)
    audit.bind(conversation_id=view.conversation.id, lead_id=handoff.lead_id)
    await audit.record(
        "handoff.resolved",
        actor=Actor.OPERATOR,
        details={"operator": operator, "handoff_id": str(handoff_id), "notes": notes},
    )
    await session.flush()
    log.info("handoff.released", handoff_id=str(handoff_id), operator=operator)
    return view


async def open_queue(
    session: AsyncSession, tenant_id: uuid.UUID, limit: int = 50
) -> list[HandoffView]:
    """Everything waiting for a person, oldest first.

    Oldest first on purpose: a queue sorted by priority alone starves the
    ordinary consumer who has been waiting longest.
    """
    result = await session.execute(
        select(HandoffQueue, Conversation)
        .join(Conversation, Conversation.id == HandoffQueue.conversation_id)
        .where(HandoffQueue.tenant_id == tenant_id)
        .where(HandoffQueue.status != HandoffStatus.RESOLVED)
        .order_by(HandoffQueue.priority.desc(), HandoffQueue.created_at)
        .limit(limit)
    )
    return [HandoffView(handoff=row[0], conversation=row[1]) for row in result.all()]


__all__ = [
    "HandoffAlreadyClaimed",
    "HandoffNotFound",
    "HandoffView",
    "claim",
    "open_queue",
    "release",
    "reply",
]
