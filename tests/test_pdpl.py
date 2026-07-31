"""PDPL: erasure that actually erases, and retention that actually runs.

These are the assertions behind the PDPL rows in ACCEPTANCE.md and behind
`docs/PDPL.md`. A compliance reviewer reading either document should be able to
land here and see the claim tested.

The hard part of erasure is not the DELETE. It is proving that nothing survived:
a cascade that did not fire, a table added six months later and never wired in,
an audit row carrying the consumer's own words behind an append-only trigger. So
the central test does not assert "the function returned" — it drives a full
conversation with a booking, erases, and then counts every table in the schema
looking for the lead id.
"""

from __future__ import annotations

import uuid

import pytest
from sqlalchemy import func, select, text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from mawjood.config import Settings
from mawjood.core.compliance import (
    RetentionPolicy,
    erase_consumer,
    plan_erasure,
    sweep_retention,
)
from mawjood.core.compliance.erasure import _CONSUMER_TABLES, identifier_hash
from mawjood.core.conversation.pipeline import handle_inbound
from mawjood.core.conversation.types import InboundMessage
from mawjood.core.enums import Category, Channel
from mawjood.db.base import Base
from mawjood.db.models import AuditLog, Booking, Consent, Lead, Message

from .conftest import RoutingSeeder, needs_database

pytestmark = [pytest.mark.integration, needs_database]

WA_ID = "971505550100"


def inbound(settings: Settings, text_body: str, wa_id: str = WA_ID) -> InboundMessage:
    return InboundMessage(
        tenant_slug=settings.default_tenant_slug,
        channel=Channel.WHATSAPP,
        wa_id=wa_id,
        text=text_body,
    )


async def a_full_conversation(
    session_factory: async_sessionmaker[AsyncSession],
    settings: Settings,
    wa_id: str = WA_ID,
) -> None:
    """Consent, a search, a booking. Touches every consumer-bearing table."""
    for message in ("SRC42", "yes", "need a haircut in Marina tomorrow at 5pm", "yes"):
        async with session_factory() as session:
            await handle_inbound(
                session, inbound(settings, message, wa_id), correlation_id="pdpl", settings=settings
            )


class TestTheErasureSurfaceIsComplete:
    """Erasure has to know about every table. This is how it stays that way."""

    async def test_every_table_with_a_lead_id_is_in_the_erasure_list(self) -> None:
        """A table added later and not wired in is a table erasure skips.

        Checked against the mapped schema rather than a hand-kept list, so the
        failure arrives on the commit that adds the table rather than during an
        audit two years later.
        """
        listed = {name for name, _ in _CONSUMER_TABLES} | {"audit_log", "leads"}
        with_lead_id = {
            name for name, table in Base.metadata.tables.items() if "lead_id" in table.columns
        }
        missing = with_lead_id - listed
        assert not missing, (
            f"these tables reference a consumer but erasure does not touch them: {missing}. "
            "Add them to _CONSUMER_TABLES in core/compliance/erasure.py."
        )

    async def test_the_hash_is_stable_and_tenant_salted(self) -> None:
        a, b = uuid.uuid4(), uuid.uuid4()
        assert identifier_hash("971501234567", a) == identifier_hash("971501234567", a)
        assert identifier_hash("971501234567", a) != identifier_hash("971501234567", b)
        assert "971501234567" not in identifier_hash("971501234567", a)


class TestErasure:
    async def test_it_removes_the_consumer_from_every_table(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        db_settings: Settings,
        tenant_id: uuid.UUID,
        routed: RoutingSeeder,
    ) -> None:
        """The central claim, proven by counting rather than by trusting.

        After erasure, every mapped table carrying a lead_id is scanned for the
        erased lead. Nothing may reference them — not a message, not an audit
        row, not a scheduled reminder.
        """
        await routed(Category.SALON, "fake_happy")
        await a_full_conversation(session_factory, db_settings)

        async with session_factory() as session:
            lead = (await session.execute(select(Lead))).scalar_one()
            lead_id = lead.id
            # The conversation really did populate the tables — otherwise this
            # test would pass against an empty database and mean nothing.
            assert (
                await session.execute(select(func.count()).select_from(Message))
            ).scalar_one() > 0
            assert (
                await session.execute(select(func.count()).select_from(Booking))
            ).scalar_one() > 0
            assert (
                await session.execute(select(func.count()).select_from(AuditLog))
            ).scalar_one() > 0

        async with session_factory() as session:
            receipt = await erase_consumer(session, tenant_id=tenant_id, wa_id=WA_ID)
            await session.commit()

        assert receipt is not None
        assert receipt.complete, f"rows survived: {receipt.remaining}"

        # Independent verification, against the schema rather than the receipt.
        async with session_factory() as session:
            survivors: dict[str, int] = {}
            for name, table in Base.metadata.tables.items():
                if "lead_id" not in table.columns:
                    continue
                count = (
                    await session.execute(
                        select(func.count()).select_from(table).where(table.c.lead_id == lead_id)
                    )
                ).scalar_one()
                if count:
                    survivors[name] = int(count)
            leads_left = (
                await session.execute(
                    select(func.count()).select_from(Lead).where(Lead.id == lead_id)
                )
            ).scalar_one()

        assert survivors == {}, f"the consumer survives in {survivors}"
        assert leads_left == 0

    async def test_it_removes_audit_rows_despite_the_append_only_trigger(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        db_settings: Settings,
        tenant_id: uuid.UUID,
        routed: RoutingSeeder,
    ) -> None:
        """The audit log carries `consumer_text` — the consumer's own words."""
        await routed(Category.SALON, "fake_happy")
        await a_full_conversation(session_factory, db_settings)

        async with session_factory() as session:
            with_text = (
                await session.execute(
                    select(func.count())
                    .select_from(AuditLog)
                    .where(AuditLog.consumer_text.isnot(None))
                )
            ).scalar_one()
        assert with_text > 0, "no audit rows carried consumer text, so this proves nothing"

        async with session_factory() as session:
            receipt = await erase_consumer(session, tenant_id=tenant_id, wa_id=WA_ID)
            await session.commit()

        assert receipt is not None
        assert receipt.deleted["audit_log"] > 0

    async def test_an_ordinary_connection_still_cannot_delete_audit_rows(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        db_settings: Settings,
        tenant_id: uuid.UUID,
        routed: RoutingSeeder,
    ) -> None:
        """The escape hatch must stay narrow. Without the flag, the trigger
        refuses — otherwise erasure would have opened a hole in the audit trail
        for everything else too."""
        from sqlalchemy.exc import DBAPIError

        await routed(Category.SALON, "fake_happy")
        await a_full_conversation(session_factory, db_settings)

        async with session_factory() as session:
            with pytest.raises(DBAPIError, match="append-only"):
                await session.execute(text("DELETE FROM audit_log"))
            await session.rollback()

    async def test_the_completion_record_names_no_one(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        db_settings: Settings,
        tenant_id: uuid.UUID,
        routed: RoutingSeeder,
    ) -> None:
        """An audit row saying "we erased +971 50 123 4567" defeats the erasure."""
        await routed(Category.SALON, "fake_happy")
        await a_full_conversation(session_factory, db_settings)

        async with session_factory() as session:
            await erase_consumer(session, tenant_id=tenant_id, wa_id=WA_ID)
            await session.commit()

        async with session_factory() as session:
            rows = (
                (
                    await session.execute(
                        select(AuditLog).where(
                            AuditLog.event_type.in_(
                                ("compliance.erasure_requested", "compliance.erasure_completed")
                            )
                        )
                    )
                )
                .scalars()
                .all()
            )

        assert len(rows) == 2
        for row in rows:
            assert WA_ID not in str(row.details)
            assert row.details["subject_hash"] == identifier_hash(WA_ID, tenant_id)
        completed = next(r for r in rows if r.event_type == "compliance.erasure_completed")
        assert completed.details["complete"] is True

    async def test_the_receipt_carries_no_identifier(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        db_settings: Settings,
        tenant_id: uuid.UUID,
        routed: RoutingSeeder,
    ) -> None:
        """The receipt gets filed and forwarded. It must not become a new copy of
        the data it certifies the removal of."""
        await routed(Category.SALON, "fake_happy")
        await a_full_conversation(session_factory, db_settings)

        async with session_factory() as session:
            receipt = await erase_consumer(session, tenant_id=tenant_id, wa_id=WA_ID)
            await session.commit()

        assert receipt is not None
        assert WA_ID not in str(receipt.as_evidence())

    async def test_erasing_an_unknown_consumer_is_not_an_error(
        self, session_factory: async_sessionmaker[AsyncSession], tenant_id: uuid.UUID
    ) -> None:
        """A request to erase somebody who was never here is satisfied by
        definition."""
        async with session_factory() as session:
            assert await erase_consumer(session, tenant_id=tenant_id, wa_id="971500000000") is None

    async def test_it_does_not_touch_another_consumer(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        db_settings: Settings,
        tenant_id: uuid.UUID,
        routed: RoutingSeeder,
    ) -> None:
        await routed(Category.SALON, "fake_happy")
        await a_full_conversation(session_factory, db_settings, wa_id=WA_ID)
        await a_full_conversation(session_factory, db_settings, wa_id="971505550999")

        async with session_factory() as session:
            await erase_consumer(session, tenant_id=tenant_id, wa_id=WA_ID)
            await session.commit()

        async with session_factory() as session:
            remaining = (await session.execute(select(Lead))).scalars().all()
        assert [lead.wa_id for lead in remaining] == ["971505550999"]

    async def test_the_plan_warns_about_a_booking_still_ahead_of_them(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        db_settings: Settings,
        tenant_id: uuid.UUID,
        routed: RoutingSeeder,
    ) -> None:
        """Erasing our record does not cancel the appointment. The venue is still
        expecting this person, and only an operator can decide what to do."""
        await routed(Category.SALON, "fake_happy")
        await a_full_conversation(session_factory, db_settings)

        async with session_factory() as session:
            plan = await plan_erasure(session, tenant_id=tenant_id, wa_id=WA_ID)

        assert plan is not None
        assert plan.total_rows > 0
        assert plan.needs_attention
        assert plan.live_bookings

    async def test_the_plan_deletes_nothing(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        db_settings: Settings,
        tenant_id: uuid.UUID,
        routed: RoutingSeeder,
    ) -> None:
        await routed(Category.SALON, "fake_happy")
        await a_full_conversation(session_factory, db_settings)

        async with session_factory() as session:
            before = (await session.execute(select(func.count()).select_from(Message))).scalar_one()
            await plan_erasure(session, tenant_id=tenant_id, wa_id=WA_ID)
        async with session_factory() as session:
            after = (await session.execute(select(func.count()).select_from(Message))).scalar_one()
        assert before == after


class TestRetention:
    async def test_it_removes_audit_rows_past_the_policy(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        db_settings: Settings,
        tenant_id: uuid.UUID,
        routed: RoutingSeeder,
    ) -> None:
        await routed(Category.SALON, "fake_happy")
        await a_full_conversation(session_factory, db_settings)

        # Insert an aged audit row rather than ageing the existing ones: the
        # append-only trigger forbids UPDATE outright, with no escape hatch, and
        # that is the correct behaviour — an audit row that can be back-dated is
        # not a record.
        async with session_factory() as session:
            await session.execute(
                text(
                    "INSERT INTO audit_log (tenant_id, event_type, actor, occurred_at) "
                    "VALUES (:t, 'test.old', 'system', now() - interval '200 days')"
                ),
                {"t": tenant_id},
            )
            await session.commit()

        async with session_factory() as session:
            sweep = await sweep_retention(session, tenant_id=tenant_id)
            await session.commit()

        assert sweep.deleted["audit_log"] == 1
        async with session_factory() as session:
            events = {
                r.event_type for r in (await session.execute(select(AuditLog))).scalars().all()
            }
        assert "test.old" not in events, "the aged row survived the sweep"
        assert "compliance.retention_swept" in events, "the sweep did not record itself"

    async def test_it_removes_consumers_past_the_policy(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        db_settings: Settings,
        tenant_id: uuid.UUID,
        routed: RoutingSeeder,
    ) -> None:
        await routed(Category.SALON, "fake_happy")
        await a_full_conversation(session_factory, db_settings)

        async with session_factory() as session:
            # Old, and the booking is in the past, so nothing holds them back.
            await session.execute(
                text("UPDATE leads SET last_seen_at = now() - interval '400 days'")
            )
            await session.execute(
                text("UPDATE bookings SET slot_start = now() - interval '390 days'")
            )
            await session.commit()

        async with session_factory() as session:
            sweep = await sweep_retention(session, tenant_id=tenant_id)
            await session.commit()

        assert sweep.deleted["leads"] == 1
        async with session_factory() as session:
            assert (await session.execute(select(Lead))).scalars().all() == []

    async def test_a_consumer_with_a_future_booking_is_kept(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        db_settings: Settings,
        tenant_id: uuid.UUID,
        routed: RoutingSeeder,
    ) -> None:
        """Deleting them would leave a venue expecting somebody we no longer
        know about."""
        await routed(Category.SALON, "fake_happy")
        await a_full_conversation(session_factory, db_settings)

        async with session_factory() as session:
            await session.execute(
                text("UPDATE leads SET last_seen_at = now() - interval '400 days'")
            )
            await session.commit()

        async with session_factory() as session:
            sweep = await sweep_retention(session, tenant_id=tenant_id)
            await session.commit()

        assert sweep.skipped_with_future_bookings == 1
        assert sweep.deleted["leads"] == 0
        async with session_factory() as session:
            assert len((await session.execute(select(Lead))).scalars().all()) == 1

    async def test_recent_data_is_untouched(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        db_settings: Settings,
        tenant_id: uuid.UUID,
        routed: RoutingSeeder,
    ) -> None:
        await routed(Category.SALON, "fake_happy")
        await a_full_conversation(session_factory, db_settings)

        async with session_factory() as session:
            sweep = await sweep_retention(session, tenant_id=tenant_id)
            await session.commit()

        assert sweep.total_deleted == 0
        async with session_factory() as session:
            assert len((await session.execute(select(Lead))).scalars().all()) == 1

    async def test_a_dry_run_counts_without_deleting(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        db_settings: Settings,
        tenant_id: uuid.UUID,
        routed: RoutingSeeder,
    ) -> None:
        """How an operator checks what a first run would do before letting it
        loose on real data."""
        await routed(Category.SALON, "fake_happy")
        await a_full_conversation(session_factory, db_settings)

        async with session_factory() as session:
            await session.execute(
                text("UPDATE leads SET last_seen_at = now() - interval '400 days'")
            )
            await session.execute(
                text("UPDATE bookings SET slot_start = now() - interval '390 days'")
            )
            await session.commit()

        async with session_factory() as session:
            sweep = await sweep_retention(session, tenant_id=tenant_id, dry_run=True)
            await session.commit()

        assert sweep.dry_run
        assert sweep.deleted["leads"] == 1
        async with session_factory() as session:
            assert len((await session.execute(select(Lead))).scalars().all()) == 1

    async def test_consent_dies_with_the_data_it_authorises(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        db_settings: Settings,
        tenant_id: uuid.UUID,
        routed: RoutingSeeder,
    ) -> None:
        """A consent row is personal data too.

        An earlier draft kept consent after the sweep, reasoning that it is the
        evidence we were permitted to hold the data. This test caught that: a
        consent row is keyed to a lead and carries the wording that person was
        shown, so retaining it once everything else is gone means holding
        personal data past the retention policy with nothing left to evidence.

        Consent lives exactly as long as the data it authorises.
        """
        await routed(Category.SALON, "fake_happy")
        await a_full_conversation(session_factory, db_settings)

        async with session_factory() as session:
            before = (await session.execute(select(func.count()).select_from(Consent))).scalar_one()
            await session.execute(
                text("UPDATE leads SET last_seen_at = now() - interval '400 days'")
            )
            await session.execute(
                text("UPDATE bookings SET slot_start = now() - interval '390 days'")
            )
            await session.commit()
        assert before > 0

        async with session_factory() as session:
            await sweep_retention(session, tenant_id=tenant_id)
            await session.commit()

        async with session_factory() as session:
            after = (await session.execute(select(func.count()).select_from(Consent))).scalar_one()
        assert after == 0, "consent outlived the data it authorised"

    async def test_erasure_removes_consent_too(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        db_settings: Settings,
        tenant_id: uuid.UUID,
        routed: RoutingSeeder,
    ) -> None:
        """Erasure is the other route to the same place."""
        await routed(Category.SALON, "fake_happy")
        await a_full_conversation(session_factory, db_settings)

        async with session_factory() as session:
            receipt = await erase_consumer(session, tenant_id=tenant_id, wa_id=WA_ID)
            await session.commit()

        assert receipt is not None
        assert receipt.deleted["consents"] > 0
        async with session_factory() as session:
            assert (await session.execute(select(Consent))).scalars().all() == []

    async def test_the_sweep_records_itself(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        tenant_id: uuid.UUID,
    ) -> None:
        """ "Did retention run?" has to be answerable from the audit log rather
        than from a cron log nobody keeps."""
        async with session_factory() as session:
            await sweep_retention(session, tenant_id=tenant_id)
            await session.commit()

        async with session_factory() as session:
            row = (
                await session.execute(
                    select(AuditLog).where(AuditLog.event_type == "compliance.retention_swept")
                )
            ).scalar_one()
        assert row.details["audit_retention_days"] == 90
        assert row.details["consumer_retention_days"] == 365
        assert "ran_at" in row.details

    async def test_the_policy_comes_from_settings(self) -> None:
        settings = Settings(
            database_url="postgresql+asyncpg://u@127.0.0.1:5432/d",
            log_retention_days=30,
            data_retention_days=90,
        )
        policy = RetentionPolicy.from_settings(settings)
        assert policy.audit_days == 30
        assert policy.consumer_days == 90

    async def test_the_default_matches_the_documented_commitment(self) -> None:
        """CLAUDE.md section 11 commits to 90-day log retention."""
        assert RetentionPolicy().audit_days == 90

    async def test_it_is_tenant_scoped(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        db_settings: Settings,
        tenant_id: uuid.UUID,
        other_tenant_id: uuid.UUID,
        routed: RoutingSeeder,
    ) -> None:
        await routed(Category.SALON, "fake_happy")
        await a_full_conversation(session_factory, db_settings)

        async with session_factory() as session:
            await session.execute(
                text("UPDATE leads SET last_seen_at = now() - interval '400 days'")
            )
            await session.execute(
                text("UPDATE bookings SET slot_start = now() - interval '390 days'")
            )
            await session.commit()

        async with session_factory() as session:
            sweep = await sweep_retention(session, tenant_id=other_tenant_id)
            await session.commit()

        assert sweep.total_deleted == 0
        async with session_factory() as session:
            assert len((await session.execute(select(Lead))).scalars().all()) == 1
