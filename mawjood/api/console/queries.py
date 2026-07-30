"""Every read the console performs, in one place.

Separated from the routes because these are the interesting part and they should
be testable without an HTTP client. A route here is four lines: authenticate,
call one of these, render.

Everything is tenant-scoped. Not because v1 has two tenants — it has one — but
because the day a second exists is not the day anyone wants to find out the
console was querying unscoped.

**Read-only.** Nothing in this module writes. There is a test that walks the AST
of the whole console package and fails on any INSERT, UPDATE, DELETE, commit, or
session.add. "Read-only" that depends on nobody adding a write later is a
convention; this is a property.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any, Literal

from sqlalchemy import Float, and_, cast, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from mawjood.core.enums import BookingStatus, Direction, HandoffStatus, NotificationStatus
from mawjood.db.models import (
    Attribution,
    AuditLog,
    Booking,
    Conversation,
    Feedback,
    HandoffQueue,
    Lead,
    Message,
    ScheduledNotification,
)

Grain = Literal["day", "week", "month"]

# Audit events that represent one attempt against one aggregator. The success
# rate and latency figures are computed from these and nothing else.
ATTEMPT_EVENT = "routing.attempt"


@dataclass(frozen=True, slots=True)
class Health:
    """What an operator checks first when something feels wrong."""

    database_ok: bool
    tenant_ok: bool
    phrasebank_locales: list[str]
    registered_adapters: list[str]
    implemented_adapters: list[str]
    # Conversations that have had no activity for a while but are not closed.
    # A rising number here means turns are dying mid-flight.
    stalled_conversations: int
    open_handoffs: int
    # Scheduled messages held behind template approval. Expected to be non-zero
    # until Meta approves the templates; a *rising* number is the signal.
    deferred_notifications: int
    failed_sends_24h: int
    oldest_pending_handoff_minutes: float | None

    @property
    def ok(self) -> bool:
        return self.database_ok and self.tenant_ok and bool(self.phrasebank_locales)


@dataclass(frozen=True, slots=True)
class AggregatorStat:
    """One platform's record, as the audit trail saw it."""

    platform_slug: str
    attempts: int
    successes: int
    median_latency_ms: int | None
    p95_latency_ms: int | None
    # Outcome counts, so "it fails a lot" can be read as "it times out a lot".
    outcomes: dict[str, int] = field(default_factory=dict)

    @property
    def success_rate(self) -> float:
        return (self.successes / self.attempts) if self.attempts else 0.0


@dataclass(frozen=True, slots=True)
class FailedAttempt:
    """An attempt that did not produce a booking, and why."""

    occurred_at: datetime
    conversation_id: uuid.UUID | None
    platform_slug: str | None
    merchant_ref: str | None
    outcome: str | None
    latency_ms: int | None
    advance_reason: str | None
    raw_error: str | None


@dataclass(frozen=True, slots=True)
class TraceStep:
    """One row of the routing trace.

    Carries both halves of the story: what the engine did, and what the consumer
    saw while it did it. Keeping them in one ordered sequence is the whole point
    of the trace view — the interleaving is the explanation.
    """

    seq: int
    occurred_at: datetime
    event_type: str
    actor: str
    platform_slug: str | None
    merchant_ref: str | None
    attempt_number: int | None
    outcome: str | None
    latency_ms: int | None
    advance_reason: str | None
    consumer_visible: bool
    consumer_text: str | None
    phrasebank_key: str | None
    decision_id: uuid.UUID | None
    turn_id: uuid.UUID | None
    details: dict[str, Any]

    @property
    def is_attempt(self) -> bool:
        return self.event_type == ATTEMPT_EVENT

    @property
    def is_decision(self) -> bool:
        return self.event_type.startswith("routing.")


async def health(
    session: AsyncSession, tenant_id: uuid.UUID | None, *, stall_minutes: int = 30
) -> Health:
    """A single call answering "is anything wrong right now?"."""
    from mawjood.core.aggregators.registry import build_default_registry
    from mawjood.core.conversation.phrasebank import get_phrasebank

    database_ok = True
    try:
        await session.execute(select(1))
    except Exception:
        database_ok = False

    registry = build_default_registry()
    adapters = registry.slugs
    implemented = [
        slug for slug in adapters if getattr(registry.get(slug), "implemented", True) is True
    ]

    if tenant_id is None:
        return Health(
            database_ok=database_ok,
            tenant_ok=False,
            phrasebank_locales=get_phrasebank().locales,
            registered_adapters=adapters,
            implemented_adapters=implemented,
            stalled_conversations=0,
            open_handoffs=0,
            deferred_notifications=0,
            failed_sends_24h=0,
            oldest_pending_handoff_minutes=None,
        )

    cutoff = datetime.now(UTC) - timedelta(minutes=stall_minutes)
    day_ago = datetime.now(UTC) - timedelta(hours=24)

    stalled = await _scalar(
        session,
        select(func.count())
        .select_from(Conversation)
        .where(Conversation.tenant_id == tenant_id)
        .where(Conversation.status.notin_(("closed",)))
        .where(Conversation.last_activity_at < cutoff),
    )
    open_handoffs = await _scalar(
        session,
        select(func.count())
        .select_from(HandoffQueue)
        .where(HandoffQueue.tenant_id == tenant_id)
        .where(HandoffQueue.status != HandoffStatus.RESOLVED),
    )
    deferred = await _scalar(
        session,
        select(func.count())
        .select_from(ScheduledNotification)
        .where(ScheduledNotification.tenant_id == tenant_id)
        .where(ScheduledNotification.status == NotificationStatus.DEFERRED),
    )
    failed_sends = await _scalar(
        session,
        select(func.count())
        .select_from(Message)
        .where(Message.tenant_id == tenant_id)
        .where(Message.status == "failed")
        .where(Message.created_at >= day_ago),
    )
    oldest = (
        await session.execute(
            select(func.min(HandoffQueue.created_at))
            .where(HandoffQueue.tenant_id == tenant_id)
            .where(HandoffQueue.status != HandoffStatus.RESOLVED)
        )
    ).scalar_one_or_none()

    return Health(
        database_ok=database_ok,
        tenant_ok=True,
        phrasebank_locales=get_phrasebank().locales,
        registered_adapters=adapters,
        implemented_adapters=implemented,
        stalled_conversations=stalled,
        open_handoffs=open_handoffs,
        deferred_notifications=deferred,
        failed_sends_24h=failed_sends,
        oldest_pending_handoff_minutes=(
            (datetime.now(UTC) - _aware(oldest)).total_seconds() / 60 if oldest else None
        ),
    )


async def live_conversations(
    session: AsyncSession, tenant_id: uuid.UUID, *, limit: int = 50
) -> list[tuple[Conversation, Lead, int]]:
    """Conversations by most recent activity, with their message counts.

    Most-recent-first because this view answers "what is happening now". The
    queue view is the one that sorts by who has waited longest.
    """
    counts = (
        select(Message.conversation_id, func.count().label("n"))
        .where(Message.tenant_id == tenant_id)
        .group_by(Message.conversation_id)
        .subquery()
    )
    result = await session.execute(
        select(Conversation, Lead, func.coalesce(counts.c.n, 0))
        .join(Lead, Lead.id == Conversation.lead_id)
        .join(counts, counts.c.conversation_id == Conversation.id, isouter=True)
        .where(Conversation.tenant_id == tenant_id)
        .order_by(Conversation.last_activity_at.desc())
        .limit(limit)
    )
    return [(row[0], row[1], int(row[2])) for row in result.all()]


async def bookings_by_grain(
    session: AsyncSession, tenant_id: uuid.UUID, *, grain: Grain = "day", periods: int = 14
) -> list[tuple[datetime, int, int]]:
    """Bookings per day/week/month: (period, confirmed, cancelled).

    Grouped in the database with ``date_trunc`` rather than in Python, so this
    stays one query when the table is large.
    """
    bucket = func.date_trunc(grain, Booking.created_at).label("bucket")
    result = await session.execute(
        select(
            bucket,
            func.count().filter(Booking.status == BookingStatus.CONFIRMED),
            func.count().filter(Booking.status == BookingStatus.CANCELLED),
        )
        .where(Booking.tenant_id == tenant_id)
        .group_by(bucket)
        .order_by(bucket.desc())
        .limit(periods)
    )
    return [(row[0], int(row[1]), int(row[2])) for row in result.all()]


async def aggregator_stats(
    session: AsyncSession, tenant_id: uuid.UUID, *, since_hours: int = 24 * 30
) -> list[AggregatorStat]:
    """Success rate and latency per platform, from the audit trail.

    Median and p95 rather than a mean: aggregator latency is long-tailed, and a
    mean is dragged around by the one call that timed out at 2.5 seconds. The
    median says what a typical consumer waits; p95 says what a bad day looks
    like.
    """
    since = datetime.now(UTC) - timedelta(hours=since_hours)
    base = (
        select(AuditLog)
        .where(AuditLog.tenant_id == tenant_id)
        .where(AuditLog.event_type == ATTEMPT_EVENT)
        .where(AuditLog.platform_slug.isnot(None))
        .where(AuditLog.occurred_at >= since)
        .subquery()
    )

    result = await session.execute(
        select(
            base.c.platform_slug,
            func.count(),
            func.count().filter(base.c.outcome == "ok"),
            func.percentile_cont(0.5).within_group(cast(base.c.latency_ms, Float)),
            func.percentile_cont(0.95).within_group(cast(base.c.latency_ms, Float)),
        )
        .group_by(base.c.platform_slug)
        .order_by(func.count().desc())
    )
    rows = result.all()

    outcomes = await session.execute(
        select(base.c.platform_slug, base.c.outcome, func.count()).group_by(
            base.c.platform_slug, base.c.outcome
        )
    )
    by_platform: dict[str, dict[str, int]] = {}
    for slug, outcome, count in outcomes.all():
        by_platform.setdefault(str(slug), {})[str(outcome)] = int(count)

    return [
        AggregatorStat(
            platform_slug=str(row[0]),
            attempts=int(row[1]),
            successes=int(row[2]),
            median_latency_ms=int(row[3]) if row[3] is not None else None,
            p95_latency_ms=int(row[4]) if row[4] is not None else None,
            outcomes=by_platform.get(str(row[0]), {}),
        )
        for row in rows
    ]


async def failed_attempts(
    session: AsyncSession, tenant_id: uuid.UUID, *, limit: int = 100
) -> list[FailedAttempt]:
    """Every attempt that did not succeed, newest first, with the reason.

    Reads ``routing.attempt`` rather than ``routing.advanced`` so an attempt that
    failed at the end of the list — where there was nothing to advance to —
    still appears. Those are the ones that ended in a handoff, which makes them
    the most interesting rows on the page.
    """
    result = await session.execute(
        select(AuditLog)
        .where(AuditLog.tenant_id == tenant_id)
        .where(AuditLog.event_type == ATTEMPT_EVENT)
        .where(and_(AuditLog.outcome.isnot(None), AuditLog.outcome != "ok"))
        .order_by(AuditLog.seq.desc())
        .limit(limit)
    )
    rows = list(result.scalars().all())

    # The advance reason lives on the following routing.advanced row for the same
    # decision. Fetched in one query and joined in Python rather than as a
    # correlated subquery per row.
    decision_ids = [row.decision_id for row in rows if row.decision_id]
    reasons: dict[tuple[uuid.UUID, str | None], str] = {}
    if decision_ids:
        advanced = await session.execute(
            select(AuditLog)
            .where(AuditLog.tenant_id == tenant_id)
            .where(AuditLog.event_type == "routing.advanced")
            .where(AuditLog.decision_id.in_(decision_ids))
        )
        for row in advanced.scalars().all():
            if row.decision_id:
                reasons[(row.decision_id, row.platform_slug)] = row.advance_reason or ""

    return [
        FailedAttempt(
            occurred_at=row.occurred_at,
            conversation_id=row.conversation_id,
            platform_slug=row.platform_slug,
            merchant_ref=row.merchant_ref,
            outcome=row.outcome,
            latency_ms=row.latency_ms,
            advance_reason=(
                row.advance_reason or reasons.get((row.decision_id, row.platform_slug))
                if row.decision_id
                else row.advance_reason
            ),
            raw_error=str(row.details.get("raw_error")) if row.details.get("raw_error") else None,
        )
        for row in rows
    ]


async def source_counts(
    session: AsyncSession, tenant_id: uuid.UUID
) -> list[tuple[str, str, int, int]]:
    """Per source code: (code, medium, leads, bookings).

    Bookings alongside leads because a code that brings a hundred people who
    never book is a different problem from one that brings ten who all do, and a
    lead count alone cannot tell them apart.
    """
    booked_leads = (
        select(Booking.lead_id)
        .where(Booking.tenant_id == tenant_id)
        .where(Booking.status == BookingStatus.CONFIRMED)
        .subquery()
    )
    result = await session.execute(
        select(
            Attribution.source_code,
            Attribution.medium,
            func.count(func.distinct(Attribution.lead_id)),
            func.count(func.distinct(booked_leads.c.lead_id)),
        )
        .join(booked_leads, booked_leads.c.lead_id == Attribution.lead_id, isouter=True)
        .where(Attribution.tenant_id == tenant_id)
        .where(Attribution.is_first_touch.is_(True))
        .group_by(Attribution.source_code, Attribution.medium)
        .order_by(func.count(func.distinct(Attribution.lead_id)).desc())
    )
    return [(str(r[0]), str(r[1]), int(r[2]), int(r[3])) for r in result.all()]


async def routing_trace(
    session: AsyncSession, tenant_id: uuid.UUID, conversation_id: uuid.UUID
) -> list[TraceStep]:
    """The whole decision tree for one conversation, in order.

    Everything: every aggregator tried, its outcome, its latency, why the engine
    moved on, and — interleaved in the same sequence — exactly what the consumer
    saw at each step. The interleaving is the point. "We tried three platforms
    and the consumer saw one holding message" is only legible when both are on
    the same timeline.
    """
    result = await session.execute(
        select(AuditLog)
        .where(AuditLog.tenant_id == tenant_id)
        .where(AuditLog.conversation_id == conversation_id)
        .order_by(AuditLog.seq)
    )
    return [
        TraceStep(
            seq=row.seq,
            occurred_at=row.occurred_at,
            event_type=row.event_type,
            actor=row.actor,
            platform_slug=row.platform_slug,
            merchant_ref=row.merchant_ref,
            attempt_number=row.attempt_number,
            outcome=row.outcome,
            latency_ms=row.latency_ms,
            advance_reason=row.advance_reason,
            consumer_visible=row.consumer_visible,
            consumer_text=row.consumer_text,
            phrasebank_key=row.phrasebank_key,
            decision_id=row.decision_id,
            turn_id=row.turn_id,
            details=row.details or {},
        )
        for row in result.scalars().all()
    ]


async def conversation_with_lead(
    session: AsyncSession, tenant_id: uuid.UUID, conversation_id: uuid.UUID
) -> tuple[Conversation, Lead] | None:
    result = await session.execute(
        select(Conversation, Lead)
        .join(Lead, Lead.id == Conversation.lead_id)
        .where(Conversation.tenant_id == tenant_id)
        .where(Conversation.id == conversation_id)
    )
    row = result.one_or_none()
    return (row[0], row[1]) if row else None


async def transcript(
    session: AsyncSession, tenant_id: uuid.UUID, conversation_id: uuid.UUID
) -> list[Message]:
    result = await session.execute(
        select(Message)
        .where(Message.tenant_id == tenant_id)
        .where(Message.conversation_id == conversation_id)
        .order_by(Message.seq)
    )
    return list(result.scalars().all())


async def bookings_for_conversation(
    session: AsyncSession, tenant_id: uuid.UUID, conversation_id: uuid.UUID
) -> list[Booking]:
    result = await session.execute(
        select(Booking)
        .where(Booking.tenant_id == tenant_id)
        .where(Booking.conversation_id == conversation_id)
        .order_by(Booking.created_at)
    )
    return list(result.scalars().all())


async def scheduled_for_tenant(
    session: AsyncSession, tenant_id: uuid.UUID, *, limit: int = 200
) -> list[tuple[ScheduledNotification, Booking, Lead]]:
    result = await session.execute(
        select(ScheduledNotification, Booking, Lead)
        .join(Booking, Booking.id == ScheduledNotification.booking_id)
        .join(Lead, Lead.id == ScheduledNotification.lead_id)
        .where(ScheduledNotification.tenant_id == tenant_id)
        .order_by(ScheduledNotification.due_at)
        .limit(limit)
    )
    return [(row[0], row[1], row[2]) for row in result.all()]


async def handoff_queue(
    session: AsyncSession, tenant_id: uuid.UUID, *, limit: int = 50
) -> list[tuple[HandoffQueue, Conversation, Lead]]:
    """Waiting for a person. Oldest first within a priority band."""
    result = await session.execute(
        select(HandoffQueue, Conversation, Lead)
        .join(Conversation, Conversation.id == HandoffQueue.conversation_id)
        .join(Lead, Lead.id == HandoffQueue.lead_id)
        .where(HandoffQueue.tenant_id == tenant_id)
        .where(HandoffQueue.status != HandoffStatus.RESOLVED)
        .order_by(HandoffQueue.priority.desc(), HandoffQueue.created_at)
        .limit(limit)
    )
    return [(row[0], row[1], row[2]) for row in result.all()]


async def satisfaction_summary(
    session: AsyncSession, tenant_id: uuid.UUID
) -> tuple[float | None, int, list[Feedback]]:
    """Average rating, how many responded, and the comments.

    The comments are returned in full because they are the part a 1-to-5 average
    throws away, and the reason the free-text field exists.
    """
    average = (
        await session.execute(
            select(func.avg(Feedback.rating))
            .where(Feedback.tenant_id == tenant_id)
            .where(Feedback.rating.isnot(None))
        )
    ).scalar_one_or_none()
    count = await _scalar(
        session,
        select(func.count())
        .select_from(Feedback)
        .where(Feedback.tenant_id == tenant_id)
        .where(Feedback.rating.isnot(None)),
    )
    comments = (
        (
            await session.execute(
                select(Feedback)
                .where(Feedback.tenant_id == tenant_id)
                .where(Feedback.comment.isnot(None))
                .order_by(Feedback.responded_at.desc())
                .limit(20)
            )
        )
        .scalars()
        .all()
    )
    return (float(average) if average is not None else None, count, list(comments))


async def headline_counts(session: AsyncSession, tenant_id: uuid.UUID) -> dict[str, int]:
    async def count(model: Any, *filters: Any) -> int:
        statement = select(func.count()).select_from(model).where(model.tenant_id == tenant_id)
        for condition in filters:
            statement = statement.where(condition)
        return await _scalar(session, statement)

    return {
        "leads": await count(Lead),
        "conversations": await count(Conversation),
        "bookings": await count(Booking, Booking.status == BookingStatus.CONFIRMED),
        "messages": await count(Message),
        "inbound": await count(Message, Message.direction == Direction.INBOUND),
        "waiting": await count(HandoffQueue, HandoffQueue.status != HandoffStatus.RESOLVED),
    }


async def _scalar(session: AsyncSession, statement: Any) -> int:
    return int((await session.execute(statement)).scalar_one())


def _aware(value: datetime) -> datetime:
    return value if value.tzinfo is not None else value.replace(tzinfo=UTC)


__all__ = [
    "ATTEMPT_EVENT",
    "AggregatorStat",
    "FailedAttempt",
    "Grain",
    "Health",
    "TraceStep",
    "aggregator_stats",
    "bookings_by_grain",
    "bookings_for_conversation",
    "conversation_with_lead",
    "failed_attempts",
    "handoff_queue",
    "headline_counts",
    "health",
    "live_conversations",
    "routing_trace",
    "satisfaction_summary",
    "scheduled_for_tenant",
    "source_counts",
    "transcript",
]
