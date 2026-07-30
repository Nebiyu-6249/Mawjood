"""Scheduled messages: offsets, persistence, idempotency, degradation, capture.

Five things the brief asks for, and each has a class here:

* **Two reminders**, at 24h and 2h, both configurable.
* **Persistent scheduling** — rows written when a booking is confirmed, so a
  restart loses nothing. Asserted by dropping the objects entirely and reading
  the schedule back from a fresh session.
* **Idempotent sends** — never double-remind. Enforced by a unique constraint,
  so the test tries to violate it directly rather than trusting a code path.
* **Template names from config**, and **degradation to session messages** when a
  template is not approved.
* **Satisfaction capture**: a 1-5 rating *and* the free-text comment.

No clock is patched anywhere. ``now`` is an argument.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from mawjood.config import Settings
from mawjood.core.enums import (
    BookingStatus,
    Category,
    Channel,
    Direction,
    MessageStatus,
    NotificationKind,
    NotificationStatus,
    PaymentStatus,
)
from mawjood.core.notifications import (
    PHRASE_FOR,
    Offsets,
    Plan,
    Scheduler,
    Template,
    TemplateRegistry,
    TemplateStatus,
    Verdict,
    build_template_registry,
    capture_satisfaction,
    decide,
    due_at,
    parse_satisfaction,
    schedule_for_booking,
    window_is_open,
)
from mawjood.db.models import (
    AuditLog,
    Booking,
    Conversation,
    Feedback,
    Lead,
    Message,
    ScheduledNotification,
)

from .conftest import RoutingSeeder, needs_database

# A fixed point. Friday 31 July 2026, 13:00 UTC — 5 pm in Dubai.
SLOT = datetime(2026, 7, 31, 13, 0, tzinfo=UTC)
OFFSETS = Offsets()


def approved_registry(*kinds: NotificationKind) -> TemplateRegistry:
    """The world on the day Meta approves the named templates."""
    registry = build_template_registry()
    for kind in kinds or tuple(NotificationKind):
        registry.declare(
            kind,
            Template(
                name=f"booking_{kind.value}",
                phrasebank_key=PHRASE_FOR[kind],
                locale="en",
                status=TemplateStatus.APPROVED,
            ),
        )
    return registry


# ---------------------------------------------------------------------------
# Offsets — two reminders, both configurable
# ---------------------------------------------------------------------------


class TestOffsets:
    def test_there_are_two_reminders(self) -> None:
        """24h out you can move your day around it; 2h out you need to leave.
        Different questions, so different messages."""
        reminders = [kind for kind in NotificationKind if kind.is_reminder]
        assert reminders == [NotificationKind.REMINDER_24H, NotificationKind.REMINDER_2H]

    def test_the_24h_reminder_lands_a_day_before(self) -> None:
        when = due_at(
            NotificationKind.REMINDER_24H, slot_start=SLOT, slot_end=None, offsets=OFFSETS
        )
        assert when == SLOT - timedelta(hours=24)

    def test_the_2h_reminder_lands_two_hours_before(self) -> None:
        when = due_at(NotificationKind.REMINDER_2H, slot_start=SLOT, slot_end=None, offsets=OFFSETS)
        assert when == SLOT - timedelta(hours=2)

    def test_the_follow_up_lands_after_service(self) -> None:
        end = SLOT + timedelta(hours=1)
        when = due_at(NotificationKind.FOLLOW_UP, slot_start=SLOT, slot_end=end, offsets=OFFSETS)
        assert when == end + timedelta(hours=2)

    def test_satisfaction_waits_a_day_after_service(self) -> None:
        end = SLOT + timedelta(hours=1)
        when = due_at(NotificationKind.SATISFACTION, slot_start=SLOT, slot_end=end, offsets=OFFSETS)
        assert when == end + timedelta(hours=24)

    def test_a_missing_end_time_falls_back_to_the_start(self) -> None:
        """Many upstreams return only a start; treating that as unschedulable
        would silently disable half the feature."""
        when = due_at(NotificationKind.FOLLOW_UP, slot_start=SLOT, slot_end=None, offsets=OFFSETS)
        assert when == SLOT + timedelta(hours=2)

    def test_both_reminders_are_configurable(self) -> None:
        settings = Settings(
            database_url="postgresql+asyncpg://u@127.0.0.1:5432/d",
            notify_reminder_24h_before=48.0,
            notify_reminder_2h_before=0.5,
        )
        offsets = Offsets.from_settings(settings)
        assert offsets.reminder_24h_before == 48.0
        assert offsets.reminder_2h_before == 0.5
        assert due_at(
            NotificationKind.REMINDER_24H, slot_start=SLOT, slot_end=None, offsets=offsets
        ) == SLOT - timedelta(hours=48)

    def test_every_kind_can_be_placed(self) -> None:
        for kind in NotificationKind:
            assert due_at(kind, slot_start=SLOT, slot_end=None, offsets=OFFSETS) is not None, (
                f"{kind} has no offset"
            )

    def test_a_negative_grace_is_refused(self) -> None:
        with pytest.raises(ValueError, match="grace_hours"):
            Offsets(grace_hours=-1)


# ---------------------------------------------------------------------------
# The send ladder — template, then session message, then deferred
# ---------------------------------------------------------------------------


class TestTheSendLadder:
    def _decide(
        self,
        *,
        last_inbound_at: datetime | None,
        template: Template | None = None,
        kind: NotificationKind = NotificationKind.REMINDER_24H,
    ) -> Plan:
        due = SLOT - timedelta(hours=24)
        return decide(
            kind,
            now=due,
            due=due,
            last_inbound_at=last_inbound_at,
            offsets=OFFSETS,
            template=template,
        )

    def test_an_approved_template_wins_regardless_of_the_window(self) -> None:
        """Preferred because it does not depend on the consumer having messaged
        recently — which a 24-hour reminder, by definition, cannot rely on."""
        approved = Template(
            name="booking_reminder_24h",
            phrasebank_key="notify.reminder_24h",
            locale="en",
            status=TemplateStatus.APPROVED,
        )
        plan = self._decide(last_inbound_at=SLOT - timedelta(days=5), template=approved)
        assert plan.verdict is Verdict.SEND_TEMPLATE
        assert plan.sends

    def test_no_template_but_an_open_window_degrades_to_a_session_message(self) -> None:
        """The degradation the brief asks for: an unapproved template costs
        nothing while the consumer is still in session."""
        plan = self._decide(last_inbound_at=SLOT - timedelta(hours=25))
        assert plan.verdict is Verdict.SEND_SESSION
        assert plan.sends
        assert "session message" in plan.reason

    def test_no_template_and_a_closed_window_defers(self) -> None:
        plan = self._decide(last_inbound_at=SLOT - timedelta(days=5))
        assert plan.verdict is Verdict.DEFERRED_NO_TEMPLATE
        assert not plan.sends
        assert "not recorded as sent" in plan.reason

    def test_a_pending_template_is_not_an_approved_one(self) -> None:
        pending = Template(
            name="booking_reminder_24h",
            phrasebank_key="notify.reminder_24h",
            locale="en",
            status=TemplateStatus.PENDING,
        )
        plan = self._decide(last_inbound_at=SLOT - timedelta(days=5), template=pending)
        assert plan.verdict is Verdict.DEFERRED_NO_TEMPLATE

    def test_a_rejected_template_is_not_an_approved_one(self) -> None:
        rejected = Template(
            name="booking_reminder_24h",
            phrasebank_key="notify.reminder_24h",
            locale="en",
            status=TemplateStatus.REJECTED,
        )
        plan = self._decide(last_inbound_at=SLOT - timedelta(days=5), template=rejected)
        assert plan.verdict is Verdict.DEFERRED_NO_TEMPLATE

    def test_a_consumer_never_heard_from_has_no_window(self) -> None:
        assert not window_is_open(last_inbound_at=None, now=SLOT, offsets=OFFSETS)

    def test_the_window_is_configurable(self) -> None:
        short = Offsets(session_window_hours=1.0)
        assert not window_is_open(
            last_inbound_at=SLOT - timedelta(hours=2), now=SLOT, offsets=short
        )

    def test_before_its_moment_it_is_not_yet(self) -> None:
        due = SLOT - timedelta(hours=24)
        plan = decide(
            NotificationKind.REMINDER_24H,
            now=due - timedelta(hours=1),
            due=due,
            last_inbound_at=due,
            offsets=OFFSETS,
            template=None,
        )
        assert plan.verdict is Verdict.NOT_YET

    def test_past_the_grace_period_it_expires(self) -> None:
        due = SLOT - timedelta(hours=24)
        plan = decide(
            NotificationKind.REMINDER_24H,
            now=due + timedelta(hours=5),
            due=due,
            last_inbound_at=due,
            offsets=OFFSETS,
            template=None,
        )
        assert plan.verdict is Verdict.EXPIRED
        assert plan.verdict.is_terminal

    def test_a_cancelled_booking_beats_everything(self) -> None:
        due = SLOT - timedelta(hours=24)
        plan = decide(
            NotificationKind.REMINDER_24H,
            now=due,
            due=due,
            last_inbound_at=None,
            offsets=OFFSETS,
            template=None,
            cancelled=True,
        )
        assert plan.verdict is Verdict.CANCELLED

    def test_naive_datetimes_do_not_explode(self) -> None:
        plan = decide(
            NotificationKind.REMINDER_2H,
            now=datetime(2026, 7, 31, 11, 0),
            due=datetime(2026, 7, 31, 11, 0),
            last_inbound_at=datetime(2026, 7, 31, 10, 0),
            offsets=OFFSETS,
            template=None,
        )
        assert plan.verdict is Verdict.SEND_SESSION


# ---------------------------------------------------------------------------
# Templates: names from config, approval from config
# ---------------------------------------------------------------------------


class TestTemplatesAreConfiguration:
    def test_names_come_from_settings_not_from_code(self) -> None:
        """A template's registered name is chosen in a portal on a day nobody
        remembers. If a resubmission needs a new name, that must not be a deploy."""
        settings = Settings(
            database_url="postgresql+asyncpg://u@127.0.0.1:5432/d",
            template_name_reminder_24h="mawjood_reminder_en_v2",
        )
        template = build_template_registry(settings).get(NotificationKind.REMINDER_24H, "en")
        assert template is not None
        assert template.name == "mawjood_reminder_en_v2"

    def test_approval_comes_from_settings(self) -> None:
        settings = Settings(
            database_url="postgresql+asyncpg://u@127.0.0.1:5432/d",
            approved_templates=("booking_reminder_24h",),
        )
        registry = build_template_registry(settings)
        assert registry.approved(NotificationKind.REMINDER_24H, "en") is not None
        assert registry.approved(NotificationKind.REMINDER_2H, "en") is None

    def test_approval_follows_a_renamed_template(self) -> None:
        """The approved list names templates, so a rename must carry through."""
        settings = Settings(
            database_url="postgresql+asyncpg://u@127.0.0.1:5432/d",
            template_name_reminder_24h="mawjood_reminder_en_v2",
            approved_templates=("mawjood_reminder_en_v2",),
        )
        assert (
            build_template_registry(settings).approved(NotificationKind.REMINDER_24H, "en")
            is not None
        )

    def test_nothing_is_approved_by_default(self) -> None:
        """If this fails, either approval landed or someone faked it."""
        assert not build_template_registry().any_approved

    @pytest.mark.parametrize("kind", list(NotificationKind))
    def test_every_kind_has_a_declared_template(self, kind: NotificationKind) -> None:
        """How we find out a new kind was added with no route out of the window."""
        assert build_template_registry().get(kind, "en") is not None

    @pytest.mark.parametrize("kind", list(NotificationKind))
    def test_every_template_mirrors_a_real_phrasebank_entry(self, kind: NotificationKind) -> None:
        from mawjood.core.conversation.phrasebank import get_phrasebank

        template = build_template_registry().get(kind, "en")
        assert template is not None
        rendered = get_phrasebank().render(
            template.phrasebank_key, "en", venue="X", day="tomorrow", time="5 pm"
        )
        assert rendered.text

    def test_no_locale_fallback(self) -> None:
        """Sending English to an Arabic consumer because English was approved
        first is exactly the kind of helpfulness nobody wants."""
        assert build_template_registry().get(NotificationKind.REMINDER_24H, "ar") is None


class TestNotificationCopy:
    def test_no_scheduled_copy_carries_failure_language(self) -> None:
        from mawjood.core.conversation.phrasebank import get_phrasebank
        from tests.cascade.test_no_empty_shelves import scan

        phrasebank = get_phrasebank()
        for key in PHRASE_FOR.values():
            text = phrasebank.render(key, "en", venue="Marina", day="tomorrow", time="5 pm").text
            assert not scan(text), f"{key}: {scan(text)}"

    def test_the_copy_is_not_chirpy(self) -> None:
        """Mawjood's voice, at the point it is easiest to lose. An unprompted
        message is an interruption; it earns its place by being useful."""
        from mawjood.core.conversation.phrasebank import get_phrasebank

        phrasebank = get_phrasebank()
        for key in PHRASE_FOR.values():
            text = phrasebank.render(key, "en", venue="X", day="tomorrow", time="5 pm").text
            assert "!" not in text, f"{key} has an exclamation mark"

    def test_the_two_reminders_read_differently(self) -> None:
        """Two identical reminders four hours apart is spam wearing a schedule."""
        from mawjood.core.conversation.phrasebank import get_phrasebank

        phrasebank = get_phrasebank()
        first = phrasebank.render(
            "notify.reminder_24h", "en", venue="X", day="tomorrow", time="5 pm"
        )
        second = phrasebank.render("notify.reminder_2h", "en", venue="X", time="5 pm")
        assert first.text != second.text


# ---------------------------------------------------------------------------
# Satisfaction capture: rating and comment
# ---------------------------------------------------------------------------


class TestParsingSatisfaction:
    @pytest.mark.parametrize(
        ("text", "rating"),
        [
            ("4", 4),
            ("5", 5),
            ("1", 1),
            ("4/5", 4),
            ("3 out of 5", 3),
            ("I'd give it a 4", 4),
            ("rate it 2", 2),
        ],
    )
    def test_a_rating_is_found(self, text: str, rating: int) -> None:
        assert parse_satisfaction(text).rating == rating

    def test_the_comment_is_kept_alongside_the_rating(self) -> None:
        """The number is what an operator averages; the sentence is why. A
        1-to-5 scale throws the second away, which is the whole reason the
        free-text field exists."""
        parsed = parse_satisfaction("4 - staff were lovely but the wait was long")
        assert parsed.rating == 4
        assert parsed.comment == "staff were lovely but the wait was long"

    def test_a_bare_rating_has_no_comment(self) -> None:
        parsed = parse_satisfaction("5")
        assert parsed.rating == 5
        assert parsed.comment is None

    def test_a_message_with_no_rating_is_not_feedback(self) -> None:
        parsed = parse_satisfaction("thanks, that was great")
        assert parsed.rating is None
        assert not parsed.is_feedback

    def test_an_empty_message_is_not_feedback(self) -> None:
        assert not parse_satisfaction("").is_feedback
        assert not parse_satisfaction("   ").is_feedback

    def test_out_of_range_numbers_are_not_ratings(self) -> None:
        assert parse_satisfaction("9").rating is None
        assert parse_satisfaction("0").rating is None


# ---------------------------------------------------------------------------
# Persistence, against a real database
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Seeded:
    lead_id: uuid.UUID
    conversation_id: uuid.UUID
    booking_id: uuid.UUID


async def seed_booking(
    session: AsyncSession,
    tenant_id: uuid.UUID,
    *,
    slot_start: datetime,
    status: BookingStatus = BookingStatus.CONFIRMED,
    wa_id: str = "971501234567",
) -> Seeded:
    lead = Lead(tenant_id=tenant_id, wa_id=wa_id, locale="en")
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
    return Seeded(lead.id, conversation.id, booking.id)


async def add_inbound(
    session: AsyncSession, tenant_id: uuid.UUID, ids: Seeded, *, at: datetime
) -> None:
    """A consumer message at a chosen time. The session window starts here."""
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
class TestPersistentScheduling:
    """Rows, not timers. A restart must lose nothing."""

    async def test_confirming_a_booking_writes_the_whole_schedule(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        tenant_id: uuid.UUID,
    ) -> None:
        async with session_factory() as session:
            ids = await seed_booking(session, tenant_id, slot_start=SLOT)
            booking = await session.get(Booking, ids.booking_id)
            assert booking is not None
            await schedule_for_booking(session, tenant_id=tenant_id, booking=booking)
            await session.commit()

        async with session_factory() as session:
            rows = (await session.execute(select(ScheduledNotification))).scalars().all()

        assert {row.kind for row in rows} == set(NotificationKind)
        assert all(row.status is NotificationStatus.PENDING for row in rows)

    async def test_the_schedule_survives_a_restart(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        tenant_id: uuid.UUID,
    ) -> None:
        """The claim in one test: everything in memory goes away, and the
        schedule is still there and still correct."""
        async with session_factory() as session:
            ids = await seed_booking(session, tenant_id, slot_start=SLOT)
            booking = await session.get(Booking, ids.booking_id)
            assert booking is not None
            await schedule_for_booking(session, tenant_id=tenant_id, booking=booking)
            await session.commit()

        # A wholly new session and a wholly new Scheduler — nothing carried over.
        async with session_factory() as session:
            planned = await Scheduler(session, tenant_id=tenant_id).pending(
                now=SLOT - timedelta(days=3)
            )

        assert len(planned) == len(NotificationKind)
        by_kind = {item.row.kind: item.row.due_at for item in planned}
        assert by_kind[NotificationKind.REMINDER_24H] == SLOT - timedelta(hours=24)
        assert by_kind[NotificationKind.REMINDER_2H] == SLOT - timedelta(hours=2)

    async def test_a_booking_with_no_slot_time_schedules_nothing(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        tenant_id: uuid.UUID,
    ) -> None:
        """No anchor to measure from. Inventing one would send a reminder at a
        moment nobody chose."""
        async with session_factory() as session:
            ids = await seed_booking(session, tenant_id, slot_start=SLOT)
            booking = await session.get(Booking, ids.booking_id)
            assert booking is not None
            booking.slot_start = None
            created = await schedule_for_booking(session, tenant_id=tenant_id, booking=booking)
            assert created == []

    async def test_the_pipeline_schedules_on_a_real_booking(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        db_settings: Settings,
        tenant_id: uuid.UUID,
        routed: RoutingSeeder,
    ) -> None:
        """End to end: a consumer books, and the reminders exist."""
        from mawjood.core.conversation.pipeline import handle_inbound
        from mawjood.core.conversation.types import InboundMessage

        await routed(Category.SALON, "fake_happy")

        for text in ("hi", "yes", "need a haircut in Marina tomorrow at 5pm", "yes"):
            async with session_factory() as session:
                await handle_inbound(
                    session,
                    InboundMessage(
                        tenant_slug=db_settings.default_tenant_slug,
                        channel=Channel.WHATSAPP,
                        wa_id="971509998888",
                        text=text,
                    ),
                    correlation_id="sched",
                    settings=db_settings,
                )

        async with session_factory() as session:
            bookings = (await session.execute(select(Booking))).scalars().all()
            rows = (await session.execute(select(ScheduledNotification))).scalars().all()

        assert len(bookings) == 1, "the booking did not complete"
        assert {row.kind for row in rows} == set(NotificationKind)


@pytest.mark.integration
@needs_database
class TestIdempotency:
    """Never double-remind. Enforced by the schema, not by a remembered query."""

    async def test_a_duplicate_row_cannot_be_inserted(
        self, session_factory: async_sessionmaker[AsyncSession], tenant_id: uuid.UUID
    ) -> None:
        """Tried directly against the constraint rather than through the code
        path, because the guarantee is the constraint."""
        async with session_factory() as session:
            ids = await seed_booking(session, tenant_id, slot_start=SLOT)
            booking = await session.get(Booking, ids.booking_id)
            assert booking is not None
            await schedule_for_booking(session, tenant_id=tenant_id, booking=booking)
            await session.commit()

        async with session_factory() as session:
            session.add(
                ScheduledNotification(
                    tenant_id=tenant_id,
                    booking_id=ids.booking_id,
                    conversation_id=ids.conversation_id,
                    lead_id=ids.lead_id,
                    kind=NotificationKind.REMINDER_24H,
                    due_at=SLOT,
                )
            )
            with pytest.raises(IntegrityError):
                await session.commit()

    async def test_scheduling_twice_is_a_no_op(
        self, session_factory: async_sessionmaker[AsyncSession], tenant_id: uuid.UUID
    ) -> None:
        async with session_factory() as session:
            ids = await seed_booking(session, tenant_id, slot_start=SLOT)
            booking = await session.get(Booking, ids.booking_id)
            assert booking is not None
            await schedule_for_booking(session, tenant_id=tenant_id, booking=booking)
            await schedule_for_booking(session, tenant_id=tenant_id, booking=booking)
            await session.commit()

        async with session_factory() as session:
            rows = (await session.execute(select(ScheduledNotification))).scalars().all()
        assert len(rows) == len(NotificationKind)

    async def test_running_the_scheduler_repeatedly_sends_once(
        self, session_factory: async_sessionmaker[AsyncSession], tenant_id: uuid.UUID
    ) -> None:
        """A five-minute timer must not send five reminders."""
        async with session_factory() as session:
            ids = await seed_booking(session, tenant_id, slot_start=SLOT)
            booking = await session.get(Booking, ids.booking_id)
            assert booking is not None
            await schedule_for_booking(session, tenant_id=tenant_id, booking=booking)
            await add_inbound(session, tenant_id, ids, at=SLOT - timedelta(hours=25))
            await session.commit()

        for _ in range(3):
            async with session_factory() as session:
                await Scheduler(session, tenant_id=tenant_id).run(now=SLOT - timedelta(hours=24))
                await session.commit()

        async with session_factory() as session:
            sent = await outbound_with_key(session, PHRASE_FOR[NotificationKind.REMINDER_24H])
        assert len(sent) == 1


@pytest.mark.integration
@needs_database
class TestRunningTheSchedule:
    async def _prepare(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        tenant_id: uuid.UUID,
        *,
        last_inbound: datetime,
    ) -> Seeded:
        async with session_factory() as session:
            ids = await seed_booking(session, tenant_id, slot_start=SLOT)
            booking = await session.get(Booking, ids.booking_id)
            assert booking is not None
            await schedule_for_booking(session, tenant_id=tenant_id, booking=booking)
            await add_inbound(session, tenant_id, ids, at=last_inbound)
            await session.commit()
        return ids

    async def test_an_open_window_sends_a_session_message(
        self, session_factory: async_sessionmaker[AsyncSession], tenant_id: uuid.UUID
    ) -> None:
        await self._prepare(session_factory, tenant_id, last_inbound=SLOT - timedelta(hours=25))

        async with session_factory() as session:
            acted = await Scheduler(session, tenant_id=tenant_id).run(
                now=SLOT - timedelta(hours=24)
            )
            await session.commit()

        assert [a.plan.verdict for a in acted] == [Verdict.SEND_SESSION]
        async with session_factory() as session:
            rows = (
                (
                    await session.execute(
                        select(ScheduledNotification).where(
                            ScheduledNotification.kind == NotificationKind.REMINDER_24H
                        )
                    )
                )
                .scalars()
                .all()
            )
        assert rows[0].status is NotificationStatus.SENT
        assert rows[0].sent_at is not None
        assert rows[0].message_id is not None
        assert rows[0].template_name is None

    async def test_a_closed_window_defers_and_writes_no_message(
        self, session_factory: async_sessionmaker[AsyncSession], tenant_id: uuid.UUID
    ) -> None:
        """The assertion the whole design turns on: held, not sent, and above all
        not *recorded* as sent."""
        await self._prepare(session_factory, tenant_id, last_inbound=SLOT - timedelta(days=5))

        async with session_factory() as session:
            acted = await Scheduler(session, tenant_id=tenant_id).run(
                now=SLOT - timedelta(hours=24)
            )
            await session.commit()

        assert [a.plan.verdict for a in acted] == [Verdict.DEFERRED_NO_TEMPLATE]
        async with session_factory() as session:
            row = (
                (
                    await session.execute(
                        select(ScheduledNotification).where(
                            ScheduledNotification.kind == NotificationKind.REMINDER_24H
                        )
                    )
                )
                .scalars()
                .one()
            )
            messages = await outbound_with_key(session, PHRASE_FOR[NotificationKind.REMINDER_24H])

        assert row.status is NotificationStatus.DEFERRED
        assert row.sent_at is None
        assert row.message_id is None
        assert row.reason and "session window" in row.reason
        assert messages == [], "a deferred notification wrote a message the consumer never got"

    async def test_a_deferred_notification_is_retried_and_released_by_approval(
        self, session_factory: async_sessionmaker[AsyncSession], tenant_id: uuid.UUID
    ) -> None:
        """What unblocks a deferral is an approval landing, so it must stay in
        the queue and go out the moment one does."""
        await self._prepare(session_factory, tenant_id, last_inbound=SLOT - timedelta(days=5))

        async with session_factory() as session:
            await Scheduler(session, tenant_id=tenant_id).run(now=SLOT - timedelta(hours=24))
            await session.commit()

        async with session_factory() as session:
            acted = await Scheduler(
                session,
                tenant_id=tenant_id,
                templates=approved_registry(NotificationKind.REMINDER_24H),
            ).run(now=SLOT - timedelta(hours=24))
            await session.commit()

        assert Verdict.SEND_TEMPLATE in [a.plan.verdict for a in acted]
        async with session_factory() as session:
            row = (
                (
                    await session.execute(
                        select(ScheduledNotification).where(
                            ScheduledNotification.kind == NotificationKind.REMINDER_24H
                        )
                    )
                )
                .scalars()
                .one()
            )
        assert row.status is NotificationStatus.SENT
        assert row.template_name == "booking_reminder_24h"
        assert row.attempts == 2

    async def test_a_deferral_is_audited_with_its_reason(
        self, session_factory: async_sessionmaker[AsyncSession], tenant_id: uuid.UUID
    ) -> None:
        await self._prepare(session_factory, tenant_id, last_inbound=SLOT - timedelta(days=5))

        async with session_factory() as session:
            await Scheduler(session, tenant_id=tenant_id).run(now=SLOT - timedelta(hours=24))
            await session.commit()

        async with session_factory() as session:
            events = (
                (
                    await session.execute(
                        select(AuditLog).where(AuditLog.event_type == "notification.deferred")
                    )
                )
                .scalars()
                .all()
            )
        assert events
        assert events[0].details["template_status"] == "not_submitted"

    async def test_a_cancelled_booking_is_skipped_terminally(
        self, session_factory: async_sessionmaker[AsyncSession], tenant_id: uuid.UUID
    ) -> None:
        async with session_factory() as session:
            ids = await seed_booking(session, tenant_id, slot_start=SLOT)
            booking = await session.get(Booking, ids.booking_id)
            assert booking is not None
            await schedule_for_booking(session, tenant_id=tenant_id, booking=booking)
            booking.status = BookingStatus.CANCELLED
            await add_inbound(session, tenant_id, ids, at=SLOT - timedelta(hours=25))
            await session.commit()

        async with session_factory() as session:
            await Scheduler(session, tenant_id=tenant_id).run(now=SLOT - timedelta(hours=24))
            await session.commit()

        async with session_factory() as session:
            rows = (await session.execute(select(ScheduledNotification))).scalars().all()
        assert all(
            row.status is NotificationStatus.SKIPPED
            for row in rows
            if row.kind is NotificationKind.REMINDER_24H
        )
        async with session_factory() as session:
            assert await outbound_with_key(session, PHRASE_FOR[NotificationKind.REMINDER_24H]) == []

    async def test_the_reminder_quotes_a_local_time(
        self, session_factory: async_sessionmaker[AsyncSession], tenant_id: uuid.UUID
    ) -> None:
        """ "5 pm", not "13:00+00:00". A reminder quoting UTC to someone in Dubai
        is worse than no reminder."""
        await self._prepare(session_factory, tenant_id, last_inbound=SLOT - timedelta(hours=25))

        async with session_factory() as session:
            await Scheduler(session, tenant_id=tenant_id).run(now=SLOT - timedelta(hours=24))
            await session.commit()

        async with session_factory() as session:
            body = (await outbound_with_key(session, PHRASE_FOR[NotificationKind.REMINDER_24H]))[
                0
            ].body
        assert "5 pm" in body
        assert "13:00" not in body

    async def test_the_send_goes_through_the_bsp_as_a_rendered_message(
        self, session_factory: async_sessionmaker[AsyncSession], tenant_id: uuid.UUID
    ) -> None:
        """Proves the scheduler cannot smuggle an f-string to a consumer."""
        from mawjood.core.conversation.phrasebank import RenderedMessage

        captured: list[object] = []

        class RecordingBSP:
            slug = "recording"

            async def send(self, outbound: object) -> str:
                captured.append(outbound)
                return "provider-1"

        await self._prepare(session_factory, tenant_id, last_inbound=SLOT - timedelta(hours=25))

        async with session_factory() as session:
            await Scheduler(session, tenant_id=tenant_id).run(
                bsp=RecordingBSP(), now=SLOT - timedelta(hours=24)
            )
            await session.commit()

        assert len(captured) == 1
        assert isinstance(captured[0].message, RenderedMessage)  # type: ignore[attr-defined]

    async def test_the_scheduler_is_tenant_scoped(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        tenant_id: uuid.UUID,
        other_tenant_id: uuid.UUID,
    ) -> None:
        await self._prepare(session_factory, tenant_id, last_inbound=SLOT - timedelta(hours=25))

        async with session_factory() as session:
            acted = await Scheduler(session, tenant_id=other_tenant_id).run(
                now=SLOT - timedelta(hours=24)
            )
        assert acted == []

    async def test_both_reminders_fire_at_their_own_moments(
        self, session_factory: async_sessionmaker[AsyncSession], tenant_id: uuid.UUID
    ) -> None:
        """The point of having two."""
        await self._prepare(session_factory, tenant_id, last_inbound=SLOT - timedelta(hours=25))

        async with session_factory() as session:
            first = await Scheduler(session, tenant_id=tenant_id).run(
                now=SLOT - timedelta(hours=24)
            )
            await session.commit()
        assert [a.row.kind for a in first] == [NotificationKind.REMINDER_24H]

        async with session_factory() as session:
            await add_inbound(
                session,
                tenant_id,
                Seeded(
                    lead_id=(await session.execute(select(Lead))).scalars().one().id,
                    conversation_id=(await session.execute(select(Conversation)))
                    .scalars()
                    .one()
                    .id,
                    booking_id=(await session.execute(select(Booking))).scalars().one().id,
                ),
                at=SLOT - timedelta(hours=3),
            )
            await session.commit()

        async with session_factory() as session:
            second = await Scheduler(session, tenant_id=tenant_id).run(
                now=SLOT - timedelta(hours=2)
            )
            await session.commit()
        assert [a.row.kind for a in second] == [NotificationKind.REMINDER_2H]


@pytest.mark.integration
@needs_database
class TestCapturingSatisfaction:
    async def test_a_rating_and_comment_are_recorded_against_the_booking(
        self, session_factory: async_sessionmaker[AsyncSession], tenant_id: uuid.UUID
    ) -> None:
        async with session_factory() as session:
            ids = await seed_booking(session, tenant_id, slot_start=SLOT)
            await session.commit()

        async with session_factory() as session:
            feedback = await capture_satisfaction(
                session,
                tenant_id=tenant_id,
                conversation_id=ids.conversation_id,
                lead_id=ids.lead_id,
                text="4 - staff were lovely, parking was a nightmare",
            )
            await session.commit()
            assert feedback is not None

        async with session_factory() as session:
            row = (await session.execute(select(Feedback))).scalar_one()
        assert row.rating == 4
        assert row.comment == "staff were lovely, parking was a nightmare"
        assert row.booking_id == ids.booking_id
        assert row.responded_at is not None

    async def test_a_message_with_no_rating_records_nothing(
        self, session_factory: async_sessionmaker[AsyncSession], tenant_id: uuid.UUID
    ) -> None:
        """Scoring an ambiguous message puts noise in the one number an operator
        trusts."""
        async with session_factory() as session:
            ids = await seed_booking(session, tenant_id, slot_start=SLOT)
            await session.commit()

        async with session_factory() as session:
            result = await capture_satisfaction(
                session,
                tenant_id=tenant_id,
                conversation_id=ids.conversation_id,
                lead_id=ids.lead_id,
                text="can you book me another one",
            )
            await session.commit()
        assert result is None

        async with session_factory() as session:
            assert (await session.execute(select(Feedback))).scalars().all() == []

    async def test_a_correction_updates_rather_than_appends(
        self, session_factory: async_sessionmaker[AsyncSession], tenant_id: uuid.UUID
    ) -> None:
        """ "actually make that a 5" must not double-count."""
        async with session_factory() as session:
            ids = await seed_booking(session, tenant_id, slot_start=SLOT)
            await session.commit()

        for text in ("3", "actually make that a 5"):
            async with session_factory() as session:
                await capture_satisfaction(
                    session,
                    tenant_id=tenant_id,
                    conversation_id=ids.conversation_id,
                    lead_id=ids.lead_id,
                    text=text,
                )
                await session.commit()

        async with session_factory() as session:
            rows = (await session.execute(select(Feedback))).scalars().all()
        assert len(rows) == 1
        assert rows[0].rating == 5

    async def test_the_pipeline_only_scores_when_it_asked(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        db_settings: Settings,
        tenant_id: uuid.UUID,
    ) -> None:
        """ "book me 5 people" is a request, not a five-star review — and reading
        it as one loses the booking as well as corrupting the number."""
        from mawjood.core.conversation.pipeline import handle_inbound
        from mawjood.core.conversation.types import InboundMessage

        async with session_factory() as session:
            await handle_inbound(
                session,
                InboundMessage(
                    tenant_slug=db_settings.default_tenant_slug,
                    channel=Channel.WHATSAPP,
                    wa_id="971507770001",
                    text="5",
                ),
                correlation_id="nofeedback",
                settings=db_settings,
            )

        async with session_factory() as session:
            assert (await session.execute(select(Feedback))).scalars().all() == []
