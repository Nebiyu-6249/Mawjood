"""Handoff, end to end: queue → mute → operator reply → release → unmute.

Decision 7 is *queue + console reply + bot mute*. Phase 2 shipped the first two
thirds: the cascade enqueued and the pipeline went quiet. That is a conversation
with no way out. A muted thread with a queue row looks handled on every dashboard
and is, to the consumer, a room with the door shut — which is the invariant
failing in slow motion rather than in one message.

So the test that matters here is the **round trip**, not any single step:

1. every candidate fails → a handoff row exists and Mawjood goes silent;
2. the consumer keeps talking → it is recorded, and Mawjood still says nothing;
3. an operator claims, replies → the consumer hears a human;
4. the operator releases → the mute lifts and Mawjood answers the next message.

Step 4 is the one that was missing, so it is the one asserted hardest.

The cascade is driven by routing rows pointing at fakes that each fail a
different way — empty, auth failure, timeout. Nothing here mocks the pipeline;
the failure is real, it just comes from a double.
"""

from __future__ import annotations

import uuid

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from mawjood.config import Settings
from mawjood.core.conversation.pipeline import handle_inbound
from mawjood.core.conversation.types import InboundMessage
from mawjood.core.enums import (
    Category,
    Channel,
    ConversationStatus,
    Direction,
    HandoffStatus,
)
from mawjood.core.handoff import (
    HandoffAlreadyClaimed,
    HandoffNotFound,
    claim,
    open_queue,
    release,
    reply,
)
from mawjood.db.models import AuditLog, Conversation, HandoffQueue, Message

from .conftest import RoutingSeeder, needs_database

pytestmark = [pytest.mark.integration, needs_database]

# Every candidate fails, each in a different way. The consumer must still never
# be told a shelf is empty — they end up with a person instead.
ALL_DOWN = ("fake_empty", "fake_auth", "fake_timeout")

WA_ID = "971509990001"


def inbound(settings: Settings, text: str, wa_id: str = WA_ID) -> InboundMessage:
    return InboundMessage(
        tenant_slug=settings.default_tenant_slug,
        channel=Channel.WHATSAPP,
        wa_id=wa_id,
        text=text,
    )


async def drive_to_handoff(
    session_factory: async_sessionmaker[AsyncSession], settings: Settings
) -> uuid.UUID:
    """Run a real conversation until the cascade gives up. Returns the handoff id."""
    for text in (
        "hi",
        "yes",
        "i need a haircut in marina tomorrow at 5pm",
    ):
        async with session_factory() as session:
            await handle_inbound(
                session,
                inbound(settings, text),
                correlation_id="handoff-test",
                settings=settings,
            )

    async with session_factory() as session:
        rows = (await session.execute(select(HandoffQueue))).scalars().all()
        # Guard against a hollow green. If the fakes were not registered the
        # candidate list would be empty, the cascade would "exhaust" instantly,
        # and every assertion below would still pass — while testing nothing.
        attempted = {
            row.platform_slug
            for row in (await session.execute(select(AuditLog))).scalars().all()
            if row.platform_slug
        }
    assert rows, "every candidate failed and nobody was queued — the consumer is stranded"
    assert attempted >= set(ALL_DOWN), (
        f"the cascade never called the fakes (attempted={sorted(attempted)}); "
        "this test would pass on an empty candidate list"
    )
    return rows[0].id


class TestTheQueueFills:
    async def test_an_exhausted_cascade_reaches_a_person(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        db_settings: Settings,
        tenant_id: uuid.UUID,
        routed: RoutingSeeder,
    ) -> None:
        await routed(Category.SALON, *ALL_DOWN)
        handoff_id = await drive_to_handoff(session_factory, db_settings)

        async with session_factory() as session:
            handoff = await session.get(HandoffQueue, handoff_id)
            assert handoff is not None
            assert handoff.status is HandoffStatus.OPEN
            assert handoff.claimed_by is None
            # A human picking this up cold needs to know what was already tried.
            assert handoff.context, "a queue row with no context makes an operator start over"

    async def test_the_bot_goes_quiet_once_handed_off(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        db_settings: Settings,
        tenant_id: uuid.UUID,
        routed: RoutingSeeder,
    ) -> None:
        await routed(Category.SALON, *ALL_DOWN)
        await drive_to_handoff(session_factory, db_settings)

        async with session_factory() as session:
            conversation = (await session.execute(select(Conversation))).scalar_one()
            assert conversation.bot_muted is True

    async def test_a_muted_conversation_still_records_what_the_consumer_says(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        db_settings: Settings,
        tenant_id: uuid.UUID,
        routed: RoutingSeeder,
    ) -> None:
        """Silence is not deafness.

        The operator opens the console to a complete thread, including whatever
        the consumer said while waiting.
        """
        await routed(Category.SALON, *ALL_DOWN)
        await drive_to_handoff(session_factory, db_settings)

        async with session_factory() as session:
            turn = await handle_inbound(
                session,
                inbound(db_settings, "are you still there?"),
                correlation_id="c-wait",
                settings=db_settings,
            )
        assert turn.outbound == (), "Mawjood answered over the top of a colleague"

        async with session_factory() as session:
            bodies = [
                row.body
                for row in (
                    await session.execute(
                        select(Message)
                        .where(Message.direction == Direction.INBOUND)
                        .order_by(Message.seq)
                    )
                )
                .scalars()
                .all()
            ]
        assert "are you still there?" in bodies

    async def test_the_consumer_was_never_told_anything_failed(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        db_settings: Settings,
        tenant_id: uuid.UUID,
        routed: RoutingSeeder,
    ) -> None:
        """The invariant, at the one moment it is under most pressure.

        Three platforms refused and the consumer is on their way to a human. That
        is the closest this system gets to an empty shelf, so it is where the
        blocklist has to hold.
        """
        from tests.cascade.test_no_empty_shelves import scan

        await routed(Category.SALON, *ALL_DOWN)
        await drive_to_handoff(session_factory, db_settings)

        async with session_factory() as session:
            outbound = (
                (
                    await session.execute(
                        select(Message).where(Message.direction == Direction.OUTBOUND)
                    )
                )
                .scalars()
                .all()
            )

        assert outbound
        for message in outbound:
            hits = scan(message.body)
            assert not hits, f"failure language reached the consumer: {hits} in {message.body!r}"


class TestClaiming:
    async def test_claiming_names_the_operator(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        db_settings: Settings,
        tenant_id: uuid.UUID,
        routed: RoutingSeeder,
    ) -> None:
        await routed(Category.SALON, *ALL_DOWN)
        handoff_id = await drive_to_handoff(session_factory, db_settings)

        async with session_factory() as session:
            await claim(session, tenant_id=tenant_id, handoff_id=handoff_id, operator="dana")
            await session.commit()

        async with session_factory() as session:
            handoff = await session.get(HandoffQueue, handoff_id)
            assert handoff is not None
            assert handoff.status is HandoffStatus.CLAIMED
            assert handoff.claimed_by == "dana"
            assert handoff.claimed_at is not None

    async def test_a_second_operator_is_refused(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        db_settings: Settings,
        tenant_id: uuid.UUID,
        routed: RoutingSeeder,
    ) -> None:
        """Two operators replying to one consumer is worse than one waiting."""
        await routed(Category.SALON, *ALL_DOWN)
        handoff_id = await drive_to_handoff(session_factory, db_settings)

        async with session_factory() as session:
            await claim(session, tenant_id=tenant_id, handoff_id=handoff_id, operator="dana")
            await session.commit()

        async with session_factory() as session:
            with pytest.raises(HandoffAlreadyClaimed):
                await claim(session, tenant_id=tenant_id, handoff_id=handoff_id, operator="omar")

    async def test_reclaiming_by_the_same_operator_is_fine(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        db_settings: Settings,
        tenant_id: uuid.UUID,
        routed: RoutingSeeder,
    ) -> None:
        """A refreshed browser tab must not lock someone out of their own case."""
        await routed(Category.SALON, *ALL_DOWN)
        handoff_id = await drive_to_handoff(session_factory, db_settings)

        async with session_factory() as session:
            await claim(session, tenant_id=tenant_id, handoff_id=handoff_id, operator="dana")
            await claim(session, tenant_id=tenant_id, handoff_id=handoff_id, operator="dana")
            await session.commit()

    async def test_another_tenants_handoff_is_not_found(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        db_settings: Settings,
        tenant_id: uuid.UUID,
        other_tenant_id: uuid.UUID,
        routed: RoutingSeeder,
    ) -> None:
        await routed(Category.SALON, *ALL_DOWN)
        handoff_id = await drive_to_handoff(session_factory, db_settings)

        async with session_factory() as session:
            with pytest.raises(HandoffNotFound):
                await claim(
                    session, tenant_id=other_tenant_id, handoff_id=handoff_id, operator="intruder"
                )


class TestReplying:
    async def test_an_unclaimed_handoff_cannot_be_replied_to(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        db_settings: Settings,
        tenant_id: uuid.UUID,
        routed: RoutingSeeder,
    ) -> None:
        await routed(Category.SALON, *ALL_DOWN)
        handoff_id = await drive_to_handoff(session_factory, db_settings)

        async with session_factory() as session:
            with pytest.raises(HandoffAlreadyClaimed):
                await reply(
                    session,
                    tenant_id=tenant_id,
                    handoff_id=handoff_id,
                    operator="dana",
                    text="hello",
                )

    async def test_an_empty_reply_is_refused(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        db_settings: Settings,
        tenant_id: uuid.UUID,
        routed: RoutingSeeder,
    ) -> None:
        await routed(Category.SALON, *ALL_DOWN)
        handoff_id = await drive_to_handoff(session_factory, db_settings)

        async with session_factory() as session:
            await claim(session, tenant_id=tenant_id, handoff_id=handoff_id, operator="dana")
            with pytest.raises(ValueError, match="cannot be empty"):
                await reply(
                    session,
                    tenant_id=tenant_id,
                    handoff_id=handoff_id,
                    operator="dana",
                    text="   ",
                )

    async def test_the_operators_words_join_the_same_transcript(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        db_settings: Settings,
        tenant_id: uuid.UUID,
        routed: RoutingSeeder,
    ) -> None:
        """One thread, not two half-conversations."""
        await routed(Category.SALON, *ALL_DOWN)
        handoff_id = await drive_to_handoff(session_factory, db_settings)

        async with session_factory() as session:
            await claim(session, tenant_id=tenant_id, handoff_id=handoff_id, operator="dana")
            message = await reply(
                session,
                tenant_id=tenant_id,
                handoff_id=handoff_id,
                operator="dana",
                text="Dana here — I've got you a 5.30 at Marina Beauty Lounge.",
                correlation_id="console-1",
            )
            await session.commit()
            message_id = message.id

        async with session_factory() as session:
            row = await session.get(Message, message_id)
            assert row is not None
            assert row.direction is Direction.OUTBOUND
            # A human wrote this, and the record says so rather than pretending
            # it came from the phrasebank.
            assert row.phrasebank_key == "operator.reply"
            assert row.body.startswith("Dana here")

    async def test_the_operator_reply_is_audited_as_consumer_visible(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        db_settings: Settings,
        tenant_id: uuid.UUID,
        routed: RoutingSeeder,
    ) -> None:
        await routed(Category.SALON, *ALL_DOWN)
        handoff_id = await drive_to_handoff(session_factory, db_settings)

        async with session_factory() as session:
            await claim(session, tenant_id=tenant_id, handoff_id=handoff_id, operator="dana")
            await reply(
                session,
                tenant_id=tenant_id,
                handoff_id=handoff_id,
                operator="dana",
                text="On it.",
            )
            await session.commit()

        async with session_factory() as session:
            events = (
                (
                    await session.execute(
                        select(AuditLog)
                        .where(AuditLog.event_type == "handoff.operator_replied")
                        .order_by(AuditLog.seq)
                    )
                )
                .scalars()
                .all()
            )
        assert len(events) == 1
        assert events[0].consumer_visible is True
        assert events[0].consumer_text == "On it."
        assert events[0].details["operator"] == "dana"


class TestRelease:
    """The half decision 7 was missing."""

    async def test_release_closes_the_queue_row(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        db_settings: Settings,
        tenant_id: uuid.UUID,
        routed: RoutingSeeder,
    ) -> None:
        await routed(Category.SALON, *ALL_DOWN)
        handoff_id = await drive_to_handoff(session_factory, db_settings)

        async with session_factory() as session:
            await claim(session, tenant_id=tenant_id, handoff_id=handoff_id, operator="dana")
            await release(
                session,
                tenant_id=tenant_id,
                handoff_id=handoff_id,
                operator="dana",
                notes="booked by phone",
            )
            await session.commit()

        async with session_factory() as session:
            handoff = await session.get(HandoffQueue, handoff_id)
            assert handoff is not None
            assert handoff.status is HandoffStatus.RESOLVED
            assert handoff.resolved_at is not None
            assert handoff.notes == "booked by phone"
            assert await open_queue(session, tenant_id) == []

    async def test_release_unmutes_and_mawjood_answers_the_next_message(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        db_settings: Settings,
        tenant_id: uuid.UUID,
        routed: RoutingSeeder,
    ) -> None:
        """The assertion this whole file exists for.

        Before release, an inbound message gets silence. After release, the same
        message gets an answer. Without this the mute is a one-way door.
        """
        await routed(Category.SALON, *ALL_DOWN)
        handoff_id = await drive_to_handoff(session_factory, db_settings)

        async with session_factory() as session:
            silent = await handle_inbound(
                session,
                inbound(db_settings, "hello?"),
                correlation_id="c-muted",
                settings=db_settings,
            )
        assert silent.outbound == ()

        async with session_factory() as session:
            await claim(session, tenant_id=tenant_id, handoff_id=handoff_id, operator="dana")
            await release(session, tenant_id=tenant_id, handoff_id=handoff_id, operator="dana")
            await session.commit()

        async with session_factory() as session:
            conversation = (await session.execute(select(Conversation))).scalar_one()
            assert conversation.bot_muted is False
            assert conversation.status is ConversationStatus.ACTIVE

        async with session_factory() as session:
            resumed = await handle_inbound(
                session,
                inbound(db_settings, "hello?"),
                correlation_id="c-released",
                settings=db_settings,
            )
        assert resumed.outbound, "released the conversation and Mawjood stayed silent"

    async def test_release_without_a_claim_still_works(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        db_settings: Settings,
        tenant_id: uuid.UUID,
        routed: RoutingSeeder,
    ) -> None:
        """A stuck conversation must be rescuable even if nobody claimed it.

        Requiring a claim first would mean an abandoned queue row keeps a
        consumer muted until someone performs a ceremony.
        """
        await routed(Category.SALON, *ALL_DOWN)
        handoff_id = await drive_to_handoff(session_factory, db_settings)

        async with session_factory() as session:
            await release(session, tenant_id=tenant_id, handoff_id=handoff_id, operator="dana")
            await session.commit()

        async with session_factory() as session:
            conversation = (await session.execute(select(Conversation))).scalar_one()
            assert conversation.bot_muted is False


class TestTheQueueOrder:
    async def test_priority_first_then_oldest(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        db_settings: Settings,
        tenant_id: uuid.UUID,
        routed: RoutingSeeder,
    ) -> None:
        """Oldest-first within a priority band, so an ordinary consumer who has
        been waiting longest is not starved by a steady trickle of new arrivals."""
        await routed(Category.SALON, *ALL_DOWN)

        for suffix in ("0002", "0003", "0004"):
            for text in ("hi", "yes", "haircut in marina tomorrow at 5pm"):
                async with session_factory() as session:
                    await handle_inbound(
                        session,
                        inbound(db_settings, text, wa_id=f"97150999{suffix}"),
                        correlation_id=f"c-{suffix}",
                        settings=db_settings,
                    )

        async with session_factory() as session:
            rows = (
                (await session.execute(select(HandoffQueue).order_by(HandoffQueue.created_at)))
                .scalars()
                .all()
            )
            assert len(rows) == 3
            # Promote the newest. It should jump the queue; the other two keep
            # their arrival order behind it.
            rows[-1].priority = 10
            newest_id = rows[-1].id
            oldest_id = rows[0].id
            await session.commit()

        async with session_factory() as session:
            queue = await open_queue(session, tenant_id)
        assert [view.handoff.id for view in queue][:2] == [newest_id, oldest_id]
