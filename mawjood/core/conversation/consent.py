"""PDPL consent.

Decision 4: **notice, then opt-in before PII leaves Mawjood.** The first reply
carries the consent wording and Mawjood keeps helping — a first turn that is a
dead end would be its own kind of empty shelf. Recorded affirmative consent is
required before any personal data reaches an aggregator or a booking is created.

What is stored (``consents`` table) is an append-only event stream carrying the
*exact wording shown*, its version, the locale and a timestamp. Current state is
derived, never overwritten, so an audit can answer "what did this person
actually read, and when" years later.

This module is pure: classification in, decision out, no I/O. That makes the
rules cheap to test exhaustively.
"""

from __future__ import annotations

import re
from enum import StrEnum, auto
from typing import Final


class ConsentSignal(StrEnum):
    """What, if anything, an inbound message says about consent."""

    NONE = auto()
    AFFIRMATIVE = auto()
    WITHDRAWAL = auto()


class ConsentState(StrEnum):
    """Where a lead stands right now, derived from the event stream."""

    # Never contacted; the notice has not been displayed.
    UNKNOWN = auto()
    # Notice displayed. Mawjood may keep helping, but no PII may leave.
    NOTICE_SHOWN = auto()
    GRANTED = auto()
    WITHDRAWN = auto()


# Kept deliberately tight. A false positive here records consent a consumer did
# not give, which is worse than asking once more.
_AFFIRMATIVE: Final = frozenset(
    {
        "yes",
        "y",
        "yeah",
        "yep",
        "yup",
        "ok",
        "okay",
        "k",
        "sure",
        "fine",
        "agreed",
        "agree",
        "accept",
        "accepted",
        "confirm",
        "confirmed",
        "go ahead",
        "sounds good",
        "no problem",
        "of course",
        "please do",
        "understood",
        "got it",
        "deal",
        "👍",
        "👌",
        "✅",
    }
)

# STOP is the WhatsApp convention and consumers expect it to work.
_WITHDRAWAL: Final = frozenset(
    {
        "stop",
        "unsubscribe",
        "opt out",
        "optout",
        "opt-out",
        "cancel consent",
        "withdraw",
        "withdraw consent",
        "delete my data",
        "delete my details",
        "erase my data",
        "forget me",
        "remove me",
    }
)

_PUNCTUATION: Final = re.compile(r"[.!,;:\s]+$")


def normalise(text: str) -> str:
    return _PUNCTUATION.sub("", text.strip().lower())


def classify(text: str) -> ConsentSignal:
    """Read a message for a consent signal.

    Whole-message matching only. "yes" is consent; "yes, I need a table at
    Zuma" is a booking request that happens to start with a word, and treating
    it as consent would be recording something the consumer did not say.
    """
    cleaned = normalise(text)
    if not cleaned:
        return ConsentSignal.NONE
    if cleaned in _WITHDRAWAL:
        return ConsentSignal.WITHDRAWAL
    if cleaned in _AFFIRMATIVE:
        return ConsentSignal.AFFIRMATIVE
    return ConsentSignal.NONE


def may_share_pii(state: ConsentState) -> bool:
    """The gate. Nothing personal reaches an aggregator without a recorded grant.

    Phase 2 calls this before any adapter sees a name or a number, and before a
    booking row is created.
    """
    return state is ConsentState.GRANTED


def needs_notice(state: ConsentState) -> bool:
    return state is ConsentState.UNKNOWN


__all__ = [
    "ConsentSignal",
    "ConsentState",
    "classify",
    "may_share_pii",
    "needs_notice",
    "normalise",
]
