"""Attribution, end to end: a printed link or QR becomes a row in the database.

PLAN.md asks for the round trip — ``wa.me`` link → conversation → attribution
row — rather than unit tests of the parser, because the parser was never the
risky part. The risky parts are the joins: a code that parses but never gets
written, a code that gets written but leaves the consumer talking to a campaign
identifier, and a returning consumer whose first touch quietly gets rewritten by
whichever poster they tapped most recently.

All three are asserted below.

The QR generator (``tools/make_qr.py``) is exercised too, so what gets printed
and what gets parsed are known to be the same string. A QR encoding a link the
parser does not recognise is a poster that does nothing, and nobody finds out
until the campaign is over.
"""

from __future__ import annotations

import uuid

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from mawjood.config import Settings
from mawjood.core.attribution import (
    build_wa_link,
    medium_for,
    parse_source_code,
    strip_source_code,
)
from mawjood.core.conversation.pipeline import handle_inbound
from mawjood.core.conversation.types import InboundMessage
from mawjood.core.enums import AttributionMedium, Channel, Direction
from mawjood.db.models import Attribution, Message

from .conftest import needs_database

pytestmark = [pytest.mark.integration, needs_database]

SOURCE = "SRC12"


def inbound(settings: Settings, text: str, wa_id: str) -> InboundMessage:
    return InboundMessage(
        tenant_slug=settings.default_tenant_slug,
        channel=Channel.WHATSAPP,
        wa_id=wa_id,
        text=text,
    )


class TestTheLinkAndTheParserAgree:
    """What gets printed must be what gets parsed."""

    def test_a_generated_link_carries_a_parsable_code(self) -> None:
        link = build_wa_link("+971 50 123 4567", SOURCE)
        assert link == "https://wa.me/971501234567?text=SRC12"
        # The consumer's first message is the prefill, verbatim.
        assert parse_source_code("SRC12") == SOURCE

    def test_the_qr_encodes_the_same_link(self) -> None:
        """A QR encoding something the parser does not recognise is a poster
        that does nothing, and nobody finds out until the campaign is over."""
        from tools.make_qr import qr_payload

        payload = qr_payload(phone="+971501234567", source="SRC12")
        assert payload == build_wa_link("+971501234567", SOURCE)
        prefill = payload.split("?text=", 1)[1]
        assert parse_source_code(prefill) == SOURCE

    def test_a_bare_code_reads_as_a_scan(self) -> None:
        """Nobody types "SRC12" unprompted."""
        assert medium_for("SRC12") is AttributionMedium.QR

    def test_a_code_inside_a_sentence_reads_as_a_link(self) -> None:
        assert medium_for("SRC12 hi, need a haircut") is AttributionMedium.WA_LINK


class TestTheRoundTrip:
    async def test_a_prefilled_first_message_writes_an_attribution_row(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        db_settings: Settings,
        tenant_id: uuid.UUID,
    ) -> None:
        async with session_factory() as session:
            turn = await handle_inbound(
                session,
                inbound(db_settings, "SRC12", "971505550001"),
                correlation_id="attr-1",
                settings=db_settings,
            )

        async with session_factory() as session:
            row = (await session.execute(select(Attribution))).scalar_one()

        assert row.source_code == SOURCE
        assert row.lead_id == turn.lead_id
        assert row.conversation_id == turn.conversation_id
        assert row.medium is AttributionMedium.QR
        assert row.is_first_touch is True
        assert row.raw_payload == "SRC12"

    async def test_the_consumer_is_greeted_not_asked_about_the_code(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        db_settings: Settings,
        tenant_id: uuid.UUID,
    ) -> None:
        """A campaign code is bookkeeping, not a request.

        Left in, the engine tries to interpret "SRC12" as what the consumer
        wants, and the first thing a scanned poster produces is Mawjood asking a
        confused question about a tracking identifier.
        """
        async with session_factory() as session:
            turn = await handle_inbound(
                session,
                inbound(db_settings, "SRC12", "971505550002"),
                correlation_id="attr-2",
                settings=db_settings,
            )

        assert turn.outbound
        for message in turn.outbound:
            assert "SRC12" not in message.text

    async def test_a_code_with_a_real_request_keeps_the_request(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        db_settings: Settings,
        tenant_id: uuid.UUID,
    ) -> None:
        """The code is stripped; what the consumer actually asked for is not."""
        text = "SRC12 need a haircut in Marina tomorrow evening"
        assert strip_source_code(text) == "need a haircut in Marina tomorrow evening"

        async with session_factory() as session:
            await handle_inbound(
                session,
                inbound(db_settings, text, "971505550003"),
                correlation_id="attr-3",
                settings=db_settings,
            )

        async with session_factory() as session:
            row = (await session.execute(select(Attribution))).scalar_one()
            # The inbound message is stored verbatim — the transcript is what the
            # consumer sent, not what the parser made of it.
            inbound_row = (
                await session.execute(select(Message).where(Message.direction == Direction.INBOUND))
            ).scalar_one()

        assert row.medium is AttributionMedium.WA_LINK
        assert inbound_row.body == text

    async def test_first_touch_survives_a_later_campaign(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        db_settings: Settings,
        tenant_id: uuid.UUID,
    ) -> None:
        """Which poster brought someone in is a different question from which
        one they tapped most recently. Conflating them makes both unanswerable."""
        for code in ("SRC12", "SRC99"):
            async with session_factory() as session:
                await handle_inbound(
                    session,
                    inbound(db_settings, code, "971505550004"),
                    correlation_id=f"attr-{code}",
                    settings=db_settings,
                )

        async with session_factory() as session:
            rows = (
                (await session.execute(select(Attribution).order_by(Attribution.created_at)))
                .scalars()
                .all()
            )

        assert [r.source_code for r in rows] == ["SRC12", "SRC99"]
        assert [r.is_first_touch for r in rows] == [True, False]

    async def test_an_ordinary_message_writes_nothing(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        db_settings: Settings,
        tenant_id: uuid.UUID,
    ) -> None:
        async with session_factory() as session:
            await handle_inbound(
                session,
                inbound(db_settings, "hi", "971505550005"),
                correlation_id="attr-5",
                settings=db_settings,
            )

        async with session_factory() as session:
            assert (await session.execute(select(Attribution))).scalars().all() == []

    async def test_the_capture_is_audited(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        db_settings: Settings,
        tenant_id: uuid.UUID,
    ) -> None:
        from mawjood.db.models import AuditLog

        async with session_factory() as session:
            await handle_inbound(
                session,
                inbound(db_settings, "SRC12", "971505550006"),
                correlation_id="attr-6",
                settings=db_settings,
            )

        async with session_factory() as session:
            event = (
                await session.execute(
                    select(AuditLog).where(AuditLog.event_type == "attribution.captured")
                )
            ).scalar_one()

        assert event.details["source_code"] == SOURCE
        assert event.details["first_touch"] is True

    async def test_attribution_is_tenant_scoped(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        db_settings: Settings,
        tenant_id: uuid.UUID,
        other_tenant_id: uuid.UUID,
    ) -> None:
        async with session_factory() as session:
            await handle_inbound(
                session,
                inbound(db_settings, "SRC12", "971505550007"),
                correlation_id="attr-7",
                settings=db_settings,
            )

        async with session_factory() as session:
            foreign = (
                (
                    await session.execute(
                        select(Attribution).where(Attribution.tenant_id == other_tenant_id)
                    )
                )
                .scalars()
                .all()
            )
        assert foreign == []
