"""The audit trail must reconstruct a routing decision months later.

The requirement, precisely: the inputs, each aggregator attempted, its outcome
and latency, the reason it advanced, and what the consumer was shown.

The router does not exist yet — it is Phase 2. That is exactly why these tests
are here now. They write the cascade trace the router *will* write, then answer
every one of those questions from the database. If the schema cannot answer them
today, it cannot answer them in Phase 2 either, and retrofitting an audit table
under a live system is the thing this is meant to avoid.
"""

from __future__ import annotations

import uuid

import pytest
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from mawjood.core.enums import AuditEvent, Category, Outcome
from mawjood.db.models import AuditLog
from mawjood.observability.audit import AuditTrail, format_trail, replay_turn

from .conftest import needs_database

pytestmark = [pytest.mark.integration, needs_database]


async def write_cascade_trace(
    session: AsyncSession, tenant_id: uuid.UUID
) -> tuple[uuid.UUID, uuid.UUID]:
    """A realistic three-deep cascade that exhausts and goes to a human.

    Zenoti has nothing, the second platform times out on create and cannot be
    reconciled, the third cannot perform the action at all. Every branch the
    invariant cares about, in one trace.
    """
    audit = AuditTrail(session=session, tenant_id=tenant_id, correlation_id="req-abc123")
    decision = audit.new_decision()

    await audit.routing_started(
        decision_id=decision,
        category=Category.SALON,
        candidates=["zenoti", "fresha", "booksy"],
        request={
            "service": "haircut",
            "area": "Dubai Marina",
            "window_start": "2026-08-01T18:00:00+04:00",
            "window_end": "2026-08-01T21:00:00+04:00",
            "party_size": 1,
        },
        deadline_ms=2500,
    )

    # 1. Nothing free.
    await audit.routing_attempt(
        decision_id=decision,
        attempt_number=1,
        category=Category.SALON,
        platform_slug="zenoti",
        merchant_ref="centre-marina-01",
        outcome=Outcome.NO_AVAILABILITY,
        latency_ms=412,
        request_id="zen-req-991",
    )
    await audit.routing_advanced(
        decision_id=decision,
        attempt_number=1,
        from_platform="zenoti",
        to_platform="fresha",
        outcome=Outcome.NO_AVAILABILITY,
        advance_reason="no slots in the requested window",
    )

    # 2. Timed out on create — we do not know whether it landed.
    await audit.routing_attempt(
        decision_id=decision,
        attempt_number=2,
        category=Category.SALON,
        platform_slug="fresha",
        outcome=Outcome.TIMEOUT,
        latency_ms=2500,
        raw_error="ReadTimeout after 2500ms on POST /bookings",
    )
    await audit.reconciliation_started(
        decision_id=decision, platform_slug="fresha", idempotency_key="conv-1:salon:1800:fresha"
    )
    await audit.reconciliation_result(
        decision_id=decision,
        platform_slug="fresha",
        conclusive=False,
        booked=None,
        latency_ms=850,
    )

    # 3. Cannot perform the action at all.
    await audit.routing_attempt(
        decision_id=decision,
        attempt_number=3,
        category=Category.SALON,
        platform_slug="booksy",
        outcome=Outcome.UNSUPPORTED,
        latency_ms=5,
    )
    await audit.routing_exhausted(
        decision_id=decision,
        attempted=["zenoti", "fresha", "booksy"],
        advance_reason="every candidate exhausted; reconciliation inconclusive",
    )
    await audit.handoff_enqueued(
        decision_id=decision,
        reason="cascade_exhausted",
        context={"category": "salon", "uncertain_booking_on": "fresha"},
    )

    # What the consumer actually saw. Never an empty shelf.
    await audit.message_sent(
        phrasebank_key="handoff.connecting",
        locale="en",
        text="I'm bringing in one of our team to take this the rest of the way.",
        channel="whatsapp",
    )
    await session.commit()
    return audit.turn_id, decision


class TestReconstruction:
    async def test_a_cascade_is_fully_reconstructable(
        self, session_factory: async_sessionmaker[AsyncSession], tenant_id: uuid.UUID
    ) -> None:
        async with session_factory() as session:
            turn_id, decision_id = await write_cascade_trace(session, tenant_id)

        async with session_factory() as session:
            rows = await replay_turn(session, tenant_id, turn_id)

        # Ordering is by database identity, not timestamp: these rows are written
        # within the same millisecond and must still read back in order.
        assert [row.seq for row in rows] == sorted(row.seq for row in rows)

        events = [row.event_type for row in rows]
        assert events == [
            AuditEvent.ROUTING_STARTED,
            AuditEvent.ROUTING_ATTEMPT,
            AuditEvent.ROUTING_ADVANCED,
            AuditEvent.ROUTING_ATTEMPT,
            AuditEvent.BOOKING_RECONCILIATION_STARTED,
            AuditEvent.BOOKING_RECONCILIATION_RESULT,
            AuditEvent.ROUTING_ATTEMPT,
            AuditEvent.ROUTING_EXHAUSTED,
            AuditEvent.HANDOFF_ENQUEUED,
            AuditEvent.MESSAGE_SENT,
        ]
        assert all(row.decision_id == decision_id for row in rows if row.decision_id)

    async def test_the_inputs_survive(
        self, session_factory: async_sessionmaker[AsyncSession], tenant_id: uuid.UUID
    ) -> None:
        """ "What did we ask for" must be answerable without guessing."""
        async with session_factory() as session:
            turn_id, _ = await write_cascade_trace(session, tenant_id)
        async with session_factory() as session:
            rows = await replay_turn(session, tenant_id, turn_id)

        started = next(r for r in rows if r.event_type == AuditEvent.ROUTING_STARTED)
        assert started.inputs["service"] == "haircut"
        assert started.inputs["area"] == "Dubai Marina"
        assert started.details["candidates"] == ["zenoti", "fresha", "booksy"]
        assert started.details["deadline_ms"] == 2500

    async def test_every_attempt_records_who_what_and_how_long(
        self, session_factory: async_sessionmaker[AsyncSession], tenant_id: uuid.UUID
    ) -> None:
        async with session_factory() as session:
            turn_id, _ = await write_cascade_trace(session, tenant_id)
        async with session_factory() as session:
            rows = await replay_turn(session, tenant_id, turn_id)

        attempts = [r for r in rows if r.event_type == AuditEvent.ROUTING_ATTEMPT]
        assert [(a.attempt_number, a.platform_slug, a.outcome, a.latency_ms) for a in attempts] == [
            (1, "zenoti", Outcome.NO_AVAILABILITY, 412),
            (2, "fresha", Outcome.TIMEOUT, 2500),
            (3, "booksy", Outcome.UNSUPPORTED, 5),
        ]

    async def test_the_reason_it_advanced_is_recorded(
        self, session_factory: async_sessionmaker[AsyncSession], tenant_id: uuid.UUID
    ) -> None:
        async with session_factory() as session:
            turn_id, _ = await write_cascade_trace(session, tenant_id)
        async with session_factory() as session:
            rows = await replay_turn(session, tenant_id, turn_id)

        advanced = next(r for r in rows if r.event_type == AuditEvent.ROUTING_ADVANCED)
        assert advanced.advance_reason == "no slots in the requested window"
        assert advanced.details["next_platform"] == "fresha"

        exhausted = next(r for r in rows if r.event_type == AuditEvent.ROUTING_EXHAUSTED)
        assert "reconciliation inconclusive" in (exhausted.advance_reason or "")

    async def test_what_the_consumer_saw_is_recorded(
        self, session_factory: async_sessionmaker[AsyncSession], tenant_id: uuid.UUID
    ) -> None:
        async with session_factory() as session:
            turn_id, _ = await write_cascade_trace(session, tenant_id)
        async with session_factory() as session:
            rows = await replay_turn(session, tenant_id, turn_id)

        visible = [r for r in rows if r.consumer_visible]
        assert len(visible) == 1
        assert visible[0].phrasebank_key == "handoff.connecting"
        assert visible[0].consumer_text
        assert visible[0].locale == "en"

    async def test_the_timeout_is_distinguishable_from_a_failure(
        self, session_factory: async_sessionmaker[AsyncSession], tenant_id: uuid.UUID
    ) -> None:
        """The double-booking defence depends on knowing "we do not know"."""
        async with session_factory() as session:
            turn_id, _ = await write_cascade_trace(session, tenant_id)
        async with session_factory() as session:
            rows = await replay_turn(session, tenant_id, turn_id)

        result = next(r for r in rows if r.event_type == AuditEvent.BOOKING_RECONCILIATION_RESULT)
        assert result.details["conclusive"] is False
        assert result.details["booked"] is None
        assert result.platform_slug == "fresha"

    async def test_raw_errors_are_kept_for_operators(
        self, session_factory: async_sessionmaker[AsyncSession], tenant_id: uuid.UUID
    ) -> None:
        async with session_factory() as session:
            turn_id, _ = await write_cascade_trace(session, tenant_id)
        async with session_factory() as session:
            rows = await replay_turn(session, tenant_id, turn_id)

        timeout = next(
            r
            for r in rows
            if r.event_type == AuditEvent.ROUTING_ATTEMPT and r.outcome == Outcome.TIMEOUT
        )
        assert "ReadTimeout" in timeout.details["raw_error"]
        # Operator detail must never be consumer-visible.
        assert timeout.consumer_visible is False
        assert timeout.consumer_text is None

    async def test_the_trail_renders_for_a_human(
        self, session_factory: async_sessionmaker[AsyncSession], tenant_id: uuid.UUID
    ) -> None:
        async with session_factory() as session:
            turn_id, _ = await write_cascade_trace(session, tenant_id)
        async with session_factory() as session:
            rows = await replay_turn(session, tenant_id, turn_id)

        rendered = format_trail(rows)
        assert "zenoti" in rendered
        assert "no_availability" in rendered
        assert "412ms" in rendered
        assert "consumer saw" in rendered


class TestQueryability:
    """Typed columns, not a JSONB swamp: these queries must stay cheap."""

    async def test_attempts_are_queryable_by_platform_and_outcome(
        self, session_factory: async_sessionmaker[AsyncSession], tenant_id: uuid.UUID
    ) -> None:
        async with session_factory() as session:
            await write_cascade_trace(session, tenant_id)

        async with session_factory() as session:
            result = await session.execute(
                select(AuditLog.platform_slug, AuditLog.latency_ms)
                .where(AuditLog.tenant_id == tenant_id)
                .where(AuditLog.outcome == Outcome.TIMEOUT)
            )
            timeouts = [tuple(row) for row in result.all()]

        assert timeouts == [("fresha", 2500)]

    async def test_correlation_id_ties_audit_rows_to_log_lines(
        self, session_factory: async_sessionmaker[AsyncSession], tenant_id: uuid.UUID
    ) -> None:
        async with session_factory() as session:
            turn_id, _ = await write_cascade_trace(session, tenant_id)
        async with session_factory() as session:
            rows = await replay_turn(session, tenant_id, turn_id)
        assert {row.correlation_id for row in rows} == {"req-abc123"}


class TestAppendOnly:
    """An audit trail that can be edited is not evidence of anything."""

    async def test_updates_are_refused(
        self, session_factory: async_sessionmaker[AsyncSession], tenant_id: uuid.UUID
    ) -> None:
        async with session_factory() as session:
            await write_cascade_trace(session, tenant_id)

        async with session_factory() as session:
            with pytest.raises(Exception, match="append-only"):
                await session.execute(
                    text("UPDATE audit_log SET outcome = 'ok' WHERE tenant_id = :t"),
                    {"t": tenant_id},
                )
                await session.commit()

    async def test_deletes_are_refused_without_the_purge_flag(
        self, session_factory: async_sessionmaker[AsyncSession], tenant_id: uuid.UUID
    ) -> None:
        async with session_factory() as session:
            await write_cascade_trace(session, tenant_id)

        async with session_factory() as session:
            with pytest.raises(Exception, match="append-only"):
                await session.execute(
                    text("DELETE FROM audit_log WHERE tenant_id = :t"), {"t": tenant_id}
                )
                await session.commit()

    async def test_the_erasure_path_can_still_delete(
        self, session_factory: async_sessionmaker[AsyncSession], tenant_id: uuid.UUID
    ) -> None:
        """PDPL grants a right to erasure. The guard must not block it."""
        async with session_factory() as session:
            await write_cascade_trace(session, tenant_id)

        async with session_factory() as session:
            await session.execute(text("SET LOCAL mawjood.allow_purge = 'on'"))
            await session.execute(
                text("DELETE FROM audit_log WHERE tenant_id = :t"), {"t": tenant_id}
            )
            await session.commit()

        async with session_factory() as session:
            remaining = await session.execute(
                select(AuditLog).where(AuditLog.tenant_id == tenant_id)
            )
            assert remaining.scalars().all() == []
