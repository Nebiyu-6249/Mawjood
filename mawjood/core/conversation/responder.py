"""Deciding what to say.

This is the seam where Phase 2's NLU engine plugs in. Everything around it — the
pipeline, persistence, consent, the audit trail, both transports — stays
unchanged when ``ScriptedResponder`` is replaced by an LLM-backed one. That is
the point of the Protocol: the conversation engine is swappable, the plumbing is
not rewritten.

A responder returns *phrasebank keys*, never text. It cannot author copy, so it
cannot bypass the blocklist scan, and neither can a language model behind it.
That property is why this signature returns keys rather than strings.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from typing import Protocol, runtime_checkable

from mawjood.core.conversation.consent import ConsentSignal, ConsentState
from mawjood.core.conversation.phrasebank import PhraseKey


@dataclass(frozen=True, slots=True)
class TurnContext:
    """Everything a responder is allowed to see about the current turn."""

    text: str
    is_new_lead: bool
    is_new_conversation: bool
    consent_state: ConsentState
    consent_signal: ConsentSignal


@runtime_checkable
class Responder(Protocol):
    """Chooses the phrases for one turn."""

    async def respond(self, context: TurnContext) -> Sequence[PhraseKey]: ...


class ScriptedResponder:
    """Phase 1's deterministic responder.

    Enough to hold a real conversation through consent and acknowledgement, with
    no model in the loop — which is what lets ``chat_sim`` run with no
    credentials of any kind. Phase 2 replaces this class, not its callers.
    """

    async def respond(self, context: TurnContext) -> Sequence[PhraseKey]:
        # Withdrawal wins over everything. A consumer asking to stop is answered
        # about stopping, whatever else the message contained.
        if context.consent_signal is ConsentSignal.WITHDRAWAL:
            return (PhraseKey.CONSENT_WITHDRAWN,)

        # First contact: greet, then show the notice. Mawjood keeps helping while
        # it is displayed — the notice is not a gate (decision 4).
        if context.is_new_lead or context.consent_state is ConsentState.UNKNOWN:
            return (PhraseKey.GREETING_WELCOME, PhraseKey.CONSENT_NOTICE)

        # Someone who previously withdrew is coming back. Re-show the notice
        # rather than silently resuming on a withdrawn consent.
        if context.consent_state is ConsentState.WITHDRAWN:
            return (PhraseKey.GREETING_RETURNING, PhraseKey.CONSENT_NOTICE)

        # They have agreed to the notice.
        if (
            context.consent_state is ConsentState.NOTICE_SHOWN
            and context.consent_signal is ConsentSignal.AFFIRMATIVE
        ):
            return (PhraseKey.CONSENT_ACKNOWLEDGED,)

        if not context.text.strip():
            return (PhraseKey.FALLBACK_UNCLEAR,)

        if context.is_new_conversation:
            return (PhraseKey.GREETING_RETURNING, PhraseKey.ACK_RECEIVED)

        # Phase 2: intent classification, slot filling, clarification and explicit
        # confirmation replace this line.
        return (PhraseKey.ACK_RECEIVED,)


__all__ = ["Responder", "ScriptedResponder", "TurnContext"]
