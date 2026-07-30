"""The inbound pipeline: persistence, consent capture and idempotency."""

from __future__ import annotations

import uuid

import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from mawjood.config import Settings
from mawjood.core.conversation.consent import ConsentState
from mawjood.core.conversation.phrasebank import PhraseKey, get_phrasebank
from mawjood.core.conversation.pipeline import UnknownTenant, handle_inbound
from mawjood.core.conversation.types import InboundMessage
from mawjood.core.enums import Channel, ConsentEvent, Direction
from mawjood.db.models import Consent, Conversation, Lead, Message
from mawjood.db.repositories.core import ConsentRepository, LeadRepository

from .conftest import needs_database

pytestmark = [pytest.mark.integration, needs_database]


def inbound(
    settings: Settings, wa_id: str, text: str, message_id: str | None = None
) -> InboundMessage:
    return InboundMessage(
        tenant_slug=settings.default_tenant_slug,
        channel=Channel.WHATSAPP,
        wa_id=wa_id,
        text=text,
        provider_message_id=message_id,
    )


class TestFirstContact:
    async def test_a_first_message_persists_lead_conversation_and_consent(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        db_settings: Settings,
        tenant_id: uuid.UUID,
    ) -> None:
        async with session_factory() as session:
            turn = await handle_inbound(
                session, inbound(db_settings, "971501111111", "hi"), correlation_id="c1"
            )

        assert turn.lead_id is not None
        assert turn.conversation_id is not None
        assert len(turn.outbound) == 2

        async with session_factory() as session:
            assert await _count(session, Lead) == 1
            assert await _count(session, Conversation) == 1
            # One inbound plus two outbound.
            assert await _count(session, Message) == 3
            assert await _count(session, Consent) == 1

    async def test_the_first_reply_greets_and_shows_the_notice(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        db_settings: Settings,
        tenant_id: uuid.UUID,
    ) -> None:
        async with session_factory() as session:
            turn = await handle_inbound(
                session, inbound(db_settings, "971501111112", "hi"), correlation_id="c1"
            )
        keys = [message.phrasebank_key for message in turn.outbound]
        assert keys == [PhraseKey.GREETING_WELCOME, PhraseKey.CONSENT_NOTICE]

    async def test_the_exact_wording_shown_is_stored(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        db_settings: Settings,
        tenant_id: uuid.UUID,
    ) -> None:
        """PDPL asks what the consumer was shown. The only reliable answer is
        the string that actually went out."""
        async with session_factory() as session:
            turn = await handle_inbound(
                session, inbound(db_settings, "971501111113", "hi"), correlation_id="c1"
            )
        notice_sent = next(
            m.text for m in turn.outbound if m.phrasebank_key == PhraseKey.CONSENT_NOTICE
        )

        async with session_factory() as session:
            consent = (await session.execute(select(Consent))).scalar_one()

        assert consent.event == ConsentEvent.NOTICE_SHOWN
        assert consent.wording == notice_sent
        assert consent.policy_version == get_phrasebank().policy_version("en")
        assert consent.locale == "en"
        assert consent.occurred_at is not None

    async def test_outbound_messages_all_name_a_phrasebank_key(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        db_settings: Settings,
        tenant_id: uuid.UUID,
    ) -> None:
        async with session_factory() as session:
            await handle_inbound(
                session, inbound(db_settings, "971501111114", "hi"), correlation_id="c1"
            )
        async with session_factory() as session:
            rows = (
                (
                    await session.execute(
                        select(Message).where(Message.direction == Direction.OUTBOUND)
                    )
                )
                .scalars()
                .all()
            )
        assert rows
        assert all(row.phrasebank_key for row in rows)


class TestConsentJourney:
    async def test_yes_after_the_notice_records_a_grant(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        db_settings: Settings,
        tenant_id: uuid.UUID,
    ) -> None:
        wa_id = "971502222222"
        async with session_factory() as session:
            await handle_inbound(session, inbound(db_settings, wa_id, "hi"), correlation_id="c1")
        async with session_factory() as session:
            turn = await handle_inbound(
                session, inbound(db_settings, wa_id, "yes"), correlation_id="c2"
            )

        # Phase 2 replaced the scripted responder with the state machine, so the
        # reply now moves the conversation forward rather than only acknowledging
        # the notice. What matters is unchanged: the grant is recorded.
        assert [m.phrasebank_key for m in turn.outbound] == [PhraseKey.ASK_SERVICE]

        async with session_factory() as session:
            lead = await LeadRepository(session, tenant_id).get_by_wa_id(wa_id)
            assert lead is not None
            consents = ConsentRepository(session, tenant_id)
            assert await consents.state_for_lead(lead.id) is ConsentState.GRANTED
            assert await consents.is_granted(lead.id) is True

    async def test_stop_records_a_withdrawal_and_revokes_the_grant(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        db_settings: Settings,
        tenant_id: uuid.UUID,
    ) -> None:
        wa_id = "971503333333"
        for text in ("hi", "yes"):
            async with session_factory() as session:
                await handle_inbound(session, inbound(db_settings, wa_id, text), correlation_id="c")

        async with session_factory() as session:
            turn = await handle_inbound(
                session, inbound(db_settings, wa_id, "STOP"), correlation_id="c3"
            )
        assert [m.phrasebank_key for m in turn.outbound] == [PhraseKey.CONSENT_WITHDRAWN]

        async with session_factory() as session:
            lead = await LeadRepository(session, tenant_id).get_by_wa_id(wa_id)
            assert lead is not None
            consents = ConsentRepository(session, tenant_id)
            assert await consents.state_for_lead(lead.id) is ConsentState.WITHDRAWN
            assert await consents.is_granted(lead.id) is False

    async def test_consent_history_is_append_only(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        db_settings: Settings,
        tenant_id: uuid.UUID,
    ) -> None:
        """Every event survives, so an audit can see the whole journey."""
        wa_id = "971504444444"
        for text in ("hi", "yes", "stop"):
            async with session_factory() as session:
                await handle_inbound(session, inbound(db_settings, wa_id, text), correlation_id="c")

        async with session_factory() as session:
            lead = await LeadRepository(session, tenant_id).get_by_wa_id(wa_id)
            assert lead is not None
            events = await ConsentRepository(session, tenant_id).events_for_lead(lead.id)

        assert [event.event for event in events] == [
            ConsentEvent.NOTICE_SHOWN,
            ConsentEvent.GRANTED,
            ConsentEvent.WITHDRAWN,
        ]

    async def test_a_returning_withdrawn_consumer_sees_the_notice_again(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        db_settings: Settings,
        tenant_id: uuid.UUID,
    ) -> None:
        wa_id = "971505555555"
        for text in ("hi", "yes", "stop"):
            async with session_factory() as session:
                await handle_inbound(session, inbound(db_settings, wa_id, text), correlation_id="c")

        async with session_factory() as session:
            turn = await handle_inbound(
                session, inbound(db_settings, wa_id, "hello again"), correlation_id="c4"
            )

        assert PhraseKey.CONSENT_NOTICE in [m.phrasebank_key for m in turn.outbound]


class TestIdempotency:
    async def test_a_redelivered_message_is_a_no_op(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        db_settings: Settings,
        tenant_id: uuid.UUID,
    ) -> None:
        """Webhook delivery is at-least-once. A retry must not re-run the turn."""
        message = inbound(db_settings, "971506666666", "hi", message_id="wamid.dup1")

        async with session_factory() as session:
            first = await handle_inbound(session, message, correlation_id="c1")
        async with session_factory() as session:
            second = await handle_inbound(session, message, correlation_id="c2")

        assert first.duplicate is False
        assert second.duplicate is True
        assert second.outbound == ()

        async with session_factory() as session:
            # Still one inbound and two outbound: nothing was sent twice.
            assert await _count(session, Message) == 3
            assert await _count(session, Consent) == 1

    async def test_distinct_messages_are_both_processed(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        db_settings: Settings,
        tenant_id: uuid.UUID,
    ) -> None:
        wa_id = "971507777777"
        async with session_factory() as session:
            await handle_inbound(
                session, inbound(db_settings, wa_id, "hi", "wamid.a"), correlation_id="c1"
            )
        async with session_factory() as session:
            second = await handle_inbound(
                session, inbound(db_settings, wa_id, "yes", "wamid.b"), correlation_id="c2"
            )
        assert second.duplicate is False


class TestConversationContinuity:
    async def test_the_same_consumer_stays_in_one_conversation(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        db_settings: Settings,
        tenant_id: uuid.UUID,
    ) -> None:
        wa_id = "971508888888"
        turns = []
        for text in ("hi", "yes", "need a haircut"):
            async with session_factory() as session:
                turns.append(
                    await handle_inbound(
                        session, inbound(db_settings, wa_id, text), correlation_id="c"
                    )
                )

        assert len({turn.conversation_id for turn in turns}) == 1
        assert len({turn.turn_id for turn in turns}) == 3

        async with session_factory() as session:
            assert await _count(session, Lead) == 1
            assert await _count(session, Conversation) == 1

    async def test_an_unknown_tenant_raises_rather_than_guessing(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        db_settings: Settings,
        tenant_id: uuid.UUID,
    ) -> None:
        message = InboundMessage(
            tenant_slug="no-such-tenant",
            channel=Channel.WHATSAPP,
            wa_id="971509999999",
            text="hi",
        )
        async with session_factory() as session:
            with pytest.raises(UnknownTenant):
                await handle_inbound(session, message, correlation_id="c1")


async def _count(session: AsyncSession, model: type) -> int:
    result = await session.execute(select(func.count()).select_from(model))
    return int(result.scalar_one())
