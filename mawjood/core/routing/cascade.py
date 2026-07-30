"""The fallback cascade — the invariant at runtime.

Walks a category's ordered candidates until one serves, and when none does, ends
in a controlled handoff rather than an exception. It never speaks to the consumer:
it returns a status, and the conversation layer renders a phrasebank pivot. That
separation is what keeps failure language structurally impossible.

Two operations:

* :meth:`Cascade.search` — walk candidates for availability.
* :meth:`Cascade.book`   — create against one candidate, with the
  timeout-reconciliation defence.

Three things this file exists to get right:

1. **Never double-book.** A create timeout reconciles; an inconclusive
   reconciliation goes to a human and *never* advances (CLAUDE.md section 5.1).
2. **Never overrun the turn budget.** When the deadline expires mid-walk it
   returns DEADLINE_EXCEEDED with the untried candidates, so the caller can send
   a holding pivot and finish in the background (section 5.2).
3. **Never raise into the conversation.** An adapter that breaks its contract and
   raises anyway is caught here and treated as an upstream error.
"""

from __future__ import annotations

import time
import uuid
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from enum import StrEnum, auto
from typing import Any, Protocol

from mawjood.core.aggregators.base import (
    AggregatorAdapter,
    AvailabilityRequest,
    BookingRef,
    BookingRequest,
    CallContext,
    Deadline,
    MerchantRef,
    Result,
    Slot,
    make_idempotency_key,
)
from mawjood.core.enums import Category, Outcome
from mawjood.core.routing.policy import RouterAction, decide
from mawjood.observability.alerts import AlertKind, Severity, alert
from mawjood.observability.logging import get_logger

log = get_logger(__name__)


class SearchStatus(StrEnum):
    FOUND = auto()
    # Every candidate tried, none served. A human takes over.
    EXHAUSTED = auto()
    # Budget ran out mid-walk. Pivot now, continue in the background.
    DEADLINE_EXCEEDED = auto()


class BookStatus(StrEnum):
    BOOKED = auto()
    # The create failed cleanly and it is safe to try elsewhere.
    FAILED = auto()
    # We do not know whether it landed. Human, not a retry.
    UNCERTAIN = auto()


@dataclass(frozen=True, slots=True)
class Candidate:
    """One merchant on one platform, with its credentials already resolved."""

    adapter: AggregatorAdapter
    merchant: MerchantRef
    credentials: Mapping[str, str] = field(default_factory=dict)

    @property
    def slug(self) -> str:
        return self.merchant.platform_slug

    def __str__(self) -> str:
        return str(self.merchant)


@dataclass(frozen=True, slots=True)
class SearchOutcome:
    status: SearchStatus
    slot: Slot | None = None
    candidate: Candidate | None = None
    attempted: tuple[str, ...] = ()
    remaining_candidates: tuple[Candidate, ...] = ()
    decision_id: uuid.UUID | None = None
    # True when the only thing found came back LOW_CONFIDENCE.
    low_confidence: bool = False
    handoff_reason: str | None = None

    @property
    def requires_explicit_confirmation(self) -> bool:
        """A low-confidence slot is never auto-booked."""
        return self.low_confidence


@dataclass(frozen=True, slots=True)
class BookOutcome:
    status: BookStatus
    ref: BookingRef | None = None
    candidate: Candidate | None = None
    decision_id: uuid.UUID | None = None
    handoff_reason: str | None = None
    reason: str | None = None


class CascadeAudit(Protocol):
    """What the cascade needs from an audit sink.

    A Protocol so tests can supply an in-memory recorder and production supplies
    the database-backed AuditTrail, with no branch in this file.
    """

    def new_decision(self) -> uuid.UUID: ...
    async def routing_started(
        self,
        *,
        decision_id: uuid.UUID,
        category: Category | str,
        candidates: list[str],
        request: dict[str, Any],
        deadline_ms: int,
    ) -> Any: ...
    async def routing_attempt(
        self,
        *,
        decision_id: uuid.UUID,
        attempt_number: int,
        category: Category | str,
        platform_slug: str,
        outcome: Outcome | str,
        latency_ms: int,
        merchant_ref: str | None = ...,
        raw_error: str | None = ...,
        request_id: str | None = ...,
        details: dict[str, Any] | None = ...,
    ) -> Any: ...
    async def routing_advanced(
        self,
        *,
        decision_id: uuid.UUID,
        attempt_number: int,
        from_platform: str,
        to_platform: str | None,
        outcome: Outcome | str,
        advance_reason: str,
    ) -> Any: ...
    async def routing_succeeded(
        self,
        *,
        decision_id: uuid.UUID,
        platform_slug: str,
        attempt_number: int,
        latency_ms: int,
        details: dict[str, Any] | None = ...,
    ) -> Any: ...
    async def routing_exhausted(
        self, *, decision_id: uuid.UUID, attempted: list[str], advance_reason: str
    ) -> Any: ...
    async def deadline_exceeded(
        self, *, decision_id: uuid.UUID, elapsed_ms: int, budget_ms: int, remaining: list[str]
    ) -> Any: ...
    async def reconciliation_started(
        self, *, decision_id: uuid.UUID, platform_slug: str, idempotency_key: str
    ) -> Any: ...
    async def reconciliation_result(
        self,
        *,
        decision_id: uuid.UUID,
        platform_slug: str,
        conclusive: bool,
        booked: bool | None,
        latency_ms: int,
    ) -> Any: ...


class Cascade:
    """One cascade run over a candidate list."""

    def __init__(
        self,
        *,
        audit: CascadeAudit,
        deadline: Deadline,
        tenant_id: uuid.UUID,
        conversation_id: uuid.UUID,
        correlation_id: str,
    ) -> None:
        self.audit = audit
        self.deadline = deadline
        self.tenant_id = tenant_id
        self.conversation_id = conversation_id
        self.correlation_id = correlation_id
        self.budget_ms = deadline.remaining_ms

    # -- context ------------------------------------------------------------

    def _context(self, candidate: Candidate) -> CallContext:
        return CallContext(
            tenant_id=self.tenant_id,
            conversation_id=self.conversation_id,
            merchant=candidate.merchant,
            credentials=candidate.credentials,
            deadline=self.deadline,
            correlation_id=self.correlation_id,
        )

    async def _call(self, description: str, coro: Any) -> Result[Any]:
        """Invoke an adapter, converting a contract breach into an outcome.

        Adapters are required not to raise. One that does anyway must not take the
        conversation down with it — that is precisely how a consumer ends up
        seeing a failure.
        """
        started = time.perf_counter()
        try:
            result: Result[Any] = await coro
        except Exception as exc:  # adapter broke its contract
            latency = int((time.perf_counter() - started) * 1000)
            log.exception("aggregator.raised_into_router", call=description)
            return Result(
                outcome=Outcome.UPSTREAM_ERROR,
                raw_error=f"adapter raised {type(exc).__name__}: {exc}",
                latency_ms=latency,
            )
        return result

    # -- search -------------------------------------------------------------

    async def search(
        self, candidates: Sequence[Candidate], req: AvailabilityRequest
    ) -> SearchOutcome:
        """Walk candidates until one has a slot."""
        decision_id = self.audit.new_decision()
        pending = list(candidates)

        await self.audit.routing_started(
            decision_id=decision_id,
            category=req.category,
            candidates=[c.slug for c in pending],
            request=req.as_audit_inputs(),
            deadline_ms=self.budget_ms,
        )

        attempted: list[str] = []
        fallback: tuple[Slot, Candidate] | None = None

        for index, candidate in enumerate(pending):
            remaining = tuple(pending[index:])

            if self.deadline.expired:
                # Budget gone. Report what is left so the caller can pivot now
                # and finish in the background.
                await self.audit.deadline_exceeded(
                    decision_id=decision_id,
                    elapsed_ms=self.budget_ms - self.deadline.remaining_ms,
                    budget_ms=self.budget_ms,
                    remaining=[c.slug for c in remaining],
                )
                if fallback is not None:
                    slot, owner = fallback
                    return SearchOutcome(
                        status=SearchStatus.FOUND,
                        slot=slot,
                        candidate=owner,
                        attempted=tuple(attempted),
                        decision_id=decision_id,
                        low_confidence=True,
                    )
                return SearchOutcome(
                    status=SearchStatus.DEADLINE_EXCEEDED,
                    attempted=tuple(attempted),
                    remaining_candidates=remaining,
                    decision_id=decision_id,
                )

            attempt_number = index + 1
            attempted.append(candidate.slug)
            ctx = self._context(candidate)

            result = await self._call(
                f"search_availability/{candidate.slug}",
                candidate.adapter.search_availability(ctx, req),
            )

            await self.audit.routing_attempt(
                decision_id=decision_id,
                attempt_number=attempt_number,
                category=req.category,
                platform_slug=candidate.slug,
                merchant_ref=str(candidate.merchant),
                outcome=result.outcome,
                latency_ms=result.latency_ms,
                raw_error=result.raw_error,
                request_id=result.request_id,
            )

            decision = decide(result.outcome, is_create=False)
            slots = result.data or []

            if decision.action is RouterAction.USE and slots:
                await self.audit.routing_succeeded(
                    decision_id=decision_id,
                    platform_slug=candidate.slug,
                    attempt_number=attempt_number,
                    latency_ms=result.latency_ms,
                    details={"slots_offered": len(slots)},
                )
                return SearchOutcome(
                    status=SearchStatus.FOUND,
                    slot=slots[0],
                    candidate=candidate,
                    attempted=tuple(attempted),
                    decision_id=decision_id,
                )

            if decision.action is RouterAction.HOLD_AS_FALLBACK and slots and fallback is None:
                # Keep it, keep looking. Only surfaced if nothing better appears,
                # and then only with an explicit confirmation.
                fallback = (slots[0], candidate)

            self._log_operational(candidate, decision, result)

            next_slug = pending[index + 1].slug if index + 1 < len(pending) else None
            await self.audit.routing_advanced(
                decision_id=decision_id,
                attempt_number=attempt_number,
                from_platform=candidate.slug,
                to_platform=next_slug,
                outcome=result.outcome,
                advance_reason=decision.reason,
            )

        if fallback is not None:
            slot, owner = fallback
            return SearchOutcome(
                status=SearchStatus.FOUND,
                slot=slot,
                candidate=owner,
                attempted=tuple(attempted),
                decision_id=decision_id,
                low_confidence=True,
            )

        await self.audit.routing_exhausted(
            decision_id=decision_id,
            attempted=attempted,
            advance_reason="every candidate exhausted without availability",
        )
        return SearchOutcome(
            status=SearchStatus.EXHAUSTED,
            attempted=tuple(attempted),
            decision_id=decision_id,
            handoff_reason="cascade_exhausted",
        )

    # -- book ---------------------------------------------------------------

    async def book(
        self,
        candidate: Candidate,
        slot: Slot,
        *,
        category: Category = Category.OTHER,
        consumer_name: str | None = None,
        consumer_phone: str | None = None,
        fallbacks: Sequence[Candidate] = (),
        decision_id: uuid.UUID | None = None,
    ) -> BookOutcome:
        """Create a booking, defending against the timeout case.

        ``fallbacks`` are candidates the caller *would* try if this create failed
        cleanly. They are deliberately never used after an inconclusive timeout —
        that is the double booking.
        """
        decision_id = decision_id or self.audit.new_decision()
        ctx = self._context(candidate)

        idempotency_key = make_idempotency_key(
            conversation_id=self.conversation_id,
            category=category,
            slot_start=slot.start,
            platform_slug=candidate.slug,
        )
        request = BookingRequest(
            slot=slot,
            idempotency_key=idempotency_key,
            consumer_name=consumer_name,
            consumer_phone=consumer_phone,
        )

        result = await self._call(
            f"create_booking/{candidate.slug}",
            candidate.adapter.create_booking(ctx, request),
        )

        await self.audit.routing_attempt(
            decision_id=decision_id,
            attempt_number=1,
            category=category,
            platform_slug=candidate.slug,
            merchant_ref=str(candidate.merchant),
            outcome=result.outcome,
            latency_ms=result.latency_ms,
            raw_error=result.raw_error,
            request_id=result.request_id,
            details={"idempotency_key": idempotency_key},
        )

        decision = decide(result.outcome, is_create=True)

        if decision.action is RouterAction.USE and result.data is not None:
            await self.audit.routing_succeeded(
                decision_id=decision_id,
                platform_slug=candidate.slug,
                attempt_number=1,
                latency_ms=result.latency_ms,
                details={"external_id": result.data.external_id},
            )
            return BookOutcome(
                status=BookStatus.BOOKED,
                ref=result.data,
                candidate=candidate,
                decision_id=decision_id,
            )

        if decision.action is RouterAction.RECONCILE:
            return await self._reconcile(candidate, ctx, idempotency_key, decision_id=decision_id)

        self._log_operational(candidate, decision, result)
        return BookOutcome(
            status=BookStatus.FAILED,
            candidate=candidate,
            decision_id=decision_id,
            reason=decision.reason,
        )

    async def _reconcile(
        self,
        candidate: Candidate,
        ctx: CallContext,
        idempotency_key: str,
        *,
        decision_id: uuid.UUID,
    ) -> BookOutcome:
        """Find out whether a timed-out create actually landed.

        Three answers, three very different consequences:

        * **It landed** → treat as booked. Recovering it is the whole point.
        * **It provably did not** → safe to fail cleanly and try elsewhere.
        * **Cannot tell** → a human takes over. Never a retry: retrying on
          "maybe" is how a consumer gets booked twice.
        """
        await self.audit.reconciliation_started(
            decision_id=decision_id,
            platform_slug=candidate.slug,
            idempotency_key=idempotency_key,
        )

        result = await self._call(
            f"find_by_idempotency_key/{candidate.slug}",
            candidate.adapter.find_by_idempotency_key(ctx, idempotency_key),
        )

        if result.outcome is Outcome.OK and result.data is not None:
            await self.audit.reconciliation_result(
                decision_id=decision_id,
                platform_slug=candidate.slug,
                conclusive=True,
                booked=True,
                latency_ms=result.latency_ms,
            )
            return BookOutcome(
                status=BookStatus.BOOKED,
                ref=result.data.ref,
                candidate=candidate,
                decision_id=decision_id,
                reason="recovered by reconciliation after a create timeout",
            )

        if result.outcome is Outcome.NO_AVAILABILITY:
            # Conclusive negative: nothing was created, so trying elsewhere is safe.
            await self.audit.reconciliation_result(
                decision_id=decision_id,
                platform_slug=candidate.slug,
                conclusive=True,
                booked=False,
                latency_ms=result.latency_ms,
            )
            return BookOutcome(
                status=BookStatus.FAILED,
                candidate=candidate,
                decision_id=decision_id,
                reason="create timed out and reconciliation confirmed nothing was created",
            )

        await self.audit.reconciliation_result(
            decision_id=decision_id,
            platform_slug=candidate.slug,
            conclusive=False,
            booked=None,
            latency_ms=result.latency_ms,
        )
        # The highest-stakes alert in the system. We do not know whether this
        # consumer has a booking, and only a person contacting the venue can find
        # out. Until they do, the consumer must not be told either way.
        alert(
            AlertKind.RECONCILIATION_INCONCLUSIVE,
            severity=Severity.CRITICAL,
            summary=(
                f"{candidate.slug}: create timed out and reconciliation was "
                "inconclusive — a human must confirm with the venue"
            ),
            platform=candidate.slug,
            merchant_ref=str(candidate.merchant),
            decision_id=str(decision_id),
        )
        return BookOutcome(
            status=BookStatus.UNCERTAIN,
            candidate=candidate,
            decision_id=decision_id,
            handoff_reason="reconciliation_inconclusive",
            reason="create timed out and reconciliation could not determine the outcome",
        )

    # -- operational logging ------------------------------------------------

    def _log_operational(self, candidate: Candidate, decision: Any, result: Result[Any]) -> None:
        """Surface our problems to operators, never to consumers.

        Both conditions here are alerts rather than log lines, because both keep
        happening until a person intervenes and neither is discoverable from
        outside: a rejected credential silently degrades every consumer routed to
        that merchant, and an open circuit silently removes a platform from the
        cascade.
        """
        if decision.degrade_credential:
            alert(
                AlertKind.CREDENTIAL_DEGRADED,
                severity=Severity.ERROR,
                summary=f"{candidate.slug} rejected our credential for {candidate.merchant.display_name}",
                platform=candidate.slug,
                merchant_ref=str(candidate.merchant),
                raw_error=result.raw_error,
            )
        if decision.open_circuit:
            alert(
                AlertKind.AGGREGATOR_CIRCUIT_OPEN,
                severity=Severity.WARNING,
                summary=f"{candidate.slug} is rate limiting us; circuit opened",
                platform=candidate.slug,
                merchant_ref=str(candidate.merchant),
            )


__all__ = [
    "BookOutcome",
    "BookStatus",
    "Candidate",
    "Cascade",
    "CascadeAudit",
    "SearchOutcome",
    "SearchStatus",
]
