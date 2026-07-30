"""The notification scheduler: offsets, the 24-hour window, and honest failure.

Two rules from PLAN.md drive this file:

* reminder / follow-up / satisfaction fire at the right offsets under a frozen
  clock;
* nothing is ever sent outside the 24-hour session window without an approved
  template.

The second is the one worth caring about. Mawjood has **no approved templates** —
none have been submitted, because that needs the operator's Meta Business
Manager. So the honest behaviour today is that most reminders cannot be sent,
and the scheduler has to say so rather than swallow it. The tests below pin all
three of the ways that could go wrong: sending anyway, dropping silently, and
recording an unsent message as sent.

No clock is patched anywhere. ``now`` is an argument.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from mawjood.config import Settings
from mawjood.core.enums import (
    BookingStatus,
    Category,
    Channel,
    Direction,
    MessageStatus,
    PaymentStatus,
)
from mawjood.core.notifications import (
    NotificationKind,
    Offsets,
    Plan,
    Scheduler,
    Template,
    TemplateRegistry,
    TemplateStatus,
    Verdict,
    build_template_registry,
    decide,
    due_at,
    window_is_open,
)
from mawjood.db.models import AuditLog, Booking, Conversation, Lead, Message

from .conftest import needs_database

# A fixed point to reason from. Friday 30 July 2026, 14:00 UTC — 6 pm in Dubai.
NOW = datetime(2026, 7, 30, 14, 0, tzinfo=UTC)
SLOT = datetime(2026, 7, 31, 13, 0, tzinfo=UTC)  # tomorrow, 5 pm Dubai
OFFSETS = Offsets()


def approved_registry() -> TemplateRegistry:
    """What the world looks like the day Meta approves the reminder template."""
    registry = build_template_registry()
    registry.declare(
        str(NotificationKind.REMINDER),
        Template(
            name="booking_reminder",
            phrasebank_key="notify.reminder",
            locale="en",
            status=TemplateStatus.APPROVED,
            variables=("venue", "time"),
        ),
    )
    return registry


class TestOffsets:
    def test_the_reminder_lands_before_the_appointment(self) -> None:
        when = due_at(NotificationKind.REMINDER, slot_start=SLOT, slot_end=None, offsets=OFFSETS)
        assert when == SLOT - timedelta(hours=3)

    def test_the_follow_up_lands_after_it_ends(self) -> None:
        end = SLOT + timedelta(hours=1)
        when = due_at(NotificationKind.FOLLOW_UP, slot_start=SLOT, slot_end=end, offsets=OFFSETS)
        assert when == end + timedelta(hours=2)

    def test_satisfaction_waits_a_day(self) -> None:
        end = SLOT + timedelta(hours=1)
        when = due_at(NotificationKind.SATISFACTION, slot_start=SLOT, slot_end=end, offsets=OFFSETS)
        assert when == end + timedelta(hours=24)

    def test_a_missing_end_time_falls_back_to_the_start(self) -> None:
        """Many upstreams return only a start.

        Treating that as unschedulable would silently disable two thirds of this
        feature for most bookings.
        """
        when = due_at(NotificationKind.FOLLOW_UP, slot_start=SLOT, slot_end=None, offsets=OFFSETS)
        assert when == SLOT + timedelta(hours=2)

    def test_a_booking_with_no_time_schedules_nothing(self) -> None:
        for kind in NotificationKind:
            assert due_at(kind, slot_start=None, slot_end=None, offsets=OFFSETS) is None

    def test_offsets_come_from_settings(self) -> None:
        settings = Settings(
            database_url="postgresql+asyncpg://u@127.0.0.1:5432/d",
            notify_reminder_hours_before=1.5,
            notify_grace_hours=0.5,
            whatsapp_session_window_hours=12.0,
        )
        offsets = Offsets.from_settings(settings)
        assert offsets.reminder_hours_before == 1.5
        assert offsets.grace_hours == 0.5
        assert offsets.session_window_hours == 12.0

    def test_a_negative_grace_is_refused(self) -> None:
        with pytest.raises(ValueError, match="grace_hours"):
            Offsets(grace_hours=-1)


class TestTheSessionWindow:
    def test_open_just_inside_twenty_four_hours(self) -> None:
        assert window_is_open(
            last_inbound_at=NOW - timedelta(hours=23, minutes=59),
            now=NOW,
            offsets=OFFSETS,
        )

    def test_closed_just_outside(self) -> None:
        assert not window_is_open(
            last_inbound_at=NOW - timedelta(hours=24, minutes=1), now=NOW, offsets=OFFSETS
        )

    def test_a_consumer_never_heard_from_has_no_window(self) -> None:
        """Silence is not a session, and it is not consent to message."""
        assert not window_is_open(last_inbound_at=None, now=NOW, offsets=OFFSETS)

    def test_the_window_length_is_configuration_not_a_constant(self) -> None:
        """It is a provider policy, not a law of nature."""
        short = Offsets(session_window_hours=1.0)
        assert not window_is_open(last_inbound_at=NOW - timedelta(hours=2), now=NOW, offsets=short)


def reminder_at(
    now: datetime,
    *,
    slot_start: datetime | None = SLOT,
    last_inbound_at: datetime | None = None,
    already_sent: bool = False,
    cancelled: bool = False,
) -> Plan:
    """A reminder decision at ``now``, with the window open unless told otherwise."""
    return decide(
        NotificationKind.REMINDER,
        now=now,
        slot_start=slot_start,
        slot_end=None,
        last_inbound_at=now if last_inbound_at is None else last_inbound_at,
        offsets=OFFSETS,
        template=None,
        already_sent=already_sent,
        cancelled=cancelled,
    )


class TestTiming:
    def test_before_its_moment_it_is_not_yet(self) -> None:
        assert reminder_at(SLOT - timedelta(hours=5)).verdict is Verdict.NOT_YET

    def test_at_its_moment_it_goes(self) -> None:
        assert reminder_at(SLOT - timedelta(hours=3)).verdict is Verdict.SEND_FREEFORM

    def test_inside_the_grace_period_it_still_goes(self) -> None:
        """A scheduler down for an hour should still send."""
        plan = reminder_at(SLOT - timedelta(hours=1, minutes=30))
        assert plan.verdict is Verdict.SEND_FREEFORM

    def test_past_the_grace_period_it_expires(self) -> None:
        """A reminder that arrives after the appointment is noise."""
        assert reminder_at(SLOT + timedelta(hours=1)).verdict is Verdict.EXPIRED

    def test_an_already_sent_notification_is_not_sent_twice(self) -> None:
        plan = reminder_at(SLOT - timedelta(hours=3), already_sent=True)
        assert plan.verdict is Verdict.ALREADY_SENT

    def test_a_cancelled_booking_notifies_nobody(self) -> None:
        plan = reminder_at(SLOT - timedelta(hours=3), cancelled=True)
        assert plan.verdict is Verdict.CANCELLED

    def test_cancellation_beats_everything(self) -> None:
        """Order matters: a cancelled booking must not read as 'blocked'."""
        plan = reminder_at(
            SLOT - timedelta(hours=3),
            cancelled=True,
            already_sent=True,
            last_inbound_at=SLOT - timedelta(hours=100),
        )
        assert plan.verdict is Verdict.CANCELLED

    def test_a_booking_with_no_slot_time_is_not_schedulable(self) -> None:
        assert reminder_at(NOW, slot_start=None).verdict is Verdict.NOT_SCHEDULABLE

    def test_naive_datetimes_do_not_explode(self) -> None:
        """A test writing datetime(2026, 1, 1) should get a sane answer.

        Comparing a naive and an aware datetime raises, so without this the
        scheduler dies on the first badly-typed input rather than deciding.
        """
        plan = decide(
            NotificationKind.REMINDER,
            now=datetime(2026, 7, 31, 10, 0),
            slot_start=datetime(2026, 7, 31, 13, 0),
            slot_end=None,
            last_inbound_at=datetime(2026, 7, 31, 9, 0),
            offsets=OFFSETS,
            template=None,
        )
        assert plan.verdict is Verdict.SEND_FREEFORM


class TestTheWindowIsEnforced:
    """The rule PLAN.md states outright: never outside the window without a template."""

    def test_inside_the_window_ordinary_copy_goes_out(self) -> None:
        plan = decide(
            NotificationKind.REMINDER,
            now=SLOT - timedelta(hours=3),
            slot_start=SLOT,
            slot_end=None,
            last_inbound_at=SLOT - timedelta(hours=4),
            offsets=OFFSETS,
            template=None,
        )
        assert plan.verdict is Verdict.SEND_FREEFORM
        assert plan.sends

    def test_outside_the_window_with_nothing_approved_is_blocked(self) -> None:
        """Today's reality for almost every reminder."""
        plan = decide(
            NotificationKind.REMINDER,
            now=SLOT - timedelta(hours=3),
            slot_start=SLOT,
            slot_end=None,
            # Confirmed two days ago and quiet since.
            last_inbound_at=SLOT - timedelta(hours=50),
            offsets=OFFSETS,
            template=build_template_registry().get("reminder", "en"),
        )
        assert plan.verdict is Verdict.BLOCKED_NO_APPROVED_TEMPLATE
        assert not plan.sends
        assert "not recorded as sent" in plan.reason

    def test_outside_the_window_with_an_approved_template_goes_out(self) -> None:
        """And the day approval lands, this path opens with no code change."""
        plan = decide(
            NotificationKind.REMINDER,
            now=SLOT - timedelta(hours=3),
            slot_start=SLOT,
            slot_end=None,
            last_inbound_at=SLOT - timedelta(hours=50),
            offsets=OFFSETS,
            template=approved_registry().get("reminder", "en"),
        )
        assert plan.verdict is Verdict.SEND_TEMPLATE
        assert plan.sends
        assert plan.template is not None
        assert plan.template.name == "booking_reminder"

    def test_a_pending_template_is_not_an_approved_one(self) -> None:
        """Submitted is not approved. Sending on a pending template is a ban."""
        pending = Template(
            name="booking_reminder",
            phrasebank_key="notify.reminder",
            locale="en",
            status=TemplateStatus.PENDING,
        )
        plan = decide(
            NotificationKind.REMINDER,
            now=SLOT - timedelta(hours=3),
            slot_start=SLOT,
            slot_end=None,
            last_inbound_at=SLOT - timedelta(hours=50),
            offsets=OFFSETS,
            template=pending,
        )
        assert plan.verdict is Verdict.BLOCKED_NO_APPROVED_TEMPLATE

    def test_a_rejected_template_is_not_an_approved_one(self) -> None:
        rejected = Template(
            name="booking_reminder",
            phrasebank_key="notify.reminder",
            locale="en",
            status=TemplateStatus.REJECTED,
        )
        plan = decide(
            NotificationKind.REMINDER,
            now=SLOT - timedelta(hours=3),
            slot_start=SLOT,
            slot_end=None,
            last_inbound_at=SLOT - timedelta(hours=50),
            offsets=OFFSETS,
            template=rejected,
        )
        assert plan.verdict is Verdict.BLOCKED_NO_APPROVED_TEMPLATE

    @pytest.mark.parametrize("kind", list(NotificationKind))
    def test_no_kind_can_escape_the_window(self, kind: NotificationKind) -> None:
        """Exhaustive over kinds, so a new one added later cannot slip past.

        A notification kind introduced without a template declaration would
        otherwise quietly acquire permission to message people at any hour.
        """
        when = due_at(kind, slot_start=SLOT, slot_end=None, offsets=OFFSETS)
        assert when is not None
        plan = decide(
            kind,
            now=when,
            slot_start=SLOT,
            slot_end=None,
            last_inbound_at=when - timedelta(hours=48),
            offsets=OFFSETS,
            template=build_template_registry().get(str(kind), "en"),
        )
        assert plan.verdict is Verdict.BLOCKED_NO_APPROVED_TEMPLATE
        assert not plan.sends


class TestTheTemplateRegistry:
    def test_nothing_is_approved_today(self) -> None:
        """If this ever fails, either approval landed or someone faked it."""
        registry = build_template_registry()
        assert not registry.any_approved
        assert all(t.status is TemplateStatus.NOT_SUBMITTED for t in registry.declared)

    @pytest.mark.parametrize("kind", list(NotificationKind))
    def test_every_kind_has_a_declared_template(self, kind: NotificationKind) -> None:
        """Declared before approved, so approval is a status flip.

        This is also how we find out a new kind was added with no route out of
        the session window.
        """
        assert build_template_registry().get(str(kind), "en") is not None

    def test_every_template_names_a_real_phrasebank_entry(self) -> None:
        """The template and the phrasebank must say the same thing.

        A consumer inside the window and a consumer outside it should get the
        same message. That cannot be checked automatically until a real template
        body exists, but the mapping can be — and a template pointing at a
        phrasebank key that does not exist is a guaranteed mismatch.
        """
        from mawjood.core.conversation.phrasebank import get_phrasebank

        phrasebank = get_phrasebank()
        for template in build_template_registry().declared:
            rendered = phrasebank.render(
                template.phrasebank_key, template.locale, venue="X", time="5 pm"
            )
            assert rendered.text

    def test_no_locale_fallback(self) -> None:
        """Sending English to an Arabic consumer because English was approved
        first is exactly the kind of helpfulness nobody wants."""
        assert build_template_registry().get("reminder", "ar") is None

    def test_approved_returns_nothing_until_it_is_approved(self) -> None:
        assert build_template_registry().approved("reminder", "en") is None
        assert approved_registry().approved("reminder", "en") is not None


class TestNotificationCopy:
    def test_the_scheduled_copy_carries_no_failure_language(self) -> None:
        """These entries are in the phrasebank, so the main scan already covers
        them. Asserted here too because they are the copy most likely to be
        written apologetically — an unprompted message invites an apology."""
        from mawjood.core.conversation.phrasebank import get_phrasebank
        from tests.cascade.test_no_empty_shelves import scan

        phrasebank = get_phrasebank()
        for key in (
            "notify.reminder",
            "notify.follow_up",
            "notify.satisfaction",
            "notify.satisfaction_thanks",
        ):
            text = phrasebank.render(key, "en", venue="Marina Beauty Lounge", time="5 pm").text
            assert not scan(text), f"{key}: {scan(text)}"

    def test_the_copy_is_not_chirpy(self) -> None:
        """Mawjood's voice, held at the point it is easiest to lose."""
        from mawjood.core.conversation.phrasebank import get_phrasebank

        phrasebank = get_phrasebank()
        for key in ("notify.reminder", "notify.follow_up", "notify.satisfaction"):
            text = phrasebank.render(key, "en", venue="X", time="5 pm").text
            assert "!" not in text, f"{key} has an exclamation mark"


# ---------------------------------------------------------------------------
# The scheduler against a real database
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class SeededBooking:
    lead_id: uuid.UUID
    conversation_id: uuid.UUID
    booking_id: uuid.UUID


async def seed_booking(
    session: AsyncSession,
    tenant_id: uuid.UUID,
    *,
    slot_start: datetime,
    status: BookingStatus = BookingStatus.CONFIRMED,
) -> SeededBooking:
    """A confirmed booking with a lead and a conversation behind it."""
    lead = Lead(tenant_id=tenant_id, wa_id="971501234567", locale="en")
    session.add(lead)
    await session.flush()

    conversation = Conversation(
        tenant_id=tenant_id, lead_id=lead.id, channel=Channel.WHATSAPP, locale="en"
    )
    session.add(conversation)
    await session.flush()

    booking = Booking(
        tenant_id=tenant_id,
        conversation_id=conversation.id,
        lead_id=lead.id,
        category=Category.SALON,
        platform_slug="fake_happy",
        idempotency_key=f"idem-{uuid.uuid4()}",
        status=status,
        slot_start=slot_start,
        slot_end=slot_start + timedelta(hours=1),
        venue_name="Marina Beauty Lounge",
        payment_status=PaymentStatus.PAY_AT_VENUE,
        confirmed_at=slot_start - timedelta(days=2),
    )
    session.add(booking)
    await session.flush()
    return SeededBooking(lead.id, conversation.id, booking.id)


async def add_inbound(
    session: AsyncSession, tenant_id: uuid.UUID, ids: SeededBooking, *, at: datetime
) -> None:
    """A consumer message at a chosen time. The session window starts here.

    ``created_at`` is set explicitly rather than left to the server default,
    because the whole point is to place it relative to the booking.
    """
    session.add(
        Message(
            tenant_id=tenant_id,
            conversation_id=ids.conversation_id,
            lead_id=ids.lead_id,
            turn_id=uuid.uuid4(),
            direction=Direction.INBOUND,
            channel=Channel.WHATSAPP,
            status=MessageStatus.RECEIVED,
            body="thanks",
            locale="en",
            created_at=at,
        )
    )
    await session.flush()


async def outbound_with_key(session: AsyncSession, key: str) -> list[Message]:
    result = await session.execute(
        select(Message)
        .where(Message.direction == Direction.OUTBOUND)
        .where(Message.phrasebank_key == key)
        .order_by(Message.seq)
    )
    return list(result.scalars().all())


@pytest.mark.integration
@needs_database
class TestTheScheduler:
    """The half that touches bookings, the transcript and the BSP.

    Everything above is pure. This is where "never pretend a send happened"
    stops being a verdict enum and becomes rows in a table.
    """

    async def test_a_reminder_inside_the_window_is_sent_and_recorded(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        db_settings: Settings,
        tenant_id: uuid.UUID,
    ) -> None:
        async with session_factory() as session:
            ids = await seed_booking(session, tenant_id, slot_start=SLOT)
            # The consumer spoke an hour ago, so the window is open.
            await add_inbound(session, tenant_id, ids, at=SLOT - timedelta(hours=4))
            await session.commit()

        async with session_factory() as session:
            scheduler = Scheduler(session, tenant_id=tenant_id)
            considered = await scheduler.run(now=SLOT - timedelta(hours=3))
            await session.commit()

        sent = [s for s in considered if s.plan.verdict is Verdict.SEND_FREEFORM]
        assert len(sent) == 1
        assert sent[0].kind is NotificationKind.REMINDER

        async with session_factory() as session:
            rows = await outbound_with_key(session, "notify.reminder")
        assert len(rows) == 1
        assert "Marina Beauty Lounge" in rows[0].body

    async def test_a_reminder_outside_the_window_is_not_sent_and_not_recorded(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        db_settings: Settings,
        tenant_id: uuid.UUID,
    ) -> None:
        """The assertion this whole feature turns on.

        No approved template exists, the window has closed, so the reminder
        cannot go out. What must NOT happen: a row in messages claiming it did.
        """
        async with session_factory() as session:
            ids = await seed_booking(session, tenant_id, slot_start=SLOT)
            await add_inbound(session, tenant_id, ids, at=SLOT - timedelta(hours=60))
            await session.commit()

        async with session_factory() as session:
            scheduler = Scheduler(session, tenant_id=tenant_id)
            considered = await scheduler.run(now=SLOT - timedelta(hours=3))
            await session.commit()

        blocked = [s for s in considered if s.plan.verdict is Verdict.BLOCKED_NO_APPROVED_TEMPLATE]
        assert blocked, "the reminder should have been blocked, not quietly dropped"

        async with session_factory() as session:
            assert await outbound_with_key(session, "notify.reminder") == []

    async def test_a_blocked_notification_is_audited_with_its_reason(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        db_settings: Settings,
        tenant_id: uuid.UUID,
    ) -> None:
        """Degrading honestly means someone can find out. Silence is not honest."""
        async with session_factory() as session:
            ids = await seed_booking(session, tenant_id, slot_start=SLOT)
            await add_inbound(session, tenant_id, ids, at=SLOT - timedelta(hours=60))
            await session.commit()

        async with session_factory() as session:
            await Scheduler(session, tenant_id=tenant_id).run(now=SLOT - timedelta(hours=3))
            await session.commit()

        async with session_factory() as session:
            events = (
                (
                    await session.execute(
                        select(AuditLog).where(AuditLog.event_type == "notification.blocked")
                    )
                )
                .scalars()
                .all()
            )
        assert events
        details = events[0].details
        assert details["verdict"] == "blocked_no_approved_template"
        assert details["template_status"] == "not_submitted"
        assert "session window" in details["reason"]

    async def test_running_twice_does_not_send_twice(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        db_settings: Settings,
        tenant_id: uuid.UUID,
    ) -> None:
        """A scheduler on a five-minute timer must not send five reminders."""
        async with session_factory() as session:
            ids = await seed_booking(session, tenant_id, slot_start=SLOT)
            await add_inbound(session, tenant_id, ids, at=SLOT - timedelta(hours=4))
            await session.commit()

        for _ in range(3):
            async with session_factory() as session:
                await Scheduler(session, tenant_id=tenant_id).run(now=SLOT - timedelta(hours=3))
                await session.commit()

        async with session_factory() as session:
            assert len(await outbound_with_key(session, "notify.reminder")) == 1

    async def test_an_approved_template_opens_the_out_of_window_path(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        db_settings: Settings,
        tenant_id: uuid.UUID,
    ) -> None:
        """Proving the day-approval-lands path works, before approval lands."""
        async with session_factory() as session:
            ids = await seed_booking(session, tenant_id, slot_start=SLOT)
            await add_inbound(session, tenant_id, ids, at=SLOT - timedelta(hours=60))
            await session.commit()

        async with session_factory() as session:
            scheduler = Scheduler(session, tenant_id=tenant_id, templates=approved_registry())
            considered = await scheduler.run(now=SLOT - timedelta(hours=3))
            await session.commit()

        assert any(s.plan.verdict is Verdict.SEND_TEMPLATE for s in considered)
        async with session_factory() as session:
            assert len(await outbound_with_key(session, "notify.reminder")) == 1

    async def test_a_cancelled_booking_is_left_alone(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        db_settings: Settings,
        tenant_id: uuid.UUID,
    ) -> None:
        async with session_factory() as session:
            ids = await seed_booking(
                session, tenant_id, slot_start=SLOT, status=BookingStatus.CANCELLED
            )
            await add_inbound(session, tenant_id, ids, at=SLOT - timedelta(hours=4))
            await session.commit()

        async with session_factory() as session:
            considered = await Scheduler(session, tenant_id=tenant_id).run(
                now=SLOT - timedelta(hours=3)
            )
            await session.commit()

        assert considered == []
        async with session_factory() as session:
            assert await outbound_with_key(session, "notify.reminder") == []

    async def test_the_reminder_quotes_a_local_time(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        db_settings: Settings,
        tenant_id: uuid.UUID,
    ) -> None:
        """ "5 pm", not "13:00+00:00".

        The booking is stored in UTC. A reminder that quotes UTC to someone in
        Dubai is worse than no reminder.
        """
        async with session_factory() as session:
            ids = await seed_booking(session, tenant_id, slot_start=SLOT)
            await add_inbound(session, tenant_id, ids, at=SLOT - timedelta(hours=4))
            await session.commit()

        async with session_factory() as session:
            await Scheduler(session, tenant_id=tenant_id).run(now=SLOT - timedelta(hours=3))
            await session.commit()

        async with session_factory() as session:
            body = (await outbound_with_key(session, "notify.reminder"))[0].body
        # 13:00 UTC is 5 pm in Asia/Dubai.
        assert "5 pm" in body
        assert "13:00" not in body

    async def test_the_scheduler_is_tenant_scoped(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        db_settings: Settings,
        tenant_id: uuid.UUID,
        other_tenant_id: uuid.UUID,
    ) -> None:
        async with session_factory() as session:
            ids = await seed_booking(session, tenant_id, slot_start=SLOT)
            await add_inbound(session, tenant_id, ids, at=SLOT - timedelta(hours=4))
            await session.commit()

        async with session_factory() as session:
            considered = await Scheduler(session, tenant_id=other_tenant_id).run(
                now=SLOT - timedelta(hours=3)
            )
        assert considered == []

    async def test_the_send_goes_through_the_bsp(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        db_settings: Settings,
        tenant_id: uuid.UUID,
    ) -> None:
        """And what reaches the BSP is a RenderedMessage, not a string.

        The send interface accepts nothing else, so this also proves the
        scheduler cannot smuggle an f-string to a consumer.
        """
        from mawjood.core.conversation.phrasebank import RenderedMessage

        captured: list[object] = []

        class RecordingBSP:
            slug = "recording"

            async def send(self, outbound: object) -> str:
                captured.append(outbound)
                return "provider-msg-1"

        async with session_factory() as session:
            ids = await seed_booking(session, tenant_id, slot_start=SLOT)
            await add_inbound(session, tenant_id, ids, at=SLOT - timedelta(hours=4))
            await session.commit()

        async with session_factory() as session:
            await Scheduler(session, tenant_id=tenant_id).run(
                bsp=RecordingBSP(), now=SLOT - timedelta(hours=3)
            )
            await session.commit()

        assert len(captured) == 1
        outbound = captured[0]
        assert isinstance(outbound.message, RenderedMessage)  # type: ignore[attr-defined]

        async with session_factory() as session:
            row = (await outbound_with_key(session, "notify.reminder"))[0]
        assert row.provider_message_id == "provider-msg-1"
        assert row.status is MessageStatus.SENT
        assert row.sent_at is not None
