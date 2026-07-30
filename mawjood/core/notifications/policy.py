"""When a notification is due, and whether it may be sent at all.

Pure functions over explicit inputs. No clock, no database, no network — the
caller passes ``now``, so a test can move time without patching anything.

There are two independent questions and it matters that they stay independent:

1. **Is it due?** Purely arithmetic, off the booking's slot times.
2. **May it be sent?** A WhatsApp policy question, answered by the 24-hour
   session window and the template registry.

Collapsing them produces a scheduler that quietly drops notifications and calls
it "not due". Keeping them apart means a blocked send is *visibly* blocked, with
a reason, which is the whole point of the deliverable.

## The 24-hour window

WhatsApp permits free-form messages only within 24 hours of the consumer's last
inbound message. Outside that window a business may only send a pre-approved
message template. Mawjood has **no approved templates** — none have been
submitted, because that needs the operator's Business Manager account (PLAN.md
lists Meta template approval as external lead time).

The honest consequence: an out-of-window notification **cannot be sent**, and the
scheduler says so. It does not send anyway, it does not silently drop it, and it
does not mark it sent. ``Verdict.BLOCKED_NO_APPROVED_TEMPLATE`` is a real
outcome that shows up in the audit trail and on the console.

This is worth stating plainly because a reminder is a *promise*: the booking
confirmation says "I'll send you a reminder beforehand". Today, for most
bookings, that promise cannot be kept — see the note in
``docs/INTEGRATION_NOTES.md``. Pretending otherwise in code would hide it.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from enum import StrEnum

from mawjood.core.notifications.templates import Template, TemplateStatus


class NotificationKind(StrEnum):
    """What a scheduled message is for."""

    # Before the appointment.
    REMINDER = "reminder"
    # Shortly after it, to catch anything that went wrong while it still matters.
    FOLLOW_UP = "follow_up"
    # The 1-5 rating, later, once the consumer has had the experience.
    SATISFACTION = "satisfaction"


class Verdict(StrEnum):
    """What the scheduler should do about one notification, right now."""

    # Inside the session window: ordinary phrasebank copy, sent as a normal message.
    SEND_FREEFORM = "send_freeform"
    # Outside the window, but an approved template covers it.
    SEND_TEMPLATE = "send_template"
    # Outside the window with no approved template. Not sent. Not pretended.
    BLOCKED_NO_APPROVED_TEMPLATE = "blocked_no_approved_template"
    # Due time has not arrived.
    NOT_YET = "not_yet"
    # So late that sending it now would be worse than not sending it. A reminder
    # after the appointment is noise; a satisfaction request a week late is rude.
    EXPIRED = "expired"
    # Already went out. Idempotency, so a scheduler run twice does not send twice.
    ALREADY_SENT = "already_sent"
    # The booking carries no slot times, so nothing can be scheduled off it.
    NOT_SCHEDULABLE = "not_schedulable"
    # The booking was cancelled, or the consumer withdrew consent.
    CANCELLED = "cancelled"

    @property
    def sends(self) -> bool:
        return self in (Verdict.SEND_FREEFORM, Verdict.SEND_TEMPLATE)


@dataclass(frozen=True, slots=True)
class Offsets:
    """How far from the appointment each notification sits.

    Hours rather than a cron expression: these are relative to a booking, and
    every one of them is a product decision an operator may want to move without
    a deploy. Defaults come from Settings.
    """

    reminder_hours_before: float = 3.0
    follow_up_hours_after: float = 2.0
    satisfaction_hours_after: float = 24.0
    # How long a notification stays sendable after its moment passes. A
    # scheduler that was down for an hour should still send; one that was down
    # for a day should not.
    grace_hours: float = 2.0
    session_window_hours: float = 24.0

    def __post_init__(self) -> None:
        if self.grace_hours < 0:
            raise ValueError("grace_hours cannot be negative")
        if self.session_window_hours <= 0:
            raise ValueError("session_window_hours must be positive")

    @classmethod
    def from_settings(cls, settings: object) -> Offsets:
        """Read the offsets from config.

        Duck-typed on purpose, matching the rest of core: nothing here imports
        Settings, so the pure layer stays testable with a plain object.
        """

        def value(name: str, fallback: float) -> float:
            return float(getattr(settings, name, fallback))

        return cls(
            reminder_hours_before=value("notify_reminder_hours_before", 3.0),
            follow_up_hours_after=value("notify_follow_up_hours_after", 2.0),
            satisfaction_hours_after=value("notify_satisfaction_hours_after", 24.0),
            grace_hours=value("notify_grace_hours", 2.0),
            session_window_hours=value("whatsapp_session_window_hours", 24.0),
        )


@dataclass(frozen=True, slots=True)
class Plan:
    """One notification, decided."""

    kind: NotificationKind
    verdict: Verdict
    due_at: datetime | None
    reason: str
    template: Template | None = None

    @property
    def sends(self) -> bool:
        return self.verdict.sends


def due_at(
    kind: NotificationKind,
    *,
    slot_start: datetime | None,
    slot_end: datetime | None,
    offsets: Offsets,
) -> datetime | None:
    """When this notification wants to go out. ``None`` if it cannot be placed.

    Follow-up and satisfaction hang off the end of the appointment where one is
    known, and off the start where it is not — a booking with no end time is
    common enough (many upstreams return only a start) that treating it as
    unschedulable would silently disable two thirds of this feature.
    """
    if slot_start is None:
        return None
    finish = slot_end or slot_start

    match kind:
        case NotificationKind.REMINDER:
            return slot_start - timedelta(hours=offsets.reminder_hours_before)
        case NotificationKind.FOLLOW_UP:
            return finish + timedelta(hours=offsets.follow_up_hours_after)
        case NotificationKind.SATISFACTION:
            return finish + timedelta(hours=offsets.satisfaction_hours_after)


def window_is_open(*, last_inbound_at: datetime | None, now: datetime, offsets: Offsets) -> bool:
    """Whether free-form messaging is still permitted on this conversation.

    A consumer we have never heard from has no open window — silence is not
    consent to message, and it is not a session either.
    """
    if last_inbound_at is None:
        return False
    return now - _aware(last_inbound_at) < timedelta(hours=offsets.session_window_hours)


def decide(
    kind: NotificationKind,
    *,
    now: datetime,
    slot_start: datetime | None,
    slot_end: datetime | None,
    last_inbound_at: datetime | None,
    offsets: Offsets,
    template: Template | None,
    already_sent: bool = False,
    cancelled: bool = False,
) -> Plan:
    """Decide what to do about one notification for one booking.

    Order matters. Terminal states are checked before timing, and timing before
    policy, so a cancelled booking never produces a "blocked" reading and a
    not-yet-due notification never consults the template registry.
    """
    when = due_at(kind, slot_start=slot_start, slot_end=slot_end, offsets=offsets)

    if cancelled:
        return Plan(kind, Verdict.CANCELLED, when, "the booking is no longer standing")
    if already_sent:
        return Plan(kind, Verdict.ALREADY_SENT, when, "already delivered for this booking")
    if when is None:
        return Plan(
            kind,
            Verdict.NOT_SCHEDULABLE,
            None,
            "the booking carries no slot time to schedule against",
        )

    when = _aware(when)
    now = _aware(now)

    if now < when:
        return Plan(kind, Verdict.NOT_YET, when, "not due yet")
    if now - when > timedelta(hours=offsets.grace_hours):
        return Plan(
            kind,
            Verdict.EXPIRED,
            when,
            f"more than {offsets.grace_hours:g}h late; sending now would be worse than not",
        )

    if window_is_open(last_inbound_at=last_inbound_at, now=now, offsets=offsets):
        return Plan(
            kind,
            Verdict.SEND_FREEFORM,
            when,
            "inside the 24-hour session window",
        )

    if template is not None and template.status is TemplateStatus.APPROVED:
        return Plan(
            kind,
            Verdict.SEND_TEMPLATE,
            when,
            f"outside the session window; approved template {template.name!r}",
            template=template,
        )

    status = template.status if template else TemplateStatus.NOT_SUBMITTED
    return Plan(
        kind,
        Verdict.BLOCKED_NO_APPROVED_TEMPLATE,
        when,
        (
            "outside the 24-hour session window and no approved template covers "
            f"this ({status}). Not sent, and not recorded as sent."
        ),
        template=template,
    )


def _aware(value: datetime) -> datetime:
    """Treat a naive datetime as UTC.

    PostgreSQL hands back aware datetimes, but a test writing ``datetime(2026,
    1, 1)`` should not have to remember a tzinfo to get a sane answer. Comparing
    a naive and an aware datetime raises, so this is the difference between a
    helpful default and a TypeError in the scheduler.
    """
    return value if value.tzinfo is not None else value.replace(tzinfo=UTC)


__all__ = [
    "NotificationKind",
    "Offsets",
    "Plan",
    "Verdict",
    "decide",
    "due_at",
    "window_is_open",
]
