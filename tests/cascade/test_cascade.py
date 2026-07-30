"""The fallback cascade: the invariant at runtime.

Written before ``core/routing/`` existed. Every scenario here is one the product
is named after surviving.

No database, no network — the cascade is pure logic over adapters and a deadline,
which is exactly why it can be tested exhaustively.
"""

from __future__ import annotations

import asyncio
import uuid
from datetime import UTC, datetime, timedelta

import pytest

from mawjood.core.aggregators.base import (
    AvailabilityRequest,
    CallContext,
    Deadline,
    MerchantRef,
    make_idempotency_key,
)
from mawjood.core.aggregators.fake import (
    AuthFailFake,
    EmptyFake,
    FlakyFake,
    HappyFake,
    InconclusiveTimeoutFake,
    LowConfidenceFake,
    RateLimitedFake,
    TimeoutFake,
    UnsupportedFake,
    fake_merchant,
)
from mawjood.core.enums import Category, Outcome
from mawjood.core.routing.cascade import (
    BookStatus,
    Candidate,
    Cascade,
    SearchStatus,
)
from mawjood.core.routing.policy import RouterAction, decide

from .conftest import RecordingAudit

CONVERSATION = uuid.uuid4()
TENANT = uuid.uuid4()


def window() -> tuple[datetime, datetime]:
    start = datetime(2026, 8, 1, 18, 0, tzinfo=UTC)
    return start, start + timedelta(hours=3)


def request() -> AvailabilityRequest:
    start, end = window()
    return AvailabilityRequest(
        category=Category.SALON,
        service="haircut",
        window_start=start,
        window_end=end,
        area="Dubai Marina",
    )


def candidate(adapter: object, name: str = "Marina Salon") -> Candidate:
    return Candidate(
        adapter=adapter,  # type: ignore[arg-type]
        merchant=fake_merchant(adapter.slug, name),  # type: ignore[attr-defined]
        credentials={"api_key": "test"},
    )


def context(merchant: MerchantRef, deadline_ms: int = 2500) -> CallContext:
    return CallContext(
        tenant_id=TENANT,
        conversation_id=CONVERSATION,
        merchant=merchant,
        credentials={"api_key": "test"},
        deadline=Deadline.in_ms(deadline_ms),
        correlation_id="test-correlation",
    )


def cascade(audit: RecordingAudit, deadline_ms: int = 2500) -> Cascade:
    return Cascade(
        audit=audit,
        deadline=Deadline.in_ms(deadline_ms),
        tenant_id=TENANT,
        conversation_id=CONVERSATION,
        correlation_id="test-correlation",
    )


# ---------------------------------------------------------------------------
# 1. First empty, second has a slot — one seamless flow
# ---------------------------------------------------------------------------


class TestAdvanceOnEmpty:
    async def test_an_empty_first_aggregator_advances_to_a_second_that_has_a_slot(self) -> None:
        audit = RecordingAudit()
        empty, happy = EmptyFake(), HappyFake()
        result = await cascade(audit).search(
            [candidate(empty, "Empty Salon"), candidate(happy, "Marina Salon")], request()
        )

        assert result.status is SearchStatus.FOUND
        assert result.slot is not None
        assert result.slot.platform_slug == "fake_happy"
        assert result.slot.venue_name == "Marina Salon"

    async def test_both_aggregators_were_actually_tried(self) -> None:
        audit = RecordingAudit()
        empty, happy = EmptyFake(), HappyFake()
        await cascade(audit).search(
            [candidate(empty, "Empty Salon"), candidate(happy, "Marina Salon")], request()
        )
        assert empty.calls == ["search_availability"]
        assert happy.calls == ["search_availability"]

    async def test_the_consumer_sees_nothing_of_the_first_attempt(self) -> None:
        """One seamless flow. The empty shelf is invisible from the outside."""
        audit = RecordingAudit()
        await cascade(audit).search(
            [candidate(EmptyFake(), "Empty"), candidate(HappyFake(), "Marina Salon")], request()
        )
        assert audit.consumer_visible_events == []

    async def test_the_advance_is_audited_with_its_reason(self) -> None:
        audit = RecordingAudit()
        await cascade(audit).search(
            [candidate(EmptyFake(), "Empty"), candidate(HappyFake())], request()
        )
        advanced = audit.of_type("routing.advanced")
        assert len(advanced) == 1
        assert advanced[0]["from_platform"] == "fake_empty"
        assert advanced[0]["to_platform"] == "fake_happy"
        assert advanced[0]["outcome"] == Outcome.NO_AVAILABILITY

    async def test_attempts_are_numbered_in_order(self) -> None:
        audit = RecordingAudit()
        await cascade(audit).search(
            [candidate(EmptyFake()), candidate(AuthFailFake()), candidate(HappyFake())], request()
        )
        attempts = audit.of_type("routing.attempt")
        assert [a["attempt_number"] for a in attempts] == [1, 2, 3]
        assert [a["platform_slug"] for a in attempts] == ["fake_empty", "fake_auth", "fake_happy"]

    @pytest.mark.parametrize(
        "failing",
        [EmptyFake, AuthFailFake, RateLimitedFake, TimeoutFake, UnsupportedFake],
        ids=["empty", "auth_error", "rate_limited", "timeout", "unsupported"],
    )
    async def test_every_failure_mode_advances_rather_than_stopping(self, failing: type) -> None:
        audit = RecordingAudit()
        happy = HappyFake()
        result = await cascade(audit).search(
            [candidate(failing()), candidate(happy, "Marina Salon")], request()
        )
        assert result.status is SearchStatus.FOUND
        assert result.slot is not None
        assert result.slot.platform_slug == "fake_happy"


# ---------------------------------------------------------------------------
# 2. Everything empty — handoff, and a pivot rather than a failure
# ---------------------------------------------------------------------------


class TestExhaustion:
    async def test_all_empty_exhausts_the_list(self) -> None:
        audit = RecordingAudit()
        result = await cascade(audit).search(
            [candidate(EmptyFake()), candidate(EmptyFake()), candidate(EmptyFake())], request()
        )
        assert result.status is SearchStatus.EXHAUSTED
        assert result.slot is None

    async def test_exhaustion_records_everything_that_was_tried(self) -> None:
        audit = RecordingAudit()
        await cascade(audit).search(
            [candidate(EmptyFake()), candidate(AuthFailFake()), candidate(TimeoutFake())], request()
        )
        exhausted = audit.of_type("routing.exhausted")
        assert len(exhausted) == 1
        assert exhausted[0]["attempted"] == ["fake_empty", "fake_auth", "fake_timeout"]

    async def test_every_upstream_down_still_exhausts_cleanly(self) -> None:
        """Not one candidate can serve. The cascade must still end in a
        controlled handoff, never an exception."""
        audit = RecordingAudit()
        result = await cascade(audit).search(
            [
                candidate(TimeoutFake()),
                candidate(AuthFailFake()),
                candidate(RateLimitedFake()),
                candidate(UnsupportedFake()),
                candidate(EmptyFake()),
            ],
            request(),
        )
        assert result.status is SearchStatus.EXHAUSTED
        assert result.handoff_reason is not None

    async def test_an_empty_candidate_list_is_exhaustion_not_a_crash(self) -> None:
        audit = RecordingAudit()
        result = await cascade(audit).search([], request())
        assert result.status is SearchStatus.EXHAUSTED

    async def test_nothing_consumer_visible_is_emitted_by_the_cascade(self) -> None:
        """The cascade never speaks. It returns a status and the conversation
        layer renders a phrasebank pivot."""
        audit = RecordingAudit()
        await cascade(audit).search([candidate(EmptyFake()) for _ in range(3)], request())
        assert audit.consumer_visible_events == []


# ---------------------------------------------------------------------------
# 3. Timeout on create — reconcile, and never double-book
# ---------------------------------------------------------------------------


class TestDoubleBookingDefence:
    async def test_a_create_timeout_triggers_a_reconciliation_read(self) -> None:
        audit = RecordingAudit()
        timeout = TimeoutFake(lands_anyway=False)
        cand = candidate(timeout)
        slot = (await cascade(audit).search([candidate(HappyFake())], request())).slot
        assert slot is not None

        await cascade(audit).book(cand, slot, consumer_name="Aisha")
        assert "find_by_idempotency_key" in timeout.calls

    async def test_a_landed_booking_is_recovered_rather_than_repeated(self) -> None:
        """The nastiest real case: it timed out for us but worked upstream.

        Reconciliation must find it and treat the booking as done.
        """
        audit = RecordingAudit()
        timeout = TimeoutFake(lands_anyway=True)
        cand = candidate(timeout)
        slot = (await cascade(audit).search([candidate(HappyFake())], request())).slot
        assert slot is not None

        result = await cascade(audit).book(cand, slot, consumer_name="Aisha")

        assert result.status is BookStatus.BOOKED
        assert len(timeout.created) == 1

    async def test_an_inconclusive_reconciliation_goes_to_a_human(self) -> None:
        """Do not know, cannot find out: hand over rather than guess.

        Guessing 'not booked' risks a double booking; guessing 'booked' risks a
        consumer turning up to nothing.
        """
        audit = RecordingAudit()
        inconclusive = InconclusiveTimeoutFake(lands_anyway=True)
        cand = candidate(inconclusive)
        slot = (await cascade(audit).search([candidate(HappyFake())], request())).slot
        assert slot is not None

        result = await cascade(audit).book(cand, slot, consumer_name="Aisha")

        assert result.status is BookStatus.UNCERTAIN
        assert result.handoff_reason == "reconciliation_inconclusive"

    async def test_an_inconclusive_create_never_advances_to_another_platform(self) -> None:
        """The whole point: falling through here books the consumer twice."""
        audit = RecordingAudit()
        inconclusive = InconclusiveTimeoutFake(lands_anyway=True)
        second = HappyFake()

        slot = (await cascade(audit).search([candidate(HappyFake())], request())).slot
        assert slot is not None
        result = await cascade(audit).book(
            candidate(inconclusive), slot, consumer_name="Aisha", fallbacks=[candidate(second)]
        )

        assert result.status is BookStatus.UNCERTAIN
        assert second.calls == [], "advanced after an inconclusive create — that double-books"

    async def test_exactly_one_create_succeeds_across_every_adapter(self) -> None:
        """The assertion the design exists to make.

        Sum every successful create across every fake. It must be one.
        """
        audit = RecordingAudit()
        timeout = TimeoutFake(lands_anyway=True)
        others = [HappyFake(), HappyFake(), HappyFake()]

        slot = (await cascade(audit).search([candidate(HappyFake())], request())).slot
        assert slot is not None
        await cascade(audit).book(
            candidate(timeout),
            slot,
            consumer_name="Aisha",
            fallbacks=[candidate(o) for o in others],
        )

        total = len(timeout.created) + sum(len(o.created) for o in others)
        assert total == 1, f"{total} bookings were created; exactly one is permitted"

    async def test_the_idempotency_key_is_derived_not_random(self) -> None:
        """Two attempts at the same intent must produce the same key, or
        reconciliation has nothing to look up."""
        start, _ = window()
        first = make_idempotency_key(
            conversation_id=CONVERSATION,
            category=Category.SALON,
            slot_start=start,
            platform_slug="zenoti",
        )
        second = make_idempotency_key(
            conversation_id=CONVERSATION,
            category=Category.SALON,
            slot_start=start,
            platform_slug="zenoti",
        )
        assert first == second

    async def test_reconciliation_is_audited_both_ends(self) -> None:
        audit = RecordingAudit()
        slot = (await cascade(audit).search([candidate(HappyFake())], request())).slot
        assert slot is not None
        await cascade(audit).book(candidate(InconclusiveTimeoutFake()), slot, consumer_name="Aisha")
        assert audit.of_type("booking.reconciliation_started")
        result = audit.of_type("booking.reconciliation_result")[0]
        assert result["conclusive"] is False


# ---------------------------------------------------------------------------
# 4. Deadline budget — pivot now, finish in the background
# ---------------------------------------------------------------------------


class TestDeadlineBudget:
    async def test_an_expired_budget_stops_the_cascade_and_reports_what_is_left(self) -> None:
        audit = RecordingAudit()
        engine = Cascade(
            audit=audit,
            deadline=Deadline.expired_now(),
            tenant_id=TENANT,
            conversation_id=CONVERSATION,
            correlation_id="test",
        )
        happy = HappyFake()
        result = await engine.search([candidate(EmptyFake()), candidate(happy)], request())

        assert result.status is SearchStatus.DEADLINE_EXCEEDED
        assert result.remaining_candidates, "the cascade must say what it did not get to"

    async def test_the_deadline_is_audited_with_what_remained(self) -> None:
        audit = RecordingAudit()
        engine = Cascade(
            audit=audit,
            deadline=Deadline.expired_now(),
            tenant_id=TENANT,
            conversation_id=CONVERSATION,
            correlation_id="test",
        )
        await engine.search([candidate(EmptyFake()), candidate(HappyFake())], request())
        exceeded = audit.of_type("routing.deadline_exceeded")
        assert len(exceeded) == 1
        assert "fake_happy" in exceeded[0]["remaining_candidates"]

    async def test_the_cascade_can_be_resumed_with_the_remaining_candidates(self) -> None:
        """The holding pivot goes out, then this continues in the background and
        the result is delivered as a follow-up."""
        audit = RecordingAudit()
        expired = Cascade(
            audit=audit,
            deadline=Deadline.expired_now(),
            tenant_id=TENANT,
            conversation_id=CONVERSATION,
            correlation_id="test",
        )
        happy = HappyFake()
        first = await expired.search([candidate(EmptyFake()), candidate(happy)], request())
        assert first.status is SearchStatus.DEADLINE_EXCEEDED

        resumed = await cascade(audit, deadline_ms=5000).search(
            first.remaining_candidates, request()
        )
        assert resumed.status is SearchStatus.FOUND
        assert resumed.slot is not None
        assert resumed.slot.platform_slug == "fake_happy"

    async def test_a_slow_cascade_does_not_run_past_its_budget(self) -> None:
        audit = RecordingAudit()
        engine = cascade(audit, deadline_ms=150)
        slow = [candidate(TimeoutFake(delay_s=0.08)) for _ in range(10)]

        started = asyncio.get_running_loop().time()
        result = await engine.search(slow, request())
        elapsed_ms = (asyncio.get_running_loop().time() - started) * 1000

        assert result.status is SearchStatus.DEADLINE_EXCEEDED
        # Generous bound: the point is that it stops early, not that it is exact.
        assert elapsed_ms < 900, f"cascade ran {elapsed_ms:.0f}ms past a 150ms budget"


# ---------------------------------------------------------------------------
# Low confidence — proposed semantics, flagged in CLAUDE.md
# ---------------------------------------------------------------------------


class TestLowConfidence:
    async def test_a_confident_result_beats_a_low_confidence_one(self) -> None:
        audit = RecordingAudit()
        result = await cascade(audit).search(
            [candidate(LowConfidenceFake()), candidate(HappyFake())], request()
        )
        assert result.status is SearchStatus.FOUND
        assert result.slot is not None
        assert result.slot.platform_slug == "fake_happy"
        assert result.low_confidence is False

    async def test_a_low_confidence_slot_is_used_only_if_nothing_better_appears(self) -> None:
        audit = RecordingAudit()
        result = await cascade(audit).search(
            [candidate(LowConfidenceFake()), candidate(EmptyFake())], request()
        )
        assert result.status is SearchStatus.FOUND
        assert result.low_confidence is True
        assert result.requires_explicit_confirmation is True

    async def test_a_low_confidence_slot_is_never_auto_booked(self) -> None:
        audit = RecordingAudit()
        result = await cascade(audit).search([candidate(LowConfidenceFake())], request())
        assert result.requires_explicit_confirmation is True


# ---------------------------------------------------------------------------
# The policy table itself
# ---------------------------------------------------------------------------


class TestPolicy:
    @pytest.mark.parametrize(
        ("outcome", "expected"),
        [
            (Outcome.OK, RouterAction.USE),
            (Outcome.NO_AVAILABILITY, RouterAction.ADVANCE),
            (Outcome.LOW_CONFIDENCE, RouterAction.HOLD_AS_FALLBACK),
            (Outcome.TIMEOUT, RouterAction.ADVANCE),
            (Outcome.AUTH_ERROR, RouterAction.ADVANCE),
            (Outcome.RATE_LIMITED, RouterAction.ADVANCE),
            (Outcome.UPSTREAM_ERROR, RouterAction.ADVANCE),
            (Outcome.UNSUPPORTED, RouterAction.ADVANCE),
        ],
    )
    def test_read_policy_matches_the_documented_table(
        self, outcome: Outcome, expected: RouterAction
    ) -> None:
        assert decide(outcome, is_create=False).action is expected

    def test_a_create_timeout_reconciles_and_never_advances(self) -> None:
        decision = decide(Outcome.TIMEOUT, is_create=True)
        assert decision.action is RouterAction.RECONCILE
        # Spelled out because advancing here is the double booking.
        assert decision.action.value != RouterAction.ADVANCE.value

    @pytest.mark.parametrize(
        "outcome", [Outcome.AUTH_ERROR, Outcome.RATE_LIMITED, Outcome.UPSTREAM_ERROR]
    )
    def test_operational_failures_are_never_the_consumers_problem(self, outcome: Outcome) -> None:
        assert decide(outcome, is_create=False).consumer_visible is False

    def test_auth_errors_flag_the_credential_for_ops(self) -> None:
        assert decide(Outcome.AUTH_ERROR, is_create=False).degrade_credential is True

    def test_rate_limits_open_a_circuit_breaker(self) -> None:
        assert decide(Outcome.RATE_LIMITED, is_create=False).open_circuit is True

    def test_every_outcome_has_a_decision(self) -> None:
        """No outcome may fall through to undefined behaviour."""
        for outcome in Outcome:
            for is_create in (True, False):
                decision = decide(outcome, is_create=is_create)
                assert decision.reason, f"{outcome}/create={is_create} has no reason"


# ---------------------------------------------------------------------------
# Flaky adapters
# ---------------------------------------------------------------------------


class TestFlaky:
    async def test_a_flaky_first_choice_does_not_cost_the_consumer_anything(self) -> None:
        audit = RecordingAudit()
        result = await cascade(audit).search(
            [candidate(FlakyFake(failure_rate=1.0)), candidate(HappyFake())], request()
        )
        assert result.status is SearchStatus.FOUND
        assert audit.consumer_visible_events == []

    async def test_a_flaky_adapter_is_seeded_and_reproducible(self) -> None:
        """A genuinely flaky test is worse than no test."""
        first = [FlakyFake(seed=42)._should_fail() for _ in range(20)]
        second = [FlakyFake(seed=42)._should_fail() for _ in range(20)]
        assert first == second
