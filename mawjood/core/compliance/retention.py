"""Retention: keeping data no longer than the policy says.

CLAUDE.md §11 commits to structured logs retained 90 days and consumer data
retained for a configurable period. A policy nobody enforces is not a policy, so
this is a job that runs and reports what it removed.

## Two clocks, deliberately different

* **Audit and log rows: 90 days.** These are the operational record — what was
  tried, what came back, why the cascade advanced. Long enough to investigate an
  incident from last quarter, short enough that a breach exposes a quarter
  rather than a history.
* **Consumer data: `data_retention_days`, default 365.** Leads, conversations,
  messages, bookings. Longer because a consumer who books once a year is a
  returning consumer, and forgetting them makes the product worse.

## Consent dies with the data it authorises

An earlier draft of this module kept consent records after the retention sweep,
on the reasoning that they are the evidence we were permitted to hold the data.
That reasoning is wrong, and a test caught it.

A consent row is itself personal data: it is keyed to a lead and carries the
exact wording that person was shown. Retaining it after the conversation,
messages and bookings are gone means holding personal data for longer than the
retention policy allows, with nothing left for it to evidence. The lawful
position is the simpler one — **consent lives exactly as long as the data it
authorises, and goes at the same moment.**

So `consents` is deleted explicitly here rather than left to cascade from
`leads`. The cascade would remove it anyway; doing it by name means the count is
in the report and a reviewer can see it happened.

## A consumer with a future booking is never swept

Their appointment has not happened yet, and deleting them would leave a venue
expecting somebody we no longer know about. The sweep skips them and says so.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
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


async def _delete_count(session: AsyncSession, statement: Any) -> int:
    """Run a DELETE and return how many rows went.

    A one-line helper because ``rowcount`` lives on ``CursorResult`` while
    ``session.execute`` is typed as returning ``Result`` — casting at six call
    sites would be noise, and the count is the only thing any caller wants.
    """
    result = cast("CursorResult[Any]", await session.execute(statement))
    return int(result.rowcount or 0)


@dataclass(frozen=True, slots=True)
class RetentionPolicy:
    """How long each class of data lives."""

    # The operational record. CLAUDE.md §11 commits to 90 days.
    audit_days: int = 90
    # Leads, conversations, messages, bookings.
    consumer_days: int = 365

    def __post_init__(self) -> None:
        if self.audit_days < 1 or self.consumer_days < 1:
            raise ValueError("retention periods must be at least one day")

    @classmethod
    def from_settings(cls, settings: object) -> RetentionPolicy:
        return cls(
            audit_days=int(getattr(settings, "log_retention_days", 90)),
            consumer_days=int(getattr(settings, "data_retention_days", 365)),
        )


@dataclass(frozen=True, slots=True)
class RetentionSweep:
    """What one run removed. The evidence that the policy is enforced."""

    ran_at: datetime
    policy: RetentionPolicy
    audit_cutoff: datetime
    consumer_cutoff: datetime
    deleted: dict[str, int] = field(default_factory=dict)
    # Consumers past the retention date who were kept anyway, because they have a
    # booking that has not happened yet.
    skipped_with_future_bookings: int = 0
    dry_run: bool = False

    @property
    def total_deleted(self) -> int:
        return sum(self.deleted.values())

    def as_evidence(self) -> dict[str, object]:
        return {
            "ran_at": self.ran_at.isoformat(),
            "dry_run": self.dry_run,
            "audit_retention_days": self.policy.audit_days,
            "consumer_retention_days": self.policy.consumer_days,
            "audit_cutoff": self.audit_cutoff.isoformat(),
            "consumer_cutoff": self.consumer_cutoff.isoformat(),
            "rows_deleted": self.deleted,
            "consumers_kept_for_future_bookings": self.skipped_with_future_bookings,
        }


async def sweep_retention(
    session: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    policy: RetentionPolicy | None = None,
    now: datetime | None = None,
    dry_run: bool = False,
) -> RetentionSweep:
    """Delete everything past its retention date. The caller commits.

    ``dry_run`` counts without deleting, which is how an operator checks what a
    first run would do before letting it loose on real data.
    """
    policy = policy or RetentionPolicy()
    moment = now or datetime.now(UTC)
    audit_cutoff = moment - timedelta(days=policy.audit_days)
    consumer_cutoff = moment - timedelta(days=policy.consumer_days)

    deleted: dict[str, int] = {}

    # --- The operational record -------------------------------------------
    audit_filter = (AuditLog.tenant_id == tenant_id, AuditLog.occurred_at < audit_cutoff)
    if dry_run:
        deleted["audit_log"] = int(
            (
                await session.execute(
                    select(func.count()).select_from(AuditLog).where(*audit_filter)
                )
            ).scalar_one()
        )
    else:
        # Same narrow escape hatch erasure uses. Session-local, dies with the
        # transaction. Without it the append-only trigger refuses, which is
        # correct — expiry is still a deliberate act.
        await session.execute(text("SET LOCAL mawjood.allow_purge = 'on'"))
        deleted["audit_log"] = await _delete_count(session, delete(AuditLog).where(*audit_filter))

    # --- Consumers ---------------------------------------------------------
    # Anyone not seen since the cutoff, *except* those with a booking still
    # ahead of them.
    future_booking_leads = (
        select(Booking.lead_id)
        .where(Booking.tenant_id == tenant_id)
        .where(Booking.status == BookingStatus.CONFIRMED)
        .where(Booking.slot_start > moment)
        .scalar_subquery()
    )

    stale = (
        select(Lead.id)
        .where(Lead.tenant_id == tenant_id)
        .where(Lead.last_seen_at < consumer_cutoff)
        .where(Lead.id.notin_(future_booking_leads))
        .scalar_subquery()
    )

    skipped = int(
        (
            await session.execute(
                select(func.count())
                .select_from(Lead)
                .where(Lead.tenant_id == tenant_id)
                .where(Lead.last_seen_at < consumer_cutoff)
                .where(Lead.id.in_(future_booking_leads))
            )
        ).scalar_one()
    )

    # `consents` is here on purpose rather than left to cascade from `leads`.
    # See the module docstring: consent is personal data too, and it dies with
    # the data it authorises. Naming it puts the count in the report.
    children: tuple[tuple[str, Any], ...] = (
        ("feedback", Feedback),
        ("consents", Consent),
        ("scheduled_notifications", ScheduledNotification),
        ("handoff_queue", HandoffQueue),
        ("attribution", Attribution),
        ("messages", Message),
        ("bookings", Booking),
        ("conversations", Conversation),
    )

    for name, model in children:
        if dry_run:
            deleted[name] = int(
                (
                    await session.execute(
                        select(func.count()).select_from(model).where(model.lead_id.in_(stale))
                    )
                ).scalar_one()
            )
        else:
            deleted[name] = await _delete_count(
                session, delete(model).where(model.lead_id.in_(stale))
            )

    if dry_run:
        deleted["leads"] = int(
            (
                await session.execute(
                    select(func.count()).select_from(Lead).where(Lead.id.in_(stale))
                )
            ).scalar_one()
        )
    else:
        deleted["leads"] = await _delete_count(session, delete(Lead).where(Lead.id.in_(stale)))

    sweep = RetentionSweep(
        ran_at=moment,
        policy=policy,
        audit_cutoff=audit_cutoff,
        consumer_cutoff=consumer_cutoff,
        deleted=deleted,
        skipped_with_future_bookings=skipped,
        dry_run=dry_run,
    )

    if not dry_run:
        # The sweep records itself, so "did retention run?" is answerable from
        # the audit log rather than from a cron log nobody keeps.
        session.add(
            AuditLog(
                tenant_id=tenant_id,
                event_type="compliance.retention_swept",
                actor="system",
                details=sweep.as_evidence(),
            )
        )
        await session.flush()

    log.info(
        "retention.swept",
        dry_run=dry_run,
        rows=sweep.total_deleted,
        kept_for_future_bookings=skipped,
    )
    return sweep


__all__ = ["RetentionPolicy", "RetentionSweep", "sweep_retention"]
