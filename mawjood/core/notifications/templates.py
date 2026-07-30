"""WhatsApp message templates, and their approval status.

Outside the 24-hour session window, WhatsApp only carries pre-approved message
templates. Approval is Meta's, granted per template per language, and it is
external lead time — PLAN.md lists it as the longest pole in the project.

So this registry exists to answer one question honestly: **is there an approved
template for this notification, in this language, right now?** Today the answer
is always no, because nothing has been submitted. That is recorded as
:attr:`TemplateStatus.NOT_SUBMITTED` rather than by leaving the registry empty,
because "we have not submitted it" and "we have never thought about it" are
different states and only one of them is a to-do list.

The registry is deliberately data, not code. When templates are approved, the
change is a status flip here (or a config table later) — not a code path.

## Why templates are declared before they are approved

Each declaration names the phrasebank key it corresponds to. That pins the
mapping now, while the copy is being written, so approval later is a status
change rather than an exercise in remembering which template said what. It also
lets a test assert every notification kind has a declared template, which is how
we find out that a new notification kind was added with no route out of the
window.

## What a template body must contain

Meta approves a *body with placeholders*, not free text. The phrasebank entry
and the approved template must say the same thing or the consumer gets one
message in the window and a different one outside it. Keeping them side by side
here is what makes that checkable.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum

from mawjood.core.conversation.phrasebank import PhraseKey


class TemplateStatus(StrEnum):
    """Where a template stands with the provider.

    Four states rather than a boolean: an operator chasing approval needs to know
    whether the ball is in their court.
    """

    # Never sent to the provider. This is where everything sits today.
    NOT_SUBMITTED = "not_submitted"
    # Submitted, awaiting review.
    PENDING = "pending"
    # Usable outside the session window.
    APPROVED = "approved"
    # Refused. Needs rewriting and resubmitting.
    REJECTED = "rejected"

    @property
    def usable(self) -> bool:
        return self is TemplateStatus.APPROVED


@dataclass(frozen=True, slots=True)
class Template:
    """One provider-side template, and the phrasebank entry it mirrors."""

    # The name registered with the provider. Meta requires snake_case.
    name: str
    # The phrasebank entry this template must match in meaning.
    phrasebank_key: str
    locale: str
    status: TemplateStatus = TemplateStatus.NOT_SUBMITTED
    # Ordered placeholder names, matching the phrasebank entry's variables.
    variables: tuple[str, ...] = ()

    @property
    def usable(self) -> bool:
        return self.status.usable


class TemplateRegistry:
    """Templates by (notification kind, locale).

    Keyed by the kind rather than the phrasebank key so the scheduler asks the
    question it actually has: "I need to send a reminder in English — can I?"
    """

    def __init__(self, templates: dict[tuple[str, str], Template] | None = None) -> None:
        self._templates = dict(templates or {})

    def declare(self, kind: str, template: Template) -> None:
        self._templates[(str(kind), template.locale)] = template

    def get(self, kind: str, locale: str) -> Template | None:
        """The template for this kind and locale, or None if none is declared.

        No fallback to another locale. Sending an English template to an Arabic
        consumer because English was approved first is exactly the kind of
        helpfulness nobody wants.
        """
        return self._templates.get((str(kind), locale))

    def approved(self, kind: str, locale: str) -> Template | None:
        template = self.get(kind, locale)
        return template if template is not None and template.usable else None

    @property
    def declared(self) -> list[Template]:
        return sorted(self._templates.values(), key=lambda t: (t.name, t.locale))

    @property
    def any_approved(self) -> bool:
        return any(t.usable for t in self._templates.values())


# ---------------------------------------------------------------------------
# The v1 declaration
# ---------------------------------------------------------------------------
# Every kind, English, none submitted. Flip a status here the day Meta approves
# one, and the scheduler starts sending outside the window with no other change.
#
# The names follow Meta's convention (lowercase, underscores). They are proposed,
# not registered — nothing has been submitted, so nothing can be confirmed.

_V1_TEMPLATES: tuple[tuple[str, Template], ...] = (
    (
        "reminder",
        Template(
            name="booking_reminder",
            phrasebank_key=PhraseKey.NOTIFY_REMINDER,
            locale="en",
            status=TemplateStatus.NOT_SUBMITTED,
            variables=("venue", "time"),
        ),
    ),
    (
        "follow_up",
        Template(
            name="booking_follow_up",
            phrasebank_key=PhraseKey.NOTIFY_FOLLOW_UP,
            locale="en",
            status=TemplateStatus.NOT_SUBMITTED,
            variables=("venue",),
        ),
    ),
    (
        "satisfaction",
        Template(
            name="booking_satisfaction",
            phrasebank_key=PhraseKey.NOTIFY_SATISFACTION,
            locale="en",
            status=TemplateStatus.NOT_SUBMITTED,
            variables=("venue",),
        ),
    ),
)


def build_template_registry() -> TemplateRegistry:
    """The registry a running Mawjood uses.

    A function rather than a module-level singleton so a test can build its own
    with a template marked approved, and prove the out-of-window path works the
    day approval lands.
    """
    registry = TemplateRegistry()
    for kind, template in _V1_TEMPLATES:
        registry.declare(kind, template)
    return registry


__all__ = [
    "Template",
    "TemplateRegistry",
    "TemplateStatus",
    "build_template_registry",
]
