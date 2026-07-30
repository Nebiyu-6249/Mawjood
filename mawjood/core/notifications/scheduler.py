"""Writing the schedule, running it, and capturing what comes back.

Three jobs, in the order they happen:

* :func:`schedule_for_booking` — the moment a booking is confirmed, write one row
  per notification kind. Rows, not timers: an in-process timer dies with the
  process and nothing afterwards knows the message was owed.
* :meth:`Scheduler.run` — every pass, take what is due, decide how to send it,
  send it, and record the outcome on the row.
* :func:`capture_satisfaction` — a consumer's "4, staff were lovely" becomes a
  rating and a comment.

## Idempotency

``(booking_id, kind)`` is unique in the schema. A second reminder for the same
booking cannot be inserted, so double-reminding is impossible even if two
scheduler passes race — the database refuses, rather than a query the caller has
to remember to run first.

Sending is guarded separately: a row moves to ``SENT`` in the same transaction as
the message it produced, so a crash between the two cannot leave a consumer
messaged with the row still pending.

## Nothing is invented

Copy comes from the phrasebank. The scheduler cannot construct a consumer-facing
message any other way, so the invariant holds here as everywhere else.
"""

from __future__ import annotations

import re
import uuid
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from zoneinfo import ZoneInfo

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from mawjood.core.conversation.phrasebank import Phrasebank, get_phrasebank
from mawjood.core.conversation.types import OutboundMessage
from mawjood.core.enums import (
    BookingStatus,
    Direction,
    MessageStatus,
    NotificationKind,
    NotificationStatus,
)
from mawjood.core.notifications.policy import Offsets, Plan, Verdict, decide, due_at
from mawjood.core.notifications.templates import (
    PHRASE_FOR,
    TemplateRegistry,
    build_template_registry,
)
from mawjood.db.models import (
    Booking,
    Conversation,
    Feedback,
    Lead,
    Message,
    ScheduledNotification,
)
from mawjood.observability.audit import AuditTrail
from mawjood.observability.logging import get_logger

log = get_logger(__name__)


@dataclass(frozen=True, slots=True)
class Due:
    """A scheduled row, its booking, and what the policy decided."""

    row: ScheduledNotification
    booking: Booking
    conversation: Conversation
    lead: Lead
    plan: Plan


async def schedule_for_booking(
    session: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    booking: Booking,
    offsets: Offsets | None = None,
) -> list[ScheduledNotification]:
    """Write the schedule for one confirmed booking. Idempotent.

    Called once, when the booking is created. Re-running it is safe: the unique
    constraint on ``(booking_id, kind)`` makes a duplicate impossible, and this
    checks first so the common case does not burn a savepoint.

    A booking with no slot time schedules nothing — there is no anchor to
    measure from, and inventing one would send a reminder at a moment nobody
    chose.
    """
    offsets = offsets or Offsets()
    if booking.slot_start is None:
        log.info("notification.not_schedulable", booking_id=str(booking.id))
        return []

    existing = {
        row.kind
        for row in (
            await session.execute(
                select(ScheduledNotification).where(ScheduledNotification.booking_id == booking.id)
            )
        )
        .scalars()
        .all()
    }

    created: list[ScheduledNotification] = []
    for kind in NotificationKind:
        if kind in existing:
            continue
        when = due_at(
            kind, slot_start=booking.slot_start, slot_end=booking.slot_end, offsets=offsets
        )
        if when is None:
            continue
        row = ScheduledNotification(
            tenant_id=tenant_id,
            booking_id=booking.id,
            conversation_id=booking.conversation_id,
            lead_id=booking.lead_id,
            kind=kind,
            status=NotificationStatus.PENDING,
            due_at=when,
        )
        session.add(row)
        created.append(row)

    try:
        await session.flush()
    except IntegrityError:
        # Another pass got there first. The constraint did its job; that is a
        # success, not a failure.
        await session.rollback()
        log.info("notification.schedule_raced", booking_id=str(booking.id))
        return []

    log.info(
        "notification.scheduled",
        booking_id=str(booking.id),
        kinds=[str(row.kind) for row in created],
    )
    return created


class Scheduler:
    """Runs the schedule for one tenant."""

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

    async def pending(self, *, now: datetime | None = None) -> list[Due]:
        """Everything not yet terminal, decided against the clock.

        Includes ``NOT_YET`` so the console can show what is coming. :meth:`due`
        is the filtered view the runner uses.
        """
        moment = now or datetime.now(UTC)
        rows = await self._load_open()
        decided: list[Due] = []

        for row, booking, conversation, lead in rows:
            plan = decide(
                row.kind,
                now=moment,
                due=row.due_at,
                last_inbound_at=await self._last_inbound_at(conversation.id),
                offsets=self._offsets,
                template=self._templates.get(row.kind, conversation.locale),
                cancelled=booking.status is BookingStatus.CANCELLED,
            )
            decided.append(
                Due(row=row, booking=booking, conversation=conversation, lead=lead, plan=plan)
            )
        return decided

    async def due(self, *, now: datetime | None = None) -> list[Due]:
        """Only what needs acting on this pass: sendable, deferred, or terminal."""
        return [d for d in await self.pending(now=now) if d.plan.verdict is not Verdict.NOT_YET]

    async def run(
        self,
        *,
        bsp: object | None = None,
        now: datetime | None = None,
        correlation_id: str = "scheduler",
    ) -> list[Due]:
        """Send what may be sent; record what may not. Returns what was acted on.

        A deferred notification writes **no** row to ``messages``. That table is
        the record of what a consumer received, and a deferred message is one
        they did not — recording it there would corrupt both the transcript and
        the delivery statistics read off it.
        """
        acted = await self.due(now=now)
        moment = now or datetime.now(UTC)

        for item in acted:
            audit = AuditTrail(
                session=self._session,
                tenant_id=self._tenant_id,
                correlation_id=correlation_id,
            )
            audit.bind(conversation_id=item.conversation.id, lead_id=item.lead.id)
            item.row.attempts += 1
            item.row.last_attempt_at = moment
            item.row.reason = item.plan.reason

            if item.plan.verdict.is_terminal:
                item.row.status = NotificationStatus.SKIPPED
                await audit.record(
                    "notification.skipped",
                    details={
                        "kind": str(item.row.kind),
                        "verdict": str(item.plan.verdict),
                        "reason": item.plan.reason,
                        "booking_id": str(item.booking.id),
                    },
                )
                continue

            if not item.plan.sends:
                item.row.status = NotificationStatus.DEFERRED
                item.row.template_name = item.plan.template.name if item.plan.template else None
                await audit.record(
                    "notification.deferred",
                    details={
                        "kind": str(item.row.kind),
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
                    "notification.deferred",
                    kind=str(item.row.kind),
                    booking_id=str(item.booking.id),
                )
                continue

            await self._send(item, bsp=bsp, audit=audit, now=moment)

        await self._session.flush()
        return acted

    # -- internals ----------------------------------------------------------

    async def _send(
        self, item: Due, *, bsp: object | None, audit: AuditTrail, now: datetime
    ) -> None:
        rendered = self._phrasebank.render(
            PHRASE_FOR[item.row.kind],
            item.conversation.locale,
            venue=item.booking.venue_name or "your booking",
            day=_local_day(item.booking.slot_start, self._timezone),
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
        await self._session.flush()

        provider_id: str | None = None
        if bsp is not None and (send := getattr(bsp, "send", None)) is not None:
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
            message.sent_at = now

        # The row and the message move together. A crash between them would
        # otherwise leave a consumer messaged and the row still pending, which is
        # how someone gets reminded twice.
        item.row.status = NotificationStatus.SENT
        item.row.sent_at = now
        item.row.message_id = message.id
        item.row.template_name = item.plan.template.name if item.plan.template else None

        await audit.record(
            "notification.sent",
            consumer_visible=True,
            phrasebank_key=rendered.key,
            locale=rendered.locale,
            consumer_text=rendered.text,
            details={
                "kind": str(item.row.kind),
                "verdict": str(item.plan.verdict),
                "booking_id": str(item.booking.id),
                "template": item.row.template_name,
            },
        )
        log.info(
            "notification.sent",
            kind=str(item.row.kind),
            verdict=str(item.plan.verdict),
            booking_id=str(item.booking.id),
        )

    async def _load_open(
        self,
    ) -> Sequence[tuple[ScheduledNotification, Booking, Conversation, Lead]]:
        result = await self._session.execute(
            select(ScheduledNotification, Booking, Conversation, Lead)
            .join(Booking, Booking.id == ScheduledNotification.booking_id)
            .join(Conversation, Conversation.id == ScheduledNotification.conversation_id)
            .join(Lead, Lead.id == ScheduledNotification.lead_id)
            .where(ScheduledNotification.tenant_id == self._tenant_id)
            .where(
                ScheduledNotification.status.in_(
                    (NotificationStatus.PENDING, NotificationStatus.DEFERRED)
                )
            )
            .order_by(ScheduledNotification.due_at)
        )
        return [(row[0], row[1], row[2], row[3]) for row in result.all()]

    async def _last_inbound_at(self, conversation_id: uuid.UUID) -> datetime | None:
        """When the consumer last spoke. The session window starts here."""
        result = await self._session.execute(
            select(Message.created_at)
            .where(Message.conversation_id == conversation_id)
            .where(Message.direction == Direction.INBOUND)
            .order_by(Message.seq.desc())
            .limit(1)
        )
        return result.scalar_one_or_none()


# ---------------------------------------------------------------------------
# Satisfaction capture
# ---------------------------------------------------------------------------

# A rating on its own line, or leading a sentence. Deliberately narrow: "I'd give
# it a 4" matches, "book me 5 people" does not, because the second is a request
# and mis-reading it as feedback would drop a real booking on the floor.
_RATING = re.compile(
    r"(?:^|\b)(?:([1-5])\s*(?:/|out of)\s*5|(?:rate|rating|give it|gave it)?\s*\b([1-5])\b)",
    re.IGNORECASE,
)


@dataclass(frozen=True, slots=True)
class Satisfaction:
    """A parsed rating and whatever else the consumer said."""

    rating: int | None
    comment: str | None

    @property
    def is_feedback(self) -> bool:
        return self.rating is not None


def parse_satisfaction(text: str) -> Satisfaction:
    """Pull a 1-5 rating and a free-text comment out of a reply.

    Both halves matter and they are separated on purpose. The rating is the
    number an operator can average; the comment is the sentence that tells them
    *why*, and it is the part a 1-to-5 scale throws away. "2 — waited 40 minutes"
    is worth more than the 2.

    Returns ``rating=None`` when nothing looks like a score, so the caller can
    treat the message as ordinary conversation rather than silently scoring it.
    """
    body = (text or "").strip()
    if not body:
        return Satisfaction(None, None)

    match = _RATING.search(body)
    if match is None:
        return Satisfaction(None, body or None)

    rating = int(match.group(1) or match.group(2))
    # Whatever is left once the score is removed. Punctuation and connectives at
    # the edges go too, so "4 - staff were lovely" comments as "staff were lovely".
    remainder = (body[: match.start()] + " " + body[match.end() :]).strip()
    remainder = re.sub(r"^[\s,.\-—:;]+|[\s,.\-—:;]+$", "", remainder)
    remainder = re.sub(r"^(?:out of 5|/5)\b", "", remainder, flags=re.IGNORECASE).strip()
    return Satisfaction(rating, remainder or None)


async def capture_satisfaction(
    session: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    conversation_id: uuid.UUID,
    lead_id: uuid.UUID,
    text: str,
    audit: AuditTrail | None = None,
) -> Feedback | None:
    """Record a consumer's rating against their most recent booking.

    Returns None when the message carries no rating, so the pipeline can carry on
    treating it as ordinary conversation. Scoring an ambiguous message would put
    noise into the one number an operator trusts.
    """
    parsed = parse_satisfaction(text)
    if not parsed.is_feedback:
        return None

    booking = (
        await session.execute(
            select(Booking)
            .where(Booking.tenant_id == tenant_id)
            .where(Booking.conversation_id == conversation_id)
            .order_by(Booking.created_at.desc())
            .limit(1)
        )
    ).scalar_one_or_none()

    existing = (
        await session.execute(
            select(Feedback)
            .where(Feedback.tenant_id == tenant_id)
            .where(Feedback.conversation_id == conversation_id)
            .limit(1)
        )
    ).scalar_one_or_none()

    now = datetime.now(UTC)
    if existing is not None:
        # A consumer correcting themselves ("actually make that a 5") should
        # update, not append. Two rows for one experience would double-count.
        existing.rating = parsed.rating
        if parsed.comment:
            existing.comment = parsed.comment
        existing.responded_at = now
        feedback = existing
    else:
        feedback = Feedback(
            tenant_id=tenant_id,
            conversation_id=conversation_id,
            lead_id=lead_id,
            booking_id=booking.id if booking else None,
            rating=parsed.rating,
            comment=parsed.comment,
            responded_at=now,
        )
        session.add(feedback)

    await session.flush()

    if audit is not None:
        await audit.record(
            "feedback.captured",
            details={
                "rating": parsed.rating,
                "has_comment": bool(parsed.comment),
                "booking_id": str(booking.id) if booking else None,
            },
        )
    log.info("feedback.captured", rating=parsed.rating, has_comment=bool(parsed.comment))
    return feedback


# ---------------------------------------------------------------------------
# Formatting
# ---------------------------------------------------------------------------


def _local_time(when: datetime | None, tzname: str) -> str:
    """A time a person would say out loud. "5 pm", not "13:00:00+00:00"."""
    if when is None:
        return "your booking time"
    local = _aware(when).astimezone(ZoneInfo(tzname))
    hour = local.strftime("%-I")
    meridiem = local.strftime("%p").lower()
    return f"{hour} {meridiem}" if local.minute == 0 else f"{hour}.{local.minute:02d} {meridiem}"


def _local_day(when: datetime | None, tzname: str) -> str:
    """ "tomorrow" where that is true, otherwise a weekday and date."""
    if when is None:
        return "soon"
    local = _aware(when).astimezone(ZoneInfo(tzname))
    today = datetime.now(UTC).astimezone(ZoneInfo(tzname)).date()
    delta = (local.date() - today).days
    if delta == 0:
        return "today"
    if delta == 1:
        return "tomorrow"
    return local.strftime("%A %-d %B")


def _aware(value: datetime) -> datetime:
    return value if value.tzinfo is not None else value.replace(tzinfo=UTC)


__all__ = [
    "Due",
    "Satisfaction",
    "Scheduler",
    "capture_satisfaction",
    "parse_satisfaction",
    "schedule_for_booking",
]
