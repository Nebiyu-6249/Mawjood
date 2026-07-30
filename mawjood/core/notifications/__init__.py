"""Scheduled messages: reminders, the follow-up, and the satisfaction ask.

Three pieces, deliberately separated:

* :mod:`~mawjood.core.notifications.policy` — pure. When is it due, and may it
  be sent? Takes ``now`` as an argument, so tests move time without patching a
  clock.
* :mod:`~mawjood.core.notifications.templates` — which WhatsApp templates exist
  and where they stand with the provider. Today: declared, none submitted.
* :mod:`~mawjood.core.notifications.scheduler` — the part that reads bookings,
  renders phrasebank copy and hands it to the BSP.

The thing to understand before changing any of it: **outside WhatsApp's 24-hour
session window, a message can only go out as a pre-approved template, and
Mawjood has none approved.** So most notifications currently cannot be sent. The
scheduler blocks them, records why, and surfaces them — it never marks an unsent
message as sent. That is what "degrade honestly" means here.
"""

from __future__ import annotations

from mawjood.core.notifications.policy import (
    NotificationKind,
    Offsets,
    Plan,
    Verdict,
    decide,
    due_at,
    window_is_open,
)
from mawjood.core.notifications.scheduler import Scheduled, Scheduler
from mawjood.core.notifications.templates import (
    Template,
    TemplateRegistry,
    TemplateStatus,
    build_template_registry,
)

__all__ = [
    "NotificationKind",
    "Offsets",
    "Plan",
    "Scheduled",
    "Scheduler",
    "Template",
    "TemplateRegistry",
    "TemplateStatus",
    "Verdict",
    "build_template_registry",
    "decide",
    "due_at",
    "window_is_open",
]
