"""Scheduled messages: two reminders, the follow-up, and the satisfaction ask.

Three pieces, deliberately separated:

* :mod:`~mawjood.core.notifications.policy` — pure. When is it due, and how may
  it be sent? ``now`` is an argument, so tests move time without patching a clock.
* :mod:`~mawjood.core.notifications.templates` — WhatsApp templates. Names come
  from config, approval status comes from config; nothing here is a constant.
* :mod:`~mawjood.core.notifications.scheduler` — writes the schedule as rows,
  runs it, and captures the rating that comes back.

Two things to understand before changing any of it.

**Rows, not timers.** Every notification is a database row written the moment a
booking is confirmed. A restart, a redeploy or a three-day outage loses nothing.
``(booking_id, kind)`` is unique, so a consumer cannot be double-reminded even if
two passes race — the constraint refuses, rather than a query somebody has to
remember.

**Templates degrade, they do not fail.** Outside WhatsApp's 24-hour session
window only an approved template may be sent, and none is approved yet. Inside
the window the same phrasebank copy goes out as an ordinary session message and
the consumer sees no difference. Only outside the window, with no approved
template, is a message deferred — held with a reason, retried each pass, never
recorded as sent.
"""

from __future__ import annotations

from mawjood.core.enums import NotificationKind, NotificationStatus
from mawjood.core.notifications.policy import (
    Offsets,
    Plan,
    Verdict,
    decide,
    due_at,
    window_is_open,
)
from mawjood.core.notifications.scheduler import (
    Due,
    Satisfaction,
    Scheduler,
    capture_satisfaction,
    parse_satisfaction,
    schedule_for_booking,
)
from mawjood.core.notifications.templates import (
    PHRASE_FOR,
    Template,
    TemplateRegistry,
    TemplateStatus,
    build_template_registry,
)

__all__ = [
    "PHRASE_FOR",
    "Due",
    "NotificationKind",
    "NotificationStatus",
    "Offsets",
    "Plan",
    "Satisfaction",
    "Scheduler",
    "Template",
    "TemplateRegistry",
    "TemplateStatus",
    "Verdict",
    "build_template_registry",
    "capture_satisfaction",
    "decide",
    "due_at",
    "parse_satisfaction",
    "schedule_for_booking",
    "window_is_open",
]
