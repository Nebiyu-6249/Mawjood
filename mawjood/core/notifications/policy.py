"""When a notification is due, and how it may be sent.

Pure functions over explicit inputs. No clock, no database, no network — the
caller passes ``now``, so a test moves time without patching anything.

Two independent questions, deliberately kept apart:

1. **When is it due?** Arithmetic on the booking's slot times and the configured
   offsets. Answered once, at scheduling time, and stored.
2. **How may it be sent, right now?** A WhatsApp policy question, answered by the
   template registry and the 24-hour session window. Answered at *send* time,
   because the answer changes — a template gets approved, a consumer replies and
   reopens the window.

Collapsing them produces a scheduler that drops messages and calls them "not
due". Keeping them apart is what makes a deferred message visible with a reason.

## The send ladder

Preference order, and the reasoning for it:

1. **Approved template.** Works regardless of the window, which makes it the
   only reliable option for a 24-hour reminder — by definition the consumer
   last spoke when they booked, which may be days ago.
2. **Session message**, if the window is open. Same phrasebank copy, no template
   needed. This is the degradation path: an unapproved template costs nothing
   while the consumer is still in session.
3. **Deferred.** Not sendable now. Not an error, not dropped, and never recorded
   as sent — it stays a row with a reason, retried on each pass, because what
   unblocks it is an approval landing rather than anything we can retry into.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from enum import StrEnum

from mawjood.core.enums import NotificationKind
from mawjood.core.notifications.templates import Template


class Verdict(StrEnum):
    """How to send one notification, right now."""

    # An approved template. Works outside the session window.
    SEND_TEMPLATE = "send_template"
    # Phrasebank copy as an ordinary session message, inside the window.
    SEND_SESSION = "send_session"
    # Due, permitted, not sendable. Stays pending with a reason.
    DEFERRED_NO_TEMPLATE = "deferred_no_template"
    # Not due yet.
    NOT_YET = "not_yet"
    # So late that sending now would be worse than not. Terminal.
    EXPIRED = "expired"
    # The booking is no longer standing. Terminal.
    CANCELLED = "cancelled"

    @property
    def sends(self) -> bool:
        return self in (Verdict.SEND_TEMPLATE, Verdict.SEND_SESSION)

    @property
    def is_terminal(self) -> bool:
        return self in (Verdict.EXPIRED, Verdict.CANCELLED)


@dataclass(frozen=True, slots=True)
class Offsets:
    """How far from the appointment each notification sits, in hours.

    Two reminders because they answer different questions. At 24 hours a consumer
    can still move their day around it; at 2 hours they need to leave. Both
    configurable, because the right numbers are a product decision an operator
    should be able to change without a deploy.
    """

    reminder_24h_before: float = 24.0
    reminder_2h_before: float = 2.0
    follow_up_after: float = 2.0
    satisfaction_after: float = 24.0
    # How long a notification stays sendable after its moment passes. A scheduler
    # down for an hour should still send; one down for a day must not.
    grace_hours: float = 2.0
    session_window_hours: float = 24.0

    def __post_init__(self) -> None:
        if self.grace_hours < 0:
            raise ValueError("grace_hours cannot be negative")
        if self.session_window_hours <= 0:
            raise ValueError("session_window_hours must be positive")

    @classmethod
    def from_settings(cls, settings: object) -> Offsets:
        """Read the offsets from config. Duck-typed, like the rest of core."""

        def value(name: str, fallback: float) -> float:
            return float(getattr(settings, name, fallback))

        return cls(
            reminder_24h_before=value("notify_reminder_24h_before", 24.0),
            reminder_2h_before=value("notify_reminder_2h_before", 2.0),
            follow_up_after=value("notify_follow_up_after", 2.0),
            satisfaction_after=value("notify_satisfaction_after", 24.0),
            grace_hours=value("notify_grace_hours", 2.0),
            session_window_hours=value("whatsapp_session_window_hours", 24.0),
        )

    def offset_for(self, kind: NotificationKind) -> float:
        match kind:
            case NotificationKind.REMINDER_24H:
                return self.reminder_24h_before
            case NotificationKind.REMINDER_2H:
                return self.reminder_2h_before
            case NotificationKind.FOLLOW_UP:
                return self.follow_up_after
            case NotificationKind.SATISFACTION:
                return self.satisfaction_after


@dataclass(frozen=True, slots=True)
class Plan:
    """One notification, decided."""

    kind: NotificationKind
    verdict: Verdict
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

    Reminders hang off the start — that is the moment the consumer has to be
    somewhere. Follow-up and satisfaction hang off the end where one is known and
    the start where it is not: many upstreams return only a start, and treating
    that as unschedulable would silently disable half this feature.
    """
    if slot_start is None:
        return None
    finish = slot_end or slot_start
    hours = offsets.offset_for(kind)

    if kind.is_reminder:
        return slot_start - timedelta(hours=hours)
    return finish + timedelta(hours=hours)


def window_is_open(*, last_inbound_at: datetime | None, now: datetime, offsets: Offsets) -> bool:
    """Whether free-form messaging is still permitted on this conversation.

    A consumer we have never heard from has no open window — silence is not a
    session, and it is not consent to message.
    """
    if last_inbound_at is None:
        return False
    return _aware(now) - _aware(last_inbound_at) < timedelta(hours=offsets.session_window_hours)


def decide(
    kind: NotificationKind,
    *,
    now: datetime,
    due: datetime,
    last_inbound_at: datetime | None,
    offsets: Offsets,
    template: Template | None,
    cancelled: bool = False,
) -> Plan:
    """Decide how to send one already-scheduled notification.

    Order matters. Terminal states before timing, timing before policy — so a
    cancelled booking never reads as "deferred", and a not-yet-due message never
    consults the template registry.
    """
    if cancelled:
        return Plan(kind, Verdict.CANCELLED, "the booking is no longer standing")

    now = _aware(now)
    due = _aware(due)

    if now < due:
        return Plan(kind, Verdict.NOT_YET, "not due yet")
    if now - due > timedelta(hours=offsets.grace_hours):
        return Plan(
            kind,
            Verdict.EXPIRED,
            f"more than {offsets.grace_hours:g}h late; sending now would be worse than not",
        )

    # 1. An approved template works regardless of the window. Preferred for
    #    exactly that reason — a 24-hour reminder usually falls outside it.
    if template is not None and template.usable:
        return Plan(
            kind,
            Verdict.SEND_TEMPLATE,
            f"approved template {template.name!r}",
            template=template,
        )

    # 2. No template, but the consumer is still in session: same copy, sent as an
    #    ordinary message. This is the degradation the operator asked for.
    if window_is_open(last_inbound_at=last_inbound_at, now=now, offsets=offsets):
        return Plan(
            kind,
            Verdict.SEND_SESSION,
            "no approved template; inside the session window, sending as a session message",
        )

    # 3. Neither. Held, with a reason, and never recorded as sent.
    status = template.status if template else "no template declared"
    name = template.name if template else "—"
    return Plan(
        kind,
        Verdict.DEFERRED_NO_TEMPLATE,
        (
            f"outside the {offsets.session_window_hours:g}h session window and template "
            f"{name!r} is {status}. Held, not sent, not recorded as sent."
        ),
        template=template,
    )


def _aware(value: datetime) -> datetime:
    """Treat a naive datetime as UTC.

    PostgreSQL returns aware datetimes, but a test writing ``datetime(2026, 1,
    1)`` should get a sane answer rather than a TypeError from comparing a naive
    and an aware value.
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
