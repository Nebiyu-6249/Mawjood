"""Golden transcripts.

Conversations as data: inbound messages plus the state transitions and outcomes
they must produce. Run against the deterministic understudy rather than a live
model, so the suite is fast, free and gives the same answer every time.

They exercise the real state machine and the real NLU path — only the provider is
swapped, at the same seam production would swap it. A transcript passing here
means the conversation logic is right, not that a mock was configured to agree.

The interesting ones are the awkward ones, and they are deliberately over-
represented: a topic change mid-flow, "actually make it Thursday", an area that
exists in two emirates, a day of silence, someone asking for a person, a bare
emoji, and Arabic arriving in an English-only build.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
import yaml

from mawjood.core.conversation.consent import ConsentSignal, ConsentState, classify
from mawjood.core.conversation.nlu import understand
from mawjood.core.conversation.phrasebank import get_phrasebank
from mawjood.core.conversation.states import (
    CollectedSlots,
    ConversationState,
    MachineInput,
    TurnAction,
    advance,
)
from mawjood.services.llm import DeterministicProvider

TRANSCRIPT_DIR = Path(__file__).parent
TRANSCRIPTS = sorted(TRANSCRIPT_DIR.glob("*.yaml"))


def load(path: Path) -> dict[str, Any]:
    data: dict[str, Any] = yaml.safe_load(path.read_text(encoding="utf-8"))
    return data


@pytest.mark.parametrize("path", TRANSCRIPTS, ids=lambda p: p.stem)
async def test_transcript(path: Path) -> None:
    script = load(path)
    provider = DeterministicProvider()
    phrasebank = get_phrasebank()

    state = ConversationState(script.get("initial_state", "greeting"))
    collected = CollectedSlots.from_dict(script.get("initial_slots") or {})
    consent = ConsentState(script.get("initial_consent", "unknown"))
    is_new_lead = bool(script.get("new_lead", True))
    pending_offer: dict[str, Any] | None = None

    for index, turn in enumerate(script["turns"], start=1):
        where = f"{path.name} turn {index} ({turn['user']!r})"

        understanding = await understand(provider, message=turn["user"])
        signal = classify(turn["user"])

        # Mirrors the pipeline: consent granted by this message counts for this
        # message. See pipeline.handle_inbound.
        effective_consent = (
            ConsentState.GRANTED
            if signal is ConsentSignal.AFFIRMATIVE and consent is ConsentState.NOTICE_SHOWN
            else consent
        )

        transition = advance(
            MachineInput(
                state=state,
                collected=collected,
                understanding=understanding,
                consent_state=effective_consent,
                consent_signal=signal,
                is_new_lead=is_new_lead,
                hours_since_last_activity=float(turn.get("hours_since", 0)),
                pending_offer=pending_offer,
                has_booking=bool(turn.get("has_booking", False)),
            )
        )

        keys = [str(spec.key) for spec in transition.phrases]

        if "expect_state" in turn:
            assert str(transition.state) == turn["expect_state"], (
                f"{where}: expected state {turn['expect_state']}, got {transition.state}"
            )
        if "expect_action" in turn:
            assert str(transition.action) == turn["expect_action"], (
                f"{where}: expected action {turn['expect_action']}, got {transition.action}"
            )
        if "expect_phrases" in turn:
            assert keys == turn["expect_phrases"], (
                f"{where}: expected phrases {turn['expect_phrases']}, got {keys}"
            )
        if "expect_phrase_contains" in turn:
            for wanted in turn["expect_phrase_contains"]:
                assert wanted in keys, f"{where}: expected {wanted} among {keys}"
        if "forbid_phrases" in turn:
            for forbidden in turn["forbid_phrases"]:
                assert forbidden not in keys, f"{where}: {forbidden} must not appear"
        if "expect_slots" in turn:
            actual = transition.collected.as_dict()
            for field, value in turn["expect_slots"].items():
                assert actual[field] == value, (
                    f"{where}: slot {field} expected {value!r}, got {actual[field]!r}"
                )

        # Everything the consumer would see must render. A transcript that names
        # a phrase which cannot be rendered is not a passing transcript.
        for spec in transition.phrases:
            variables = dict(spec.variables)
            # Offers carry runtime values the machine does not produce; supply
            # placeholders so rendering is still exercised.
            for name in ("service", "venue", "area", "day", "time", "price", "code", "options"):
                variables.setdefault(name, "x")
            rendered = phrasebank.render(spec.key, "en", **variables)
            assert rendered.text.strip(), f"{where}: {spec.key} rendered empty"

        # Carry state forward, as the pipeline does.
        state = transition.state
        collected = transition.collected
        is_new_lead = False
        if effective_consent is ConsentState.GRANTED:
            consent = ConsentState.GRANTED
        elif "consent.notice" in keys:
            consent = ConsentState.NOTICE_SHOWN
        if signal is ConsentSignal.WITHDRAWAL:
            consent = ConsentState.WITHDRAWN

        if transition.action is TurnAction.SEARCH:
            # Stand in for the cascade returning a slot.
            pending_offer = turn.get("offer_found", {"venue_name": "Marina Beauty Lounge"})
            state = ConversationState.CONFIRMATION
        elif transition.clear_pending_offer:
            pending_offer = None


def test_there_are_transcripts() -> None:
    """A parametrised suite over an empty glob passes silently forever."""
    assert len(TRANSCRIPTS) >= 8, f"expected at least 8 transcripts, found {len(TRANSCRIPTS)}"


def test_every_transcript_declares_what_it_covers() -> None:
    for path in TRANSCRIPTS:
        script = load(path)
        assert script.get("description"), f"{path.name} has no description"
        assert script.get("turns"), f"{path.name} has no turns"
