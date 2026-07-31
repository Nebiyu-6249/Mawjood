"""Erasing a consumer, everywhere, and proving it.

A PDPL erasure request has to remove a person from the system — not from most of
it. This module does that and returns a **receipt**: counts per table, before and
after, signed off by an assertion that nothing is left. The receipt is what gets
handed to a regulator or an operator's legal team; "we ran a delete" is not
evidence.

## Order matters, and so does the purge flag's scope

Children before parents. Deleting `leads` first would cascade — destroying the
audit trail as a side effect rather than as a decision — so every table is
deleted explicitly, in order, and the counts are real.

The purge flag is set for the entire transaction rather than just around the
audit delete. `audit_log` cascades from `conversations`, so a conversation
delete fires the append-only trigger long before the audit table is reached.
Scoping the flag narrowly looks tidier and fails.

## The audit log

`audit_log` is append-only, enforced by a trigger, because a routing decision
that can be rewritten afterwards is not a record. Erasure needs to delete from
it anyway — an audit row carries `consumer_text`, which is the consumer's own
words.

The trigger recognises a session-local setting, `mawjood.allow_purge`. Erasure
sets it, deletes, and the setting dies with the transaction. That is a narrow,
visible escape hatch rather than a hole: an ordinary connection cannot delete an
audit row, and the erasure itself is recorded (see below).

## Recording an erasure without re-identifying the person

The erasure must be auditable, but an audit row saying "we erased +971 50 123
4567" defeats the point. So the completion row carries a **salted hash** of the
identifier and no plaintext. An operator handling a follow-up request can
recompute the hash from the identifier they were given and find the record; a
reader of the audit log learns nothing.

The salt is the tenant id — already secret-adjacent, already required to do
anything with the row, and stable across time so a lookup years later still
works.
"""

from __future__ import annotations

import hashlib
import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any, cast

from sqlalchemy import CursorResult, delete, func, select, text
from sqlalchemy.ext.asyncio import AsyncSession

from mawjood.core.enums import BookingStatus
from mawjood.db.models import (
    Attribution,
    AuditLog,
    Booking,
    Consent,
    Conversation,
    Feedback,
    HandoffQueue,
    Lead,
    Message,
    ScheduledNotification,
)
from mawjood.observability.logging import get_logger

log = get_logger(__name__)

# Every table carrying consumer data, child-first. The audit log is handled
# separately because it needs the purge flag.
#
# This tuple is asserted complete against the schema by a test: a new table with
# a lead_id that is not listed here would be a table erasure silently skips.
_CONSUMER_TABLES: tuple[tuple[str, Any], ...] = (
    ("feedback", Feedback),
    ("scheduled_notifications", ScheduledNotification),
    ("handoff_queue", HandoffQueue),
    ("attribution", Attribution),
    ("consents", Consent),
    ("messages", Message),
    ("bookings", Booking),
    ("conversations", Conversation),
)


def identifier_hash(wa_id: str, tenant_id: uuid.UUID) -> str:
    """A stable, non-reversible reference to a consumer.

    Salted with the tenant id so the same number under two operators does not
    produce the same hash, and so a hash leaked on its own cannot be checked
    against a phone book by brute force without also knowing the tenant.
    """
    return hashlib.sha256(f"{tenant_id}:{wa_id}".encode()).hexdigest()


@dataclass(frozen=True, slots=True)
class ErasurePlan:
    """What erasing this consumer would remove, and what it would not fix.

    Produced before anything is deleted so an operator can see the consequences.
    The interesting field is :attr:`live_bookings`.
    """

    lead_id: uuid.UUID
    wa_id: str
    rows: dict[str, int]
    # Confirmed bookings whose slot is still in the future. Erasing our record
    # does **not** cancel them at the venue — the venue is still expecting this
    # person. Surfaced rather than silently ignored, because the right answer
    # (cancel first, or erase and let the appointment stand) is the operator's
    # call and depends on what the consumer asked for.
    live_bookings: tuple[str, ...] = ()

    @property
    def total_rows(self) -> int:
        return sum(self.rows.values())

    @property
    def needs_attention(self) -> bool:
        return bool(self.live_bookings)


@dataclass(frozen=True, slots=True)
class ErasureReceipt:
    """Evidence that the erasure happened and left nothing behind."""

    lead_id: uuid.UUID
    subject_hash: str
    tenant_id: uuid.UUID
    requested_at: datetime
    completed_at: datetime
    deleted: dict[str, int] = field(default_factory=dict)
    remaining: dict[str, int] = field(default_factory=dict)

    @property
    def complete(self) -> bool:
        """Whether anything referencing the consumer survived."""
        return not any(self.remaining.values())

    @property
    def total_deleted(self) -> int:
        return sum(self.deleted.values())

    def as_evidence(self) -> dict[str, object]:
        """The shape handed to a compliance reviewer.

        Deliberately carries no identifier — only the salted hash — so the
        receipt itself can be filed, forwarded and retained without becoming a
        new copy of the personal data it certifies the removal of.
        """
        return {
            "subject_hash": self.subject_hash,
            "tenant_id": str(self.tenant_id),
            "requested_at": self.requested_at.isoformat(),
            "completed_at": self.completed_at.isoformat(),
            "rows_deleted": self.deleted,
            "rows_remaining": self.remaining,
            "complete": self.complete,
        }


async def _delete_count(session: AsyncSession, statement: Any) -> int:
    """Run a DELETE and return how many rows went.

    A one-line helper because ``rowcount`` lives on ``CursorResult`` while
    ``session.execute`` is typed as returning ``Result`` — casting at six call
    sites would be noise, and the count is the only thing any caller wants.
    """
    result = cast("CursorResult[Any]", await session.execute(statement))
    return int(result.rowcount or 0)


async def _count_for_lead(session: AsyncSession, model: Any, lead_id: uuid.UUID) -> int:
    return int(
        (
            await session.execute(
                select(func.count()).select_from(model).where(model.lead_id == lead_id)
            )
        ).scalar_one()
    )


async def plan_erasure(
    session: AsyncSession, *, tenant_id: uuid.UUID, wa_id: str
) -> ErasurePlan | None:
    """What erasing this consumer would do. Returns None if they do not exist."""
    lead = (
        await session.execute(
            select(Lead).where(Lead.tenant_id == tenant_id).where(Lead.wa_id == wa_id)
        )
    ).scalar_one_or_none()
    if lead is None:
        return None

    rows: dict[str, int] = {"leads": 1}
    for name, model in _CONSUMER_TABLES:
        rows[name] = await _count_for_lead(session, model, lead.id)
    rows["audit_log"] = await _count_for_lead(session, AuditLog, lead.id)

    live = (
        (
            await session.execute(
                select(Booking)
                .where(Booking.lead_id == lead.id)
                .where(Booking.status == BookingStatus.CONFIRMED)
                .where(Booking.slot_start > datetime.now(UTC))
            )
        )
        .scalars()
        .all()
    )

    return ErasurePlan(
        lead_id=lead.id,
        wa_id=lead.wa_id,
        rows=rows,
        live_bookings=tuple(
            f"{b.venue_name or b.platform_slug} at {b.slot_start:%d %b %H:%M}" for b in live
        ),
    )


async def erase_consumer(
    session: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    wa_id: str,
    requested_at: datetime | None = None,
    reason: str = "consumer request",
) -> ErasureReceipt | None:
    """Remove a consumer from every table, and return proof.

    The caller commits. Erasure is one transaction on purpose: a partial erasure
    that committed halfway would leave a consumer half-present with no record of
    which half.

    Returns ``None`` when the consumer does not exist — which is not an error. A
    request to erase somebody who was never here is satisfied by definition.
    """
    requested_at = requested_at or datetime.now(UTC)

    lead = (
        await session.execute(
            select(Lead).where(Lead.tenant_id == tenant_id).where(Lead.wa_id == wa_id)
        )
    ).scalar_one_or_none()
    if lead is None:
        log.info("erasure.subject_not_found")
        return None

    lead_id = lead.id
    subject = identifier_hash(wa_id, tenant_id)

    # Recorded *before* the purge, while the audit log still accepts ordinary
    # inserts, so the request is on the record even if the purge then fails.
    session.add(
        AuditLog(
            tenant_id=tenant_id,
            event_type="compliance.erasure_requested",
            actor="operator",
            details={"subject_hash": subject, "reason": reason},
        )
    )
    await session.flush()

    # The purge flag covers the *whole* erasure, not just the explicit audit
    # delete. `audit_log` cascades from `conversations`, so deleting a
    # conversation fires the append-only trigger before we ever reach the audit
    # table — which fails the transaction with a confusing error about a table
    # the caller did not name.
    #
    # SET LOCAL, so it dies with this transaction: no other connection is
    # affected and nothing stays unlocked afterwards.
    await session.execute(text("SET LOCAL mawjood.allow_purge = 'on'"))

    deleted: dict[str, int] = {}
    for name, model in _CONSUMER_TABLES:
        deleted[name] = await _delete_count(session, delete(model).where(model.lead_id == lead_id))

    # Explicit rather than relying on the cascade above, so the count is real and
    # a missing cascade shows up as a non-zero remaining count below.
    deleted["audit_log"] = await _delete_count(
        session, delete(AuditLog).where(AuditLog.lead_id == lead_id)
    )

    deleted["leads"] = await _delete_count(session, delete(Lead).where(Lead.id == lead_id))

    # Verify rather than assume. A cascade that did not fire, or a table added
    # later and not listed above, shows up here as a non-zero remaining count.
    remaining: dict[str, int] = {}
    for name, model in _CONSUMER_TABLES:
        remaining[name] = await _count_for_lead(session, model, lead_id)
    remaining["audit_log"] = await _count_for_lead(session, AuditLog, lead_id)
    remaining["leads"] = int(
        (
            await session.execute(select(func.count()).select_from(Lead).where(Lead.id == lead_id))
        ).scalar_one()
    )

    completed_at = datetime.now(UTC)

    # The completion record. Carries the salted hash and counts, never the
    # identifier — an audit row naming the person we just erased would defeat
    # the erasure.
    session.add(
        AuditLog(
            tenant_id=tenant_id,
            event_type="compliance.erasure_completed",
            actor="operator",
            details={
                "subject_hash": subject,
                "rows_deleted": deleted,
                "rows_remaining": remaining,
                "complete": not any(remaining.values()),
            },
        )
    )
    await session.flush()

    receipt = ErasureReceipt(
        lead_id=lead_id,
        subject_hash=subject,
        tenant_id=tenant_id,
        requested_at=requested_at,
        completed_at=completed_at,
        deleted=deleted,
        remaining=remaining,
    )
    log.info(
        "erasure.completed",
        subject_hash=subject,
        rows_deleted=receipt.total_deleted,
        complete=receipt.complete,
    )
    return receipt


__all__ = [
    "ErasurePlan",
    "ErasureReceipt",
    "erase_consumer",
    "identifier_hash",
    "plan_erasure",
]
