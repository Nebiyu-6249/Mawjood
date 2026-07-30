"""The phrasebank: every word a consumer can ever see.

Two jobs, both structural rather than advisory.

**Language readiness.** Copy is data, keyed by (key, locale), never a string
literal in a code path. v1 ships ``en``. Adding ``ar`` is dropping in a file
(CLAUDE.md section 6).

**Making the invariant enforceable.** ``RenderedMessage`` can only be produced by
this module, and the send interface accepts nothing else. That is what makes the
blocklist scan over the phrasebank *exhaustive* instead of best-effort: there is
no second way for text to reach a consumer, so scanning the phrasebank scans
everything (CLAUDE.md section 2).

The blocklist itself lives in ``tests/cascade/``, not here. Application code
should not be able to edit the list it is judged against.
"""

from __future__ import annotations

import tomllib
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from string import Template
from typing import Any, Final

PHRASES_DIR: Final = Path(__file__).parent / "phrases"
DEFAULT_LOCALE: Final = "en"

# Only this module may build a RenderedMessage. Deliberately module-private.
_CONSTRUCTOR_TOKEN: Final = object()


class PhraseKey(StrEnum):
    """Keys used by v1.

    An enum rather than free strings so a typo is a failure at import time, and
    so a test can assert every key exists in every shipped locale — a missing
    translation must never surface as a blank message.
    """

    GREETING_WELCOME = "greeting.welcome"
    GREETING_RETURNING = "greeting.returning"

    CONSENT_NOTICE = "consent.notice"
    CONSENT_ACKNOWLEDGED = "consent.acknowledged"
    CONSENT_WITHDRAWN = "consent.withdrawn"

    GREETING_RESUMED = "greeting.resumed"

    ASK_WHAT_YOU_NEED = "ask.what_you_need"
    ASK_SERVICE = "ask.service"
    ASK_AREA = "ask.area"
    ASK_WHEN = "ask.when"
    CLARIFY_AREA = "clarify.area"
    ACK_RECEIVED = "ack.received"
    CHANGE_ACKNOWLEDGED = "ack.changed"

    # Presenting a concrete slot and taking an explicit confirmation. Nothing is
    # booked without the consumer answering the second of these.
    OFFER_SLOT = "offer.slot"
    OFFER_SLOT_HEDGED = "offer.slot_hedged"
    CONFIRM_REQUEST = "confirm.request"
    BOOKING_CONFIRMED = "booking.confirmed"

    POST_STATUS = "post.status"
    POST_RESCHEDULE_ASK = "post.reschedule_ask"
    POST_CANCELLED = "post.cancelled"

    FALLBACK_EMOJI = "fallback.emoji"
    LANGUAGE_ENGLISH_ONLY = "language.english_only"

    # The graceful pivot. Also the latency cover when the turn budget expires
    # mid-cascade — one mechanism, both jobs (CLAUDE.md section 5.2).
    PIVOT_HOLDING = "pivot.holding"
    PIVOT_STILL_LOOKING = "pivot.still_looking"

    HANDOFF_CONNECTING = "handoff.connecting"
    FALLBACK_UNCLEAR = "fallback.unclear"

    # Scheduled messages. Outside WhatsApp's 24-hour session window these can
    # only go out as an approved template — see core/notifications/templates.py.
    NOTIFY_REMINDER = "notify.reminder"
    NOTIFY_FOLLOW_UP = "notify.follow_up"
    NOTIFY_SATISFACTION = "notify.satisfaction"
    NOTIFY_SATISFACTION_THANKS = "notify.satisfaction_thanks"


@dataclass(frozen=True, slots=True)
class RenderedMessage:
    """Consumer-facing text, provably from the phrasebank.

    Constructing one outside this module raises. That is the whole point: it
    makes "all consumer copy is phrasebank copy" a property of the type system
    rather than a rule people remember.
    """

    key: str
    locale: str
    text: str
    _token: object

    def __post_init__(self) -> None:
        if self._token is not _CONSTRUCTOR_TOKEN:
            raise TypeError(
                "RenderedMessage cannot be constructed directly. Consumer-facing "
                "copy must come from the phrasebank so the no-empty-shelves "
                "blocklist scan covers it. Use Phrasebank.render()."
            )


class PhraseNotFound(KeyError):
    """A key or locale that the phrasebank does not carry."""


@dataclass(frozen=True, slots=True)
class Locale:
    locale: str
    policy_version: str
    phrases: dict[str, str]


class Phrasebank:
    """Loads locale files and renders keys into RenderedMessage."""

    def __init__(self, locales: dict[str, Locale], default_locale: str = DEFAULT_LOCALE) -> None:
        if default_locale not in locales:
            raise PhraseNotFound(f"default locale {default_locale!r} was not loaded")
        self._locales = locales
        self._default = default_locale

    @classmethod
    def load(
        cls, directory: Path | None = None, default_locale: str = DEFAULT_LOCALE
    ) -> Phrasebank:
        directory = directory or PHRASES_DIR
        locales: dict[str, Locale] = {}
        for path in sorted(directory.glob("*.toml")):
            raw: dict[str, Any] = tomllib.loads(path.read_text(encoding="utf-8"))
            locale = str(raw["locale"])
            locales[locale] = Locale(
                locale=locale,
                policy_version=str(raw["policy_version"]),
                phrases={str(k): str(v) for k, v in raw["phrases"].items()},
            )
        if not locales:
            raise PhraseNotFound(f"no locale files found in {directory}")
        return cls(locales, default_locale=default_locale)

    @property
    def locales(self) -> list[str]:
        return sorted(self._locales)

    def policy_version(self, locale: str | None = None) -> str:
        return self._locale(locale).policy_version

    def _locale(self, locale: str | None) -> Locale:
        wanted = locale or self._default
        found = self._locales.get(wanted)
        if found is None:
            # Falling back beats showing nothing: a missing locale must never
            # become a dead end for the consumer.
            found = self._locales[self._default]
        return found

    def all_phrases(self) -> dict[str, dict[str, str]]:
        """Every phrase in every locale. The blocklist scan iterates this."""
        return {locale: dict(data.phrases) for locale, data in self._locales.items()}

    def raw(self, key: PhraseKey | str, locale: str | None = None) -> str:
        data = self._locale(locale)
        text = data.phrases.get(str(key))
        if text is None:
            raise PhraseNotFound(f"phrase {str(key)!r} missing for locale {data.locale!r}")
        return text

    def render(
        self,
        key: PhraseKey | str,
        locale: str | None = None,
        /,
        **variables: object,
    ) -> RenderedMessage:
        """Render a phrase into the only type the send path accepts.

        Uses ``$name`` substitution and raises on a missing variable. A partially
        substituted message reaching a consumer would be worse than a loud error
        here.
        """
        data = self._locale(locale)
        template = self.raw(key, data.locale)
        try:
            text = Template(template).substitute(
                {name: str(value) for name, value in variables.items()}
            )
        except KeyError as exc:
            raise PhraseNotFound(
                f"phrase {str(key)!r} needs variable {exc.args[0]!r}, which was not supplied"
            ) from exc
        return RenderedMessage(
            key=str(key),
            locale=data.locale,
            text=text,
            _token=_CONSTRUCTOR_TOKEN,
        )


_phrasebank: Phrasebank | None = None


def get_phrasebank() -> Phrasebank:
    """Process-wide phrasebank. Loaded once."""
    global _phrasebank
    if _phrasebank is None:
        _phrasebank = Phrasebank.load()
    return _phrasebank


__all__ = [
    "DEFAULT_LOCALE",
    "Locale",
    "PhraseKey",
    "PhraseNotFound",
    "Phrasebank",
    "RenderedMessage",
    "get_phrasebank",
]
