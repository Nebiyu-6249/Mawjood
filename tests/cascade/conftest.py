"""Cascade test fixtures.

The cascade writes to an audit sink but does not care what kind. ``RecordingAudit``
captures calls in memory so cascade behaviour can be asserted without a database,
which keeps this suite fast enough to run on every keystroke.

It satisfies the same structural interface as the real ``AuditTrail``, and
``test_audit_sink_parity`` below asserts that — a recorder that has drifted from
the thing it stands in for proves nothing.
"""

from __future__ import annotations

import uuid
from typing import Any

from mawjood.core.enums import Actor, AuditEvent, Category, Outcome


class RecordingAudit:
    """In-memory audit sink."""

    def __init__(self) -> None:
        self.rows: list[dict[str, Any]] = []
        self.turn_id = uuid.uuid4()

    # -- helpers used by assertions ---------------------------------------

    def of_type(self, event_type: str) -> list[dict[str, Any]]:
        return [row for row in self.rows if row["event_type"] == event_type]

    @property
    def event_types(self) -> list[str]:
        return [row["event_type"] for row in self.rows]

    @property
    def consumer_visible_events(self) -> list[dict[str, Any]]:
        return [row for row in self.rows if row.get("consumer_visible")]

    def _add(self, event_type: str, **fields: Any) -> dict[str, Any]:
        row = {"event_type": event_type, **fields}
        self.rows.append(row)
        return row

    # -- the interface the cascade uses -----------------------------------

    def new_decision(self) -> uuid.UUID:
        return uuid.uuid4()

    async def routing_started(
        self,
        *,
        decision_id: uuid.UUID,
        category: Category | str,
        candidates: list[str],
        request: dict[str, Any],
        deadline_ms: int,
    ) -> dict[str, Any]:
        return self._add(
            AuditEvent.ROUTING_STARTED,
            decision_id=decision_id,
            category=str(category),
            candidates=candidates,
            request=request,
            deadline_ms=deadline_ms,
        )

    async def routing_attempt(
        self,
        *,
        decision_id: uuid.UUID,
        attempt_number: int,
        category: Category | str,
        platform_slug: str,
        outcome: Outcome | str,
        latency_ms: int,
        merchant_ref: str | None = None,
        raw_error: str | None = None,
        request_id: str | None = None,
        details: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        return self._add(
            AuditEvent.ROUTING_ATTEMPT,
            decision_id=decision_id,
            attempt_number=attempt_number,
            category=str(category),
            platform_slug=platform_slug,
            outcome=outcome,
            latency_ms=latency_ms,
            merchant_ref=merchant_ref,
            raw_error=raw_error,
            request_id=request_id,
            details=details or {},
            actor=Actor.AGGREGATOR,
        )

    async def routing_advanced(
        self,
        *,
        decision_id: uuid.UUID,
        attempt_number: int,
        from_platform: str,
        to_platform: str | None,
        outcome: Outcome | str,
        advance_reason: str,
    ) -> dict[str, Any]:
        return self._add(
            AuditEvent.ROUTING_ADVANCED,
            decision_id=decision_id,
            attempt_number=attempt_number,
            from_platform=from_platform,
            to_platform=to_platform,
            outcome=outcome,
            advance_reason=advance_reason,
        )

    async def routing_succeeded(
        self,
        *,
        decision_id: uuid.UUID,
        platform_slug: str,
        attempt_number: int,
        latency_ms: int,
        details: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        return self._add(
            AuditEvent.ROUTING_SUCCEEDED,
            decision_id=decision_id,
            platform_slug=platform_slug,
            attempt_number=attempt_number,
            latency_ms=latency_ms,
            details=details or {},
        )

    async def routing_exhausted(
        self, *, decision_id: uuid.UUID, attempted: list[str], advance_reason: str
    ) -> dict[str, Any]:
        return self._add(
            AuditEvent.ROUTING_EXHAUSTED,
            decision_id=decision_id,
            attempted=attempted,
            advance_reason=advance_reason,
        )

    async def deadline_exceeded(
        self, *, decision_id: uuid.UUID, elapsed_ms: int, budget_ms: int, remaining: list[str]
    ) -> dict[str, Any]:
        return self._add(
            AuditEvent.ROUTING_DEADLINE_EXCEEDED,
            decision_id=decision_id,
            elapsed_ms=elapsed_ms,
            budget_ms=budget_ms,
            remaining_candidates=remaining,
        )

    async def reconciliation_started(
        self, *, decision_id: uuid.UUID, platform_slug: str, idempotency_key: str
    ) -> dict[str, Any]:
        return self._add(
            AuditEvent.BOOKING_RECONCILIATION_STARTED,
            decision_id=decision_id,
            platform_slug=platform_slug,
            idempotency_key=idempotency_key,
        )

    async def reconciliation_result(
        self,
        *,
        decision_id: uuid.UUID,
        platform_slug: str,
        conclusive: bool,
        booked: bool | None,
        latency_ms: int,
    ) -> dict[str, Any]:
        return self._add(
            AuditEvent.BOOKING_RECONCILIATION_RESULT,
            decision_id=decision_id,
            platform_slug=platform_slug,
            conclusive=conclusive,
            booked=booked,
            latency_ms=latency_ms,
        )

    async def handoff_enqueued(
        self, *, reason: str, decision_id: uuid.UUID | None = None, context: dict[str, Any]
    ) -> dict[str, Any]:
        return self._add(
            AuditEvent.HANDOFF_ENQUEUED,
            decision_id=decision_id,
            advance_reason=reason,
            context=context,
        )

    async def message_sent(
        self, *, phrasebank_key: str, locale: str, text: str, channel: str
    ) -> dict[str, Any]:
        return self._add(
            AuditEvent.MESSAGE_SENT,
            consumer_visible=True,
            phrasebank_key=phrasebank_key,
            locale=locale,
            consumer_text=text,
            channel=channel,
        )


def test_audit_sink_parity() -> None:
    """RecordingAudit must expose everything the real AuditTrail does.

    Otherwise the cascade tests pass against a recorder that no longer resembles
    production, and a missing audit call goes unnoticed until an incident.
    """
    import inspect

    from mawjood.observability.audit import AuditTrail

    used_by_cascade = {
        "new_decision",
        "routing_started",
        "routing_attempt",
        "routing_advanced",
        "routing_succeeded",
        "routing_exhausted",
        "deadline_exceeded",
        "reconciliation_started",
        "reconciliation_result",
        "handoff_enqueued",
    }

    for name in sorted(used_by_cascade):
        assert hasattr(RecordingAudit, name), f"RecordingAudit is missing {name}"
        assert hasattr(AuditTrail, name), f"AuditTrail is missing {name}"

        real = inspect.signature(getattr(AuditTrail, name))
        fake = inspect.signature(getattr(RecordingAudit, name))
        real_kwargs = {p for p in real.parameters if p != "self"}
        fake_kwargs = {p for p in fake.parameters if p != "self"}
        missing = real_kwargs - fake_kwargs
        assert not missing, f"RecordingAudit.{name} is missing parameters: {missing}"
