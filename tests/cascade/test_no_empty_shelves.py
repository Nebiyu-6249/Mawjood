"""The no-empty-shelves suite.

**A consumer is never told there are no vendors, no availability, no options, or
that something failed.** Not once, not in an edge case, not when every upstream
is down. The product is named after this property and it is treated as a safety
property, not a feature.

This file is the enforcement. It scans every phrase in every locale against a
blocklist of failure language and fails the build on a hit.

The blocklist lives here, in the test suite, and not in application code — code
must not be able to edit the list it is judged against.

## Why scanning the phrasebank is sufficient

Because it is not possible to send anything else. ``RenderedMessage`` can only be
built by the phrasebank, the send interface accepts nothing but a
``RenderedMessage``, responders return phrasebank *keys* rather than text, and the
database CHECKs that every outbound message names its phrasebank key. There is no
second route to a consumer's screen, so scanning the phrasebank scans everything.

Those structural claims are asserted below too — a scan is only exhaustive while
the thing that makes it exhaustive still holds.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from mawjood.core.conversation.phrasebank import (
    DEFAULT_LOCALE,
    Phrasebank,
    PhraseKey,
    RenderedMessage,
    get_phrasebank,
)

# Failure language. If a consumer can read it, the build fails.
#
# Each entry is matched case-insensitively against phrase text. Word boundaries
# keep "downtown" from tripping the "down" rule while still catching "the system
# is down".
BLOCKLIST: tuple[str, ...] = (
    r"\bno availability\b",
    r"\bnot available\b",
    r"\bunavailable\b",
    r"\bnothing available\b",
    r"\bno options\b",
    r"\bno vendors\b",
    r"\bno results\b",
    r"\bnone found\b",
    r"\bnothing found\b",
    r"\bno slots\b",
    r"\bfully booked\b",
    r"\bcould ?n[o']?t find\b",
    r"\bcan ?n[o']?t find\b",
    r"\bunable to\b",
    r"\bcan ?n[o']?t help\b",
    r"\bcan ?n[o']?t do\b",
    r"\bwe do ?n[o']?t have\b",
    r"\bsorry\b",
    r"\bapologies\b",
    r"\bapologise\b",
    r"\bapologize\b",
    r"\bunfortunately\b",
    r"\bregret\b",
    r"\bfailed\b",
    r"\bfailure\b",
    r"\berror\b",
    r"\bwent wrong\b",
    r"\bsomething broke\b",
    r"\btry again later\b",
    r"\btry later\b",
    r"\bis down\b",
    r"\bare down\b",
    r"\boffline\b",
    r"\bout of service\b",
    r"\bno luck\b",
    r"\bempty\b",
)

_COMPILED = tuple((pattern, re.compile(pattern, re.IGNORECASE)) for pattern in BLOCKLIST)


def scan(text: str) -> list[str]:
    """Return every blocklist pattern the text violates."""
    return [pattern for pattern, compiled in _COMPILED if compiled.search(text)]


def _all_phrases() -> list[tuple[str, str, str]]:
    """(locale, key, text) for every phrase Mawjood ships."""
    phrasebank = get_phrasebank()
    return [
        (locale, key, text)
        for locale, phrases in phrasebank.all_phrases().items()
        for key, text in sorted(phrases.items())
    ]


class TestTheInvariant:
    @pytest.mark.parametrize(("locale", "key", "text"), _all_phrases())
    def test_no_phrase_contains_failure_language(self, locale: str, key: str, text: str) -> None:
        violations = scan(text)
        assert not violations, (
            f"Phrase {key!r} in locale {locale!r} can reach a consumer and contains "
            f"failure language matching {violations}.\n"
            f"    {text!r}\n"
            "No empty shelves: when an aggregator has nothing, the router advances; "
            "when the list is exhausted, a human takes over. The consumer sees a "
            "pivot, never an apology for an empty platform."
        )

    def test_the_scan_is_not_vacuous(self) -> None:
        """There is something to scan.

        A scan that silently iterates an empty collection passes forever and
        protects nothing.
        """
        phrases = _all_phrases()
        assert len(phrases) >= len(PhraseKey), (
            f"expected at least {len(PhraseKey)} phrases, found {len(phrases)}"
        )


class TestTheTestItself:
    """A scan that cannot fail is not a scan."""

    @pytest.mark.parametrize(
        "bad_copy",
        [
            "Sorry, there's no availability for that.",
            "Unfortunately we couldn't find anything.",
            "That failed — please try again later.",
            "The booking system is down right now.",
            "We don't have any options for you.",
            "An error occurred.",
            "Nothing found, sorry!",
        ],
    )
    def test_planted_failure_language_is_caught(self, bad_copy: str) -> None:
        assert scan(bad_copy), f"blocklist failed to catch: {bad_copy!r}"

    def test_a_planted_phrase_fails_the_real_scan(self, tmp_path: Path) -> None:
        """End to end: poison a locale file, and the scan over it must fail."""
        (tmp_path / "en.toml").write_text(
            'locale = "en"\n'
            'policy_version = "test"\n'
            "[phrases]\n"
            '"greeting.welcome" = "Sorry, no availability today."\n',
            encoding="utf-8",
        )
        poisoned = Phrasebank.load(tmp_path)

        violations = [
            (key, scan(text))
            for phrases in poisoned.all_phrases().values()
            for key, text in phrases.items()
            if scan(text)
        ]
        assert violations, "the scan did not fail on a deliberately poisoned phrasebank"

    def test_good_copy_is_not_flagged(self) -> None:
        """Guard against a blocklist so broad it blocks legitimate copy."""
        for phrase in (
            "Let me check a few more options for you — one moment.",
            "I'm bringing in one of our team to take this the rest of the way.",
            "Got it. Give me a moment and I'll line up your options.",
            "Welcome back 👋 What can I line up for you today?",
        ):
            assert not scan(phrase), f"blocklist is too broad, flagged: {phrase!r}"


class TestCopyIsUnforgeable:
    """The structural claims that make scanning the phrasebank exhaustive."""

    def test_rendered_message_cannot_be_constructed_directly(self) -> None:
        with pytest.raises(TypeError, match="cannot be constructed directly"):
            RenderedMessage(
                key="greeting.welcome",
                locale="en",
                text="Sorry, nothing available.",
                _token=object(),
            )

    def test_the_phrasebank_can_construct_one(self) -> None:
        message = get_phrasebank().render(PhraseKey.GREETING_WELCOME)
        assert isinstance(message, RenderedMessage)
        assert message.text

    def test_responders_return_keys_not_text(self) -> None:
        """A responder cannot author copy, so neither can a model behind one."""
        import inspect

        from mawjood.core.conversation.responder import ScriptedResponder

        signature = inspect.signature(ScriptedResponder.respond)
        assert "PhraseKey" in str(signature.return_annotation)

    def test_every_key_exists_in_every_locale(self) -> None:
        """A missing translation must not surface as a blank message."""
        phrasebank = get_phrasebank()
        for locale, phrases in phrasebank.all_phrases().items():
            missing = [key for key in PhraseKey if str(key) not in phrases]
            assert not missing, f"locale {locale!r} is missing phrases: {missing}"

    def test_the_pivot_and_handoff_phrases_exist(self) -> None:
        """The invariant's escape hatches must always be renderable."""
        phrasebank = get_phrasebank()
        for key in (
            PhraseKey.PIVOT_HOLDING,
            PhraseKey.PIVOT_STILL_LOOKING,
            PhraseKey.HANDOFF_CONNECTING,
        ):
            assert phrasebank.render(key, DEFAULT_LOCALE).text.strip()
