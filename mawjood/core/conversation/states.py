"""The conversation state machine.

**The LLM does understanding and phrasing. This decides what happens next.**

That split is deliberate and load-bearing. A model asked to drive control flow
will, eventually, confirm a booking nobody agreed to, or agree that nothing is
available. Here the model only ever produces an :class:`Understanding` — a
structured reading of one message — and every decision about what to do with it
is an explicit transition in this file, testable without a model in the loop.

    greeting → intent → slot filling → clarification → slot presentation
             → confirmation → booking → confirmed → post-booking

**Nothing is booked without an explicit confirmation of a concrete slot.** The only
transition into ``BOOKING`` requires ``confirmation == "yes"`` while a specific
offer is pending. There is no path around it.

This module is pure: input in, transition out, no I/O. Which is why the awkward
cases — mid-flow topic change, "actually make it Thursday", a day of silence, an
emoji, Arabic in an English-only build — are cheap to cover exhaustively.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field, replace
from enum import StrEnum, auto
from typing import Any

from mawjood.core.conversation.consent import ConsentSignal, ConsentState, may_share_pii
from mawjood.core.conversation.phrasebank import PhraseKey


class ConversationState(StrEnum):
    GREETING = auto()
    INTENT = auto()
    SLOT_FILLING = auto()
    CLARIFICATION = auto()
    SLOT_PRESENTATION = auto()
    CONFIRMATION = auto()
    BOOKING = auto()
    CONFIRMED = auto()
    POST_BOOKING = auto()
    HANDOFF = auto()


class TurnAction(StrEnum):
    """Side effects the pipeline performs after a transition."""

    NONE = auto()
    SEARCH = auto()
    BOOK = auto()
    HANDOFF = auto()
    LOOKUP_BOOKING = auto()
    CANCEL_BOOKING = auto()


@dataclass(frozen=True, slots=True)
class PhraseSpec:
    """A phrasebank key plus its variables. Never text."""

    key: PhraseKey
    variables: Mapping[str, str] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class Understanding:
    """A structured reading of one consumer message.

    Whatever produced it — a model or the deterministic understudy — it cannot
    carry copy, only facts. Unknown fields are None rather than guessed.
    """

    intent: str = "unclear"
    service: str | None = None
    category: str | None = None
    area: str | None = None
    area_ambiguous: tuple[str, ...] = ()
    date_text: str | None = None
    time_text: str | None = None
    confirmation: str | None = None
    wants_human: bool = False
    is_correction: bool = False
    emoji_only: bool = False
    language: str = "en"

    @classmethod
    def from_dict(cls, raw: Mapping[str, Any]) -> Understanding:
        """Build from a provider response, ignoring anything unrecognised.

        Tolerant on purpose: a model returning an extra or misspelled field should
        degrade to "understood less", never to an exception mid-conversation.
        """
        ambiguous = raw.get("area_ambiguous") or ()
        return cls(
            intent=str(raw.get("intent", "unclear")),
            service=_maybe_str(raw.get("service")),
            category=_maybe_str(raw.get("category")),
            area=_maybe_str(raw.get("area")),
            area_ambiguous=tuple(str(a) for a in ambiguous),
            date_text=_maybe_str(raw.get("date_text")),
            time_text=_maybe_str(raw.get("time_text")),
            confirmation=_maybe_str(raw.get("confirmation")),
            wants_human=bool(raw.get("wants_human", False)),
            is_correction=bool(raw.get("is_correction", False)),
            emoji_only=bool(raw.get("emoji_only", False)),
            language=str(raw.get("language", "en")),
        )

    @property
    def carries_detail(self) -> bool:
        return any((self.service, self.area, self.date_text, self.time_text))


def _maybe_str(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


@dataclass(frozen=True, slots=True)
class CollectedSlots:
    """What Mawjood knows about the request so far."""

    service: str | None = None
    category: str | None = None
    area: str | None = None
    date_text: str | None = None
    time_text: str | None = None

    @property
    def complete(self) -> bool:
        """Enough to search. Time is optional — "Thursday" is a workable window."""
        return bool(self.service and self.area and self.date_text)

    def missing(self) -> list[str]:
        gaps = []
        if not self.service:
            gaps.append("service")
        if not self.area:
            gaps.append("area")
        if not self.date_text:
            gaps.append("date")
        return gaps

    def merged_with(self, understanding: Understanding) -> CollectedSlots:
        """Apply an understanding. Later values win — that is how a correction
        works, and 'actually make it Thursday' must overwrite, not append."""
        return replace(
            self,
            service=understanding.service or self.service,
            category=understanding.category or self.category,
            area=understanding.area or self.area,
            date_text=understanding.date_text or self.date_text,
            time_text=understanding.time_text or self.time_text,
        )

    def as_dict(self) -> dict[str, Any]:
        return {
            "service": self.service,
            "category": self.category,
            "area": self.area,
            "date_text": self.date_text,
            "time_text": self.time_text,
        }

    @classmethod
    def from_dict(cls, raw: Mapping[str, Any] | None) -> CollectedSlots:
        raw = raw or {}
        return cls(
            service=_maybe_str(raw.get("service")),
            category=_maybe_str(raw.get("category")),
            area=_maybe_str(raw.get("area")),
            date_text=_maybe_str(raw.get("date_text")),
            time_text=_maybe_str(raw.get("time_text")),
        )


@dataclass(frozen=True, slots=True)
class MachineInput:
    state: ConversationState
    collected: CollectedSlots
    understanding: Understanding
    consent_state: ConsentState = ConsentState.UNKNOWN
    consent_signal: ConsentSignal = ConsentSignal.NONE
    is_new_lead: bool = False
    hours_since_last_activity: float = 0.0
    pending_offer: Mapping[str, Any] | None = None
    has_booking: bool = False


@dataclass(frozen=True, slots=True)
class Transition:
    state: ConversationState
    phrases: tuple[PhraseSpec, ...] = ()
    action: TurnAction = TurnAction.NONE
    collected: CollectedSlots = field(default_factory=CollectedSlots)
    clear_pending_offer: bool = False
    handoff_reason: str | None = None
    note: str = ""


# A gap longer than this and the consumer has almost certainly moved on with their
# day, so the next message is re-oriented rather than answered as if mid-sentence.
RESUMPTION_GAP_HOURS = 6.0


def advance(inp: MachineInput) -> Transition:
    """Decide the next state and what to say. Pure."""
    understanding = inp.understanding
    collected = inp.collected.merged_with(understanding)

    # --- Overrides that outrank the current state -------------------------

    # Asking for a person is honoured immediately, from anywhere.
    if understanding.wants_human:
        return Transition(
            state=ConversationState.HANDOFF,
            phrases=(PhraseSpec(PhraseKey.HANDOFF_CONNECTING),),
            action=TurnAction.HANDOFF,
            collected=collected,
            handoff_reason="consumer_request",
            note="consumer asked for a human",
        )

    # A withdrawal is handled by the consent layer; the machine goes quiet.
    if inp.consent_signal is ConsentSignal.WITHDRAWAL:
        return Transition(
            state=ConversationState.GREETING,
            phrases=(PhraseSpec(PhraseKey.CONSENT_WITHDRAWN),),
            collected=CollectedSlots(),
            clear_pending_offer=True,
            note="consent withdrawn",
        )

    # v1 is English only. Answer warmly in English rather than pretending.
    if understanding.language == "ar":
        return Transition(
            state=inp.state
            if inp.state != ConversationState.GREETING
            else ConversationState.INTENT,
            phrases=(PhraseSpec(PhraseKey.LANGUAGE_ENGLISH_ONLY),),
            collected=collected,
            note="non-English message in an English-only build",
        )

    # --- First contact ----------------------------------------------------

    if inp.is_new_lead or inp.consent_state is ConsentState.UNKNOWN:
        phrases = [PhraseSpec(PhraseKey.GREETING_WELCOME), PhraseSpec(PhraseKey.CONSENT_NOTICE)]
        # If they led with a real request, acknowledge it rather than making them
        # repeat themselves after the housekeeping.
        if collected.complete:
            # They led with everything. Show the notice and get on with it.
            return Transition(
                state=ConversationState.SLOT_PRESENTATION,
                phrases=tuple(phrases),
                action=TurnAction.SEARCH,
                collected=collected,
                note="greeted, notice shown, searching on their opening request",
            )
        if collected.service:
            phrases.append(_ask_for_next_gap(collected))
            return Transition(
                state=ConversationState.SLOT_FILLING,
                phrases=tuple(phrases),
                collected=collected,
                note="greeted, notice shown, and kept their request",
            )
        return Transition(
            state=ConversationState.INTENT,
            phrases=tuple(phrases),
            collected=collected,
        )

    if inp.consent_state is ConsentState.WITHDRAWN:
        return Transition(
            state=ConversationState.INTENT,
            phrases=(
                PhraseSpec(PhraseKey.GREETING_RETURNING),
                PhraseSpec(PhraseKey.CONSENT_NOTICE),
            ),
            collected=collected,
            note="previously withdrawn; notice re-shown",
        )

    # --- Post-booking -----------------------------------------------------

    if (
        inp.state in (ConversationState.CONFIRMED, ConversationState.POST_BOOKING)
        or inp.has_booking
    ):
        post = _post_booking(inp, collected)
        if post is not None:
            return post

    # --- A long silence ---------------------------------------------------

    if (
        inp.hours_since_last_activity >= RESUMPTION_GAP_HOURS
        and inp.state not in (ConversationState.GREETING, ConversationState.INTENT)
        and not understanding.carries_detail
        and understanding.confirmation is None
    ):
        # They went quiet and came back with nothing new. Re-orient rather than
        # continuing a sentence they have forgotten.
        return Transition(
            state=ConversationState.SLOT_FILLING if collected.service else ConversationState.INTENT,
            phrases=(PhraseSpec(PhraseKey.GREETING_RESUMED),),
            collected=collected,
            note="resumed after a gap",
        )

    # --- Awaiting confirmation of a concrete offer ------------------------

    if inp.state is ConversationState.CONFIRMATION and inp.pending_offer:
        if understanding.confirmation == "yes":
            # Decision 4: a booking sends a name and number to a venue, so it
            # needs recorded consent. Availability search does not — nothing
            # personal leaves in it — which is why the gate is here and not
            # earlier.
            if not may_share_pii(inp.consent_state):
                return Transition(
                    state=ConversationState.CONFIRMATION,
                    phrases=(PhraseSpec(PhraseKey.CONSENT_NOTICE),),
                    collected=collected,
                    note="consent required before a booking can be created",
                )
            # The only route into BOOKING.
            return Transition(
                state=ConversationState.BOOKING,
                action=TurnAction.BOOK,
                collected=collected,
                note="explicit confirmation of a concrete slot",
            )
        if understanding.confirmation == "no" or understanding.is_correction:
            # Declined or amended: drop the offer and look again.
            return Transition(
                state=ConversationState.SLOT_FILLING,
                phrases=(PhraseSpec(PhraseKey.CHANGE_ACKNOWLEDGED),),
                action=TurnAction.SEARCH if collected.complete else TurnAction.NONE,
                collected=collected,
                clear_pending_offer=True,
                note="offer declined or amended",
            )
        if understanding.carries_detail:
            # New details while an offer was pending is an implicit amendment.
            return Transition(
                state=ConversationState.SLOT_FILLING,
                phrases=(PhraseSpec(PhraseKey.CHANGE_ACKNOWLEDGED),),
                action=TurnAction.SEARCH if collected.complete else TurnAction.NONE,
                collected=collected,
                clear_pending_offer=True,
                note="new details supersede the pending offer",
            )
        # Anything else: ask again without losing the offer. Silence is not consent.
        return Transition(
            state=ConversationState.CONFIRMATION,
            phrases=(PhraseSpec(PhraseKey.CONFIRM_REQUEST),),
            collected=collected,
            note="confirmation still outstanding",
        )

    # --- Ambiguous area ---------------------------------------------------

    if understanding.area_ambiguous:
        return Transition(
            state=ConversationState.CLARIFICATION,
            phrases=(
                PhraseSpec(
                    PhraseKey.CLARIFY_AREA,
                    {"options": " or ".join(understanding.area_ambiguous)},
                ),
            ),
            collected=replace(collected, area=None),
            note="area matches more than one emirate",
        )

    # --- Nothing understood ----------------------------------------------

    if understanding.emoji_only:
        return Transition(
            state=inp.state,
            phrases=(PhraseSpec(PhraseKey.FALLBACK_EMOJI),),
            collected=collected,
            note="emoji only",
        )

    if understanding.intent == "unclear" and not understanding.carries_detail:
        if collected.complete:
            # Nothing new in this message, but everything needed is already
            # known. Get on with it rather than acknowledging into silence.
            return Transition(
                state=ConversationState.SLOT_PRESENTATION,
                action=TurnAction.SEARCH,
                collected=collected,
                clear_pending_offer=True,
                note="unclear message, but the request is already complete",
            )
        if collected.service:
            return Transition(
                state=ConversationState.SLOT_FILLING,
                phrases=(_ask_for_next_gap(collected),),
                collected=collected,
                note="unclear; re-asked the outstanding detail",
            )
        return Transition(
            state=ConversationState.INTENT,
            phrases=(PhraseSpec(PhraseKey.FALLBACK_UNCLEAR),),
            collected=collected,
        )

    # --- Slot filling -----------------------------------------------------

    if collected.complete:
        acknowledgement: tuple[PhraseSpec, ...] = ()
        if understanding.is_correction:
            acknowledgement = (PhraseSpec(PhraseKey.CHANGE_ACKNOWLEDGED),)
        return Transition(
            state=ConversationState.SLOT_PRESENTATION,
            phrases=acknowledgement,
            action=TurnAction.SEARCH,
            collected=collected,
            clear_pending_offer=True,
            note="all details present; searching",
        )

    return Transition(
        state=ConversationState.SLOT_FILLING,
        phrases=(_ask_for_next_gap(collected),),
        collected=collected,
        note=f"still missing: {', '.join(collected.missing())}",
    )


def _post_booking(inp: MachineInput, collected: CollectedSlots) -> Transition | None:
    """Status, reschedule and cancel after a booking exists."""
    intent = inp.understanding.intent

    if intent == "booking_status":
        return Transition(
            state=ConversationState.POST_BOOKING,
            action=TurnAction.LOOKUP_BOOKING,
            collected=collected,
            note="status requested",
        )
    if intent == "reschedule_booking":
        # Rescheduling is a fresh search plus a fresh confirmation. It is never
        # applied silently — the consumer confirms the new slot like any other.
        return Transition(
            state=ConversationState.SLOT_FILLING,
            phrases=(PhraseSpec(PhraseKey.POST_RESCHEDULE_ASK),),
            collected=replace(collected, date_text=None, time_text=None).merged_with(
                inp.understanding
            ),
            clear_pending_offer=True,
            note="reschedule requested",
        )
    if intent == "cancel_booking":
        return Transition(
            state=ConversationState.POST_BOOKING,
            action=TurnAction.CANCEL_BOOKING,
            collected=collected,
            note="cancellation requested",
        )
    if inp.understanding.service and inp.understanding.service != collected.service:
        # A brand-new request after a completed booking starts a new flow.
        fresh = CollectedSlots().merged_with(inp.understanding)
        return Transition(
            state=ConversationState.SLOT_FILLING,
            phrases=(_ask_for_next_gap(fresh),),
            collected=fresh,
            clear_pending_offer=True,
            note="new request after a booking",
        )
    return None


def _ask_for_next_gap(collected: CollectedSlots) -> PhraseSpec:
    """Ask for one missing thing at a time.

    A single question reads like a concierge. Three at once reads like a form.
    """
    gaps = collected.missing()
    if "service" in gaps:
        return PhraseSpec(PhraseKey.ASK_SERVICE)
    if "area" in gaps:
        return PhraseSpec(PhraseKey.ASK_AREA)
    if "date" in gaps:
        return PhraseSpec(PhraseKey.ASK_WHEN)
    return PhraseSpec(PhraseKey.ACK_RECEIVED)


__all__ = [
    "RESUMPTION_GAP_HOURS",
    "CollectedSlots",
    "ConversationState",
    "MachineInput",
    "PhraseSpec",
    "Transition",
    "TurnAction",
    "Understanding",
    "advance",
]
