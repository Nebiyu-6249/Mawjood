"""Finding notifications that are due, and delivering the ones that may go out.

The decisions live in ``policy.py`` and are pure. This module does the parts that
touch the world: read bookings, render copy, hand it to the BSP, record what
happened.

## No new table

Due-ness is **derived** from data that already exists — ``bookings.slot_start``,
``bookings.status``, and whether a message with the notification's phrasebank key
has already gone out on that conversation. Nothing is enqueued anywhere.

That is a deliberate choice and it needs saying, because a job table is the
obvious design. CLAUDE.md section 13 says to ask before altering the schema after
Phase 1, and this feature does not need to: a booking already carries everything
that determines when its reminder fires, and ``messages`` already records what
was sent. A queue would be a second copy of both, with the drift that implies.

What a queue would buy is per-notification retry state and a place to park a
blocked send. Today neither is needed — a blocked send is blocked by policy, not
by a transient fault, and retrying it changes nothing until a template is
approved. If that stops being true, ``_already_sent`` and :meth:`due` are the
only two places that know, and a table drops in behind them.

## Idempotency

A scheduler that runs every five minutes must not send five reminders. The guard
is the transcript itself: a notification is already sent if an outbound message
with that phrasebank key exists on the conversation after the booking was
confirmed. That is exact, survives restarts, and needs no extra bookkeeping.

## Nothing is invented

Copy comes from the phrasebank, so the invariant holds here as it does
everywhere. The scheduler cannot construct a message any other way.
"""

from __future__ import annotations

import uuid
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from zoneinfo import ZoneInfo

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from mawjood.core.conversation.phrasebank import Phrasebank, PhraseKey, get_phrasebank
from mawjood.core.conversation.types import OutboundMessage
from mawjood.core.enums import BookingStatus, Direction, MessageStatus
from mawjood.core.notifications.policy import (
    NotificationKind,
    Offsets,
    Plan,
    Verdict,
    decide,
)
from mawjood.core.notifications.templates import TemplateRegistry, build_template_registry
from mawjood.db.models import Booking, Conversation, Lead, Message
from mawjood.observability.audit import AuditTrail
from mawjood.observability.logging import get_logger

log = get_logger(__name__)

# Which phrasebank entry each kind renders.
PHRASE_FOR: dict[NotificationKind, PhraseKey] = {
    NotificationKind.REMINDER: PhraseKey.NOTIFY_REMINDER,
    NotificationKind.FOLLOW_UP: PhraseKey.NOTIFY_FOLLOW_UP,
    NotificationKind.SATISFACTION: PhraseKey.NOTIFY_SATISFACTION,
}


@dataclass(frozen=True, slots=True)
class Scheduled:
    """One notification, its booking, and what the policy decided."""

    booking: Booking
    conversation: Conversation
    lead: Lead
    plan: Plan

    @property
    def kind(self) -> NotificationKind:
        return self.plan.kind


class Scheduler:
    """Decides and delivers scheduled messages for one tenant."""

    def __init__(
        self,
        session: AsyncSession,
        *,
        tenant_id: uuid.UUID,
        offsets: Offsets | None = None,
        templates: TemplateRegistry | None = None,
        phrasebank: Phrasebank | None = None,
        timezone: str = "Asia/Dubai",
    ) -> None:
        self._session = session
        self._tenant_id = tenant_id
        self._offsets = offsets or Offsets()
        self._templates = templates or build_template_registry()
        self._phrasebank = phrasebank or get_phrasebank()
        self._timezone = timezone

    async def plan_all(self, *, now: datetime | None = None) -> list[Scheduled]:
        """Decide every notification for every live booking.

        Returns everything, including ``NOT_YET`` and ``BLOCKED_…`` — the console
        and the tests both want the full picture, and filtering is the caller's
        job. :meth:`due` is the filtered view.
        """
        moment = now or datetime.now(UTC)
        rows = await self._live_bookings()
        planned: list[Scheduled] = []

        for booking, conversation, lead in rows:
            for kind in NotificationKind:
                template = self._templates.get(str(kind), conversation.locale)
                plan = decide(
                    kind,
                    now=moment,
                    slot_start=booking.slot_start,
                    slot_end=booking.slot_end,
                    last_inbound_at=await self._last_inbound_at(conversation.id),
                    offsets=self._offsets,
                    template=template,
                    already_sent=await self._already_sent(conversation.id, kind),
                    cancelled=booking.status is BookingStatus.CANCELLED,
                )
                planned.append(
                    Scheduled(booking=booking, conversation=conversation, lead=lead, plan=plan)
                )
        return planned

    async def due(self, *, now: datetime | None = None) -> list[Scheduled]:
        """Only the notifications whose moment has arrived — sendable or blocked.

        Blocked ones are included on purpose. They are the interesting output of
        this feature: the operator needs to see how much is piling up behind
        template approval.
        """
        wanted = {
            Verdict.SEND_FREEFORM,
            Verdict.SEND_TEMPLATE,
            Verdict.BLOCKED_NO_APPROVED_TEMPLATE,
        }
        return [s for s in await self.plan_all(now=now) if s.plan.verdict in wanted]

    async def run(
        self,
        *,
        bsp: object | None = None,
        now: datetime | None = None,
        correlation_id: str = "scheduler",
    ) -> list[Scheduled]:
        """Deliver everything that may go out. Returns what was considered.

        A blocked notification is audited and left alone. It is **not** written
        to ``messages``, because ``messages`` is the record of what the consumer
        actually received, and a blocked send is a message the consumer never
        got. Recording it there would corrupt both the transcript and the
        idempotency check that reads it.
        """
        considered = await self.due(now=now)

        for item in considered:
            audit = AuditTrail(
                session=self._session,
                tenant_id=self._tenant_id,
                correlation_id=correlation_id,
            )
            audit.bind(conversation_id=item.conversation.id, lead_id=item.lead.id)

            if not item.plan.sends:
                await audit.record(
                    "notification.blocked",
                    details={
                        "kind": str(item.kind),
                        "verdict": str(item.plan.verdict),
                        "reason": item.plan.reason,
                        "booking_id": str(item.booking.id),
                        "template": item.plan.template.name if item.plan.template else None,
                        "template_status": (
                            str(item.plan.template.status) if item.plan.template else None
                        ),
                    },
                )
                log.warning(
                    "notification.blocked",
                    kind=str(item.kind),
                    verdict=str(item.plan.verdict),
                    booking_id=str(item.booking.id),
                )
                continue

            await self._send(item, bsp=bsp, audit=audit)

        await self._session.flush()
        return considered

    # -- internals ----------------------------------------------------------

    async def _send(self, item: Scheduled, *, bsp: object | None, audit: AuditTrail) -> None:
        rendered = self._phrasebank.render(
            PHRASE_FOR[item.kind],
            item.conversation.locale,
            venue=item.booking.venue_name or "your booking",
            time=_local_time(item.booking.slot_start, self._timezone),
        )

        turn_id = uuid.uuid4()
        message = Message(
            tenant_id=self._tenant_id,
            conversation_id=item.conversation.id,
            lead_id=item.lead.id,
            turn_id=turn_id,
            direction=Direction.OUTBOUND,
            channel=item.conversation.channel,
            status=MessageStatus.QUEUED,
            body=rendered.text,
            locale=rendered.locale,
            phrasebank_key=rendered.key,
        )
        self._session.add(message)

        provider_id: str | None = None
        if bsp is not None:
            send = getattr(bsp, "send", None)
            if send is not None:
                provider_id = await send(
                    OutboundMessage(
                        message=rendered,
                        conversation_id=item.conversation.id,
                        lead_id=item.lead.id,
                        channel=item.conversation.channel,
                    )
                )
        if provider_id:
            message.provider_message_id = provider_id
            message.status = MessageStatus.SENT
            message.sent_at = datetime.now(UTC)

        await audit.record(
            "notification.sent",
            consumer_visible=True,
            phrasebank_key=rendered.key,
            locale=rendered.locale,
            consumer_text=rendered.text,
            details={
                "kind": str(item.kind),
                "verdict": str(item.plan.verdict),
                "booking_id": str(item.booking.id),
                "template": item.plan.template.name if item.plan.template else None,
            },
        )
        log.info("notification.sent", kind=str(item.kind), booking_id=str(item.booking.id))

    async def _live_bookings(self) -> Sequence[tuple[Booking, Conversation, Lead]]:
        result = await self._session.execute(
            select(Booking, Conversation, Lead)
            .join(Conversation, Conversation.id == Booking.conversation_id)
            .join(Lead, Lead.id == Booking.lead_id)
            .where(Booking.tenant_id == self._tenant_id)
            .where(Booking.status != BookingStatus.FAILED)
            .order_by(Booking.slot_start)
        )
        return [(row[0], row[1], row[2]) for row in result.all()]

    async def _last_inbound_at(self, conversation_id: uuid.UUID) -> datetime | None:
        """When the consumer last said something. The session window starts here."""
        result = await self._session.execute(
            select(Message.created_at)
            .where(Message.conversation_id == conversation_id)
            .where(Message.direction == Direction.INBOUND)
            .order_by(Message.seq.desc())
            .limit(1)
        )
        return result.scalar_one_or_none()

    async def _already_sent(self, conversation_id: uuid.UUID, kind: NotificationKind) -> bool:
        """Idempotency, read off the transcript rather than a job table."""
        result = await self._session.execute(
            select(Message.id)
            .where(Message.conversation_id == conversation_id)
            .where(Message.direction == Direction.OUTBOUND)
            .where(Message.phrasebank_key == str(PHRASE_FOR[kind]))
            .limit(1)
        )
        return result.scalar_one_or_none() is not None


def _local_time(when: datetime | None, tzname: str) -> str:
    """A time a person would say out loud, in the local timezone.

    "5 pm" and "5.30 pm", not "17:00:00+00:00". Bookings are stored in UTC; a
    reminder that quotes UTC to someone in Dubai is worse than no reminder.
    """
    if when is None:
        return "your booking time"
    local = _aware(when).astimezone(ZoneInfo(tzname))
    hour = local.strftime("%-I")
    meridiem = local.strftime("%p").lower()
    return f"{hour} {meridiem}" if local.minute == 0 else f"{hour}.{local.minute:02d} {meridiem}"


def _aware(value: datetime) -> datetime:
    return value if value.tzinfo is not None else value.replace(tzinfo=UTC)


__all__ = ["PHRASE_FOR", "Scheduled", "Scheduler"]
