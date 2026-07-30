"""WhatsApp message templates: names from config, approval status from config.

Outside the 24-hour session window WhatsApp only carries pre-approved message
templates. Approval is Meta's, granted per template per language, and it is an
**operational dependency** — it needs the operator's Business Manager account and
a review that takes as long as it takes.

Two consequences shape this module.

## Template names are configuration, not code

A template's registered name is chosen by whoever submits it, in a portal, on a
day nobody will remember. Hard-coding ``"booking_reminder"`` here means that if
the operator submits ``mawjood_reminder_en_v2`` — because the first submission
was rejected and the name was taken — the fix is a code change and a deploy.

So names come from ``MAWJOOD_TEMPLATE_*`` environment variables, and approval is
declared by ``MAWJOOD_APPROVED_TEMPLATES``. Nothing here is a constant.

## Not-approved must degrade, not fail

An unapproved template does not stop a message. It stops a message *outside the
window*. Inside the window the same copy goes out as an ordinary session
message, from the phrasebank, and the consumer sees no difference.

So the send policy is: **template first if one is approved, session message if
the window is open, and only then blocked.** Templates are preferred because
they do not depend on the consumer having messaged recently — but their absence
costs nothing while the window is open.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum

from mawjood.core.enums import NotificationKind


class TemplateStatus(StrEnum):
    """Where a template stands with the provider.

    Four states rather than a boolean, because an operator chasing approval needs
    to know whose court the ball is in.
    """

    NOT_SUBMITTED = "not_submitted"
    PENDING = "pending"
    APPROVED = "approved"
    REJECTED = "rejected"

    @property
    def usable(self) -> bool:
        return self is TemplateStatus.APPROVED


@dataclass(frozen=True, slots=True)
class Template:
    """One provider-side template, and the phrasebank entry it mirrors."""

    # The name registered with the provider. Comes from config — see the module
    # docstring for why this must never be a literal.
    name: str
    # The phrasebank entry this template must match in meaning. A consumer inside
    # the window and one outside it should receive the same message.
    phrasebank_key: str
    locale: str
    status: TemplateStatus = TemplateStatus.NOT_SUBMITTED
    variables: tuple[str, ...] = ()

    @property
    def usable(self) -> bool:
        return self.status.usable


# Which phrasebank entry each kind renders, and which placeholders it needs.
# This part *is* code: it is our copy, not the provider's registry.
PHRASE_FOR: dict[NotificationKind, str] = {
    NotificationKind.REMINDER_24H: "notify.reminder_24h",
    NotificationKind.REMINDER_2H: "notify.reminder_2h",
    NotificationKind.FOLLOW_UP: "notify.follow_up",
    NotificationKind.SATISFACTION: "notify.satisfaction",
}

VARIABLES_FOR: dict[NotificationKind, tuple[str, ...]] = {
    NotificationKind.REMINDER_24H: ("venue", "day", "time"),
    NotificationKind.REMINDER_2H: ("venue", "time"),
    NotificationKind.FOLLOW_UP: ("venue",),
    NotificationKind.SATISFACTION: ("venue",),
}

# The default name we would submit, if config says nothing. Meta's convention is
# lowercase with underscores. These are proposals, not registrations.
DEFAULT_NAMES: dict[NotificationKind, str] = {
    NotificationKind.REMINDER_24H: "booking_reminder_24h",
    NotificationKind.REMINDER_2H: "booking_reminder_2h",
    NotificationKind.FOLLOW_UP: "booking_follow_up",
    NotificationKind.SATISFACTION: "booking_satisfaction",
}


class TemplateRegistry:
    """Templates by (kind, locale).

    Keyed by kind rather than by phrasebank key so the scheduler asks the
    question it actually has: "I need to send a 2-hour reminder in English — is
    there an approved template for that?"
    """

    def __init__(self, templates: dict[tuple[str, str], Template] | None = None) -> None:
        self._templates = dict(templates or {})

    def declare(self, kind: NotificationKind | str, template: Template) -> None:
        self._templates[(str(kind), template.locale)] = template

    def get(self, kind: NotificationKind | str, locale: str) -> Template | None:
        """The template for this kind and locale, or None.

        No fallback to another locale. Sending an English template to an Arabic
        consumer because English was approved first is exactly the kind of
        helpfulness nobody wants.
        """
        return self._templates.get((str(kind), locale))

    def approved(self, kind: NotificationKind | str, locale: str) -> Template | None:
        template = self.get(kind, locale)
        return template if template is not None and template.usable else None

    @property
    def declared(self) -> list[Template]:
        return sorted(self._templates.values(), key=lambda t: (t.name, t.locale))

    @property
    def any_approved(self) -> bool:
        return any(t.usable for t in self._templates.values())


def build_template_registry(settings: object = None, locale: str = "en") -> TemplateRegistry:
    """Build the registry from configuration.

    ``settings`` is duck-typed, matching the rest of ``core``: nothing here
    imports Settings, so this stays testable with a plain object.

    Two config surfaces:

    * ``template_name_<kind>`` — the name registered with the provider. Falls
      back to our proposed name when unset.
    * ``approved_templates`` — the names Meta has approved, as a set. A name in
      this set flips its template to APPROVED and opens the out-of-window path
      with no code change.
    """
    approved_names = {
        str(name).strip()
        for name in (getattr(settings, "approved_templates", None) or ())
        if str(name).strip()
    }

    registry = TemplateRegistry()
    for kind in NotificationKind:
        configured = getattr(settings, f"template_name_{kind.value}", None)
        name = str(configured).strip() if configured else DEFAULT_NAMES[kind]
        registry.declare(
            kind,
            Template(
                name=name,
                phrasebank_key=PHRASE_FOR[kind],
                locale=locale,
                status=(
                    TemplateStatus.APPROVED
                    if name in approved_names
                    else TemplateStatus.NOT_SUBMITTED
                ),
                variables=VARIABLES_FOR[kind],
            ),
        )
    return registry


__all__ = [
    "DEFAULT_NAMES",
    "PHRASE_FOR",
    "VARIABLES_FOR",
    "Template",
    "TemplateRegistry",
    "TemplateStatus",
    "build_template_registry",
]
