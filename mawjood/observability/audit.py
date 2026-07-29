"""The audit trail.

The requirement: **every routing decision must be reconstructable months later**
— the inputs, each aggregator attempted, its outcome and latency, the reason the
cascade advanced, and what the consumer was actually shown.

## How the pieces fit

Three identifiers, nested:

* ``correlation_id`` — the HTTP request or console session. Ties audit rows to the
  JSON log lines emitted alongside them.
* ``turn_id`` — one inbound consumer message and everything done because of it.
  One turn can contain several decisions.
* ``decision_id`` — one cascade run: every aggregator attempt within it, in order.

Reconstruction is then a single ordered read::

    SELECT * FROM audit_log WHERE turn_id = :turn ORDER BY seq;

``seq`` is a database identity, not a timestamp, because timestamps collide at
millisecond resolution and cannot be ordered on alone.

## Why the writes are transactional

Audit rows are written on the same session as the state change they describe. If
the state change rolls back, so does its audit row, and the two can never
disagree about what happened. The cost is that an audit failure fails the turn —
which is the correct trade for a compliance record.

## Why this API is typed

Every method here writes a row with the right columns populated. A free-form
``insert(**anything)`` would produce an audit table that is technically full and
practically unreadable, which is the failure mode this design exists to avoid.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from mawjood.core.enums import Actor, AuditEvent, Category, Outcome
from mawjood.db.models import AuditLog
from mawjood.observability.logging import get_logger

log = get_logger(__name__)

SCHEMA_VERSION = 1


@dataclass(slots=True)
class AuditTrail:
    """Writes the audit trail for one turn.

    Constructed per inbound message. ``turn_id`` is generated if not supplied so
    that a caller cannot accidentally write uncorrelated rows.
    """

    session: AsyncSession
    tenant_id: uuid.UUID
    correlation_id: str
    turn_id: uuid.UUID = field(default_factory=uuid.uuid4)
    conversation_id: uuid.UUID | None = None
    lead_id: uuid.UUID | None = None

    def bind(
        self,
        *,
        conversation_id: uuid.UUID | None = None,
        lead_id: uuid.UUID | None = None,
    ) -> None:
        """Attach ids discovered mid-turn, so later rows carry them."""
        if conversation_id is not None:
            self.conversation_id = conversation_id
        if lead_id is not None:
            self.lead_id = lead_id

    # -- low level ----------------------------------------------------------

    async def record(
        self,
        event_type: str,
        *,
        actor: Actor = Actor.SYSTEM,
        decision_id: uuid.UUID | None = None,
        category: str | None = None,
        platform_slug: str | None = None,
        merchant_ref: str | None = None,
        attempt_number: int | None = None,
        outcome: str | None = None,
        latency_ms: int | None = None,
        advance_reason: str | None = None,
        consumer_visible: bool = False,
        phrasebank_key: str | None = None,
        locale: str | None = None,
        consumer_text: str | None = None,
        inputs: dict[str, Any] | None = None,
        details: dict[str, Any] | None = None,
    ) -> AuditLog:
        row = AuditLog(
            tenant_id=self.tenant_id,
            turn_id=self.turn_id,
            decision_id=decision_id,
            conversation_id=self.conversation_id,
            lead_id=self.lead_id,
            correlation_id=self.correlation_id,
            event_type=event_type,
            actor=str(actor),
            category=category,
            platform_slug=platform_slug,
            merchant_ref=merchant_ref,
            attempt_number=attempt_number,
            outcome=outcome,
            latency_ms=latency_ms,
            advance_reason=advance_reason,
            consumer_visible=consumer_visible,
            phrasebank_key=phrasebank_key,
            locale=locale,
            consumer_text=consumer_text,
            inputs=inputs or {},
            details=details or {},
            schema_version=SCHEMA_VERSION,
        )
        self.session.add(row)
        await self.session.flush()

        # Mirror to the structured log so an operator reading logs and an auditor
        # reading the database see the same event under the same correlation id.
        log.info(
            event_type,
            turn_id=str(self.turn_id),
            decision_id=str(decision_id) if decision_id else None,
            conversation_id=str(self.conversation_id) if self.conversation_id else None,
            platform_slug=platform_slug,
            outcome=outcome,
            latency_ms=latency_ms,
            advance_reason=advance_reason,
        )
        return row

    # -- message lifecycle --------------------------------------------------

    async def message_received(
        self, *, body: str, provider_message_id: str | None, channel: str
    ) -> AuditLog:
        return await self.record(
            AuditEvent.MESSAGE_RECEIVED,
            actor=Actor.CONSUMER,
            inputs={
                "channel": channel,
                "provider_message_id": provider_message_id,
                "body_length": len(body),
            },
        )

    async def duplicate_ignored(self, *, provider_message_id: str, channel: str) -> AuditLog:
        return await self.record(
            AuditEvent.MESSAGE_DUPLICATE_IGNORED,
            inputs={"channel": channel, "provider_message_id": provider_message_id},
            details={"reason": "provider message id already processed"},
        )

    async def message_sent(
        self, *, phrasebank_key: str, locale: str, text: str, channel: str
    ) -> AuditLog:
        """What the consumer was shown. The 'what did they see' half of a replay."""
        return await self.record(
            AuditEvent.MESSAGE_SENT,
            consumer_visible=True,
            phrasebank_key=phrasebank_key,
            locale=locale,
            consumer_text=text,
            details={"channel": channel},
        )

    # -- lead and conversation ---------------------------------------------

    async def lead_created(self) -> AuditLog:
        return await self.record(AuditEvent.LEAD_CREATED)

    async def conversation_started(self, *, channel: str) -> AuditLog:
        return await self.record(AuditEvent.CONVERSATION_STARTED, details={"channel": channel})

    async def conversation_resumed(self, *, channel: str) -> AuditLog:
        return await self.record(AuditEvent.CONVERSATION_RESUMED, details={"channel": channel})

    # -- consent ------------------------------------------------------------

    async def consent_notice_shown(
        self, *, policy_version: str, wording: str, locale: str
    ) -> AuditLog:
        return await self.record(
            AuditEvent.CONSENT_NOTICE_SHOWN,
            locale=locale,
            details={"policy_version": policy_version, "wording": wording},
        )

    async def consent_granted(self, *, policy_version: str, evidence: str) -> AuditLog:
        return await self.record(
            AuditEvent.CONSENT_GRANTED,
            actor=Actor.CONSUMER,
            details={"policy_version": policy_version, "evidence": evidence},
        )

    async def consent_withdrawn(self, *, policy_version: str, evidence: str) -> AuditLog:
        return await self.record(
            AuditEvent.CONSENT_WITHDRAWN,
            actor=Actor.CONSUMER,
            details={"policy_version": policy_version, "evidence": evidence},
        )

    # -- routing and the cascade (used from Phase 2) ------------------------

    def new_decision(self) -> uuid.UUID:
        """Mint a decision id grouping one cascade run's attempts."""
        return uuid.uuid4()

    async def routing_started(
        self,
        *,
        decision_id: uuid.UUID,
        category: Category | str,
        candidates: list[str],
        request: dict[str, Any],
        deadline_ms: int,
    ) -> AuditLog:
        """The inputs half of a replay: what we asked, and who we planned to ask."""
        return await self.record(
            AuditEvent.ROUTING_STARTED,
            decision_id=decision_id,
            category=str(category),
            inputs=request,
            details={"candidates": candidates, "deadline_ms": deadline_ms},
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
    ) -> AuditLog:
        """One aggregator call: who, what came back, how long it took."""
        payload = dict(details or {})
        if raw_error is not None:
            # Kept for operators. Never shown to a consumer.
            payload["raw_error"] = raw_error
        if request_id is not None:
            payload["upstream_request_id"] = request_id
        return await self.record(
            AuditEvent.ROUTING_ATTEMPT,
            actor=Actor.AGGREGATOR,
            decision_id=decision_id,
            attempt_number=attempt_number,
            category=str(category),
            platform_slug=platform_slug,
            merchant_ref=merchant_ref,
            outcome=str(outcome),
            latency_ms=latency_ms,
            details=payload,
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
    ) -> AuditLog:
        """Why the cascade moved on. The first column an incident review reads."""
        return await self.record(
            AuditEvent.ROUTING_ADVANCED,
            decision_id=decision_id,
            attempt_number=attempt_number,
            platform_slug=from_platform,
            outcome=str(outcome),
            advance_reason=advance_reason,
            details={"next_platform": to_platform},
        )

    async def routing_succeeded(
        self,
        *,
        decision_id: uuid.UUID,
        platform_slug: str,
        attempt_number: int,
        latency_ms: int,
        details: dict[str, Any] | None = None,
    ) -> AuditLog:
        return await self.record(
            AuditEvent.ROUTING_SUCCEEDED,
            decision_id=decision_id,
            platform_slug=platform_slug,
            attempt_number=attempt_number,
            outcome=str(Outcome.OK),
            latency_ms=latency_ms,
            details=details or {},
        )

    async def routing_exhausted(
        self, *, decision_id: uuid.UUID, attempted: list[str], advance_reason: str
    ) -> AuditLog:
        """Every candidate tried and none served. The consumer is handed to a
        human — never told the shelves are empty."""
        return await self.record(
            AuditEvent.ROUTING_EXHAUSTED,
            decision_id=decision_id,
            advance_reason=advance_reason,
            details={"attempted": attempted, "attempt_count": len(attempted)},
        )

    async def deadline_exceeded(
        self, *, decision_id: uuid.UUID, elapsed_ms: int, budget_ms: int, remaining: list[str]
    ) -> AuditLog:
        """Budget blown mid-cascade: pivot now, finish in the background."""
        return await self.record(
            AuditEvent.ROUTING_DEADLINE_EXCEEDED,
            decision_id=decision_id,
            latency_ms=elapsed_ms,
            advance_reason="turn deadline exceeded; continuing in background",
            details={"budget_ms": budget_ms, "remaining_candidates": remaining},
        )

    # -- booking safety -----------------------------------------------------

    async def reconciliation_started(
        self, *, decision_id: uuid.UUID, platform_slug: str, idempotency_key: str
    ) -> AuditLog:
        return await self.record(
            AuditEvent.BOOKING_RECONCILIATION_STARTED,
            decision_id=decision_id,
            platform_slug=platform_slug,
            advance_reason="create_booking timed out; outcome unknown",
            details={"idempotency_key": idempotency_key},
        )

    async def reconciliation_result(
        self,
        *,
        decision_id: uuid.UUID,
        platform_slug: str,
        conclusive: bool,
        booked: bool | None,
        latency_ms: int,
    ) -> AuditLog:
        return await self.record(
            AuditEvent.BOOKING_RECONCILIATION_RESULT,
            decision_id=decision_id,
            platform_slug=platform_slug,
            latency_ms=latency_ms,
            advance_reason=(
                "reconciliation conclusive" if conclusive else "reconciliation inconclusive"
            ),
            details={"conclusive": conclusive, "booked": booked},
        )

    # -- handoff ------------------------------------------------------------

    async def handoff_enqueued(
        self, *, reason: str, decision_id: uuid.UUID | None = None, context: dict[str, Any]
    ) -> AuditLog:
        return await self.record(
            AuditEvent.HANDOFF_ENQUEUED,
            decision_id=decision_id,
            advance_reason=reason,
            details=context,
        )


# ---------------------------------------------------------------------------
# Reconstruction
# ---------------------------------------------------------------------------


async def replay_turn(
    session: AsyncSession, tenant_id: uuid.UUID, turn_id: uuid.UUID
) -> list[AuditLog]:
    """Every audit row for one turn, in the order it happened."""
    result = await session.execute(
        select(AuditLog)
        .where(AuditLog.tenant_id == tenant_id)
        .where(AuditLog.turn_id == turn_id)
        .order_by(AuditLog.seq)
    )
    return list(result.scalars().all())


async def replay_conversation(
    session: AsyncSession, tenant_id: uuid.UUID, conversation_id: uuid.UUID
) -> list[AuditLog]:
    result = await session.execute(
        select(AuditLog)
        .where(AuditLog.tenant_id == tenant_id)
        .where(AuditLog.conversation_id == conversation_id)
        .order_by(AuditLog.seq)
    )
    return list(result.scalars().all())


def format_trail(rows: list[AuditLog]) -> str:
    """Render an audit trail for a human.

    Used by ``tools/chat_sim.py --audit`` and by incident review. Deliberately
    plain text: the point is that someone can read it months later without
    tooling.
    """
    lines: list[str] = []
    for row in rows:
        parts = [f"[{row.seq:>4}] {row.occurred_at:%H:%M:%S.%f}"[:-3], f"{row.event_type:<34}"]
        if row.platform_slug:
            attempt = f"#{row.attempt_number}" if row.attempt_number is not None else ""
            parts.append(f"{row.platform_slug}{attempt}")
        if row.outcome:
            parts.append(f"-> {row.outcome}")
        if row.latency_ms is not None:
            parts.append(f"({row.latency_ms}ms)")
        if row.advance_reason:
            parts.append(f"| {row.advance_reason}")
        if row.consumer_visible and row.consumer_text:
            parts.append(f'| consumer saw: "{row.consumer_text}"')
        lines.append(" ".join(parts))
    return "\n".join(lines)


__all__ = [
    "SCHEMA_VERSION",
    "AuditTrail",
    "format_trail",
    "replay_conversation",
    "replay_turn",
]
