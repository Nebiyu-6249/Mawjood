"""What an outcome *means*.

Adapters report; the router decides. This module is that decision, as a pure
function over the table in CLAUDE.md section 4 — no I/O, no state, so every cell
is testable in isolation.

The single most important row: **a timeout on create does not advance.** If a
create timed out we do not know whether the booking landed, and falling through
to the next platform books the consumer twice. It reconciles instead.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum, auto

from mawjood.core.enums import Outcome


class RouterAction(StrEnum):
    """What the cascade does next."""

    # Take this result and stop looking.
    USE = auto()
    # Try the next candidate.
    ADVANCE = auto()
    # Keep it as a last resort, but keep looking for something better.
    HOLD_AS_FALLBACK = auto()
    # We do not know whether a write landed. Find out before doing anything else.
    RECONCILE = auto()
    # Nothing left to try; a human takes over.
    HANDOFF = auto()


@dataclass(frozen=True, slots=True)
class Decision:
    action: RouterAction
    reason: str
    # Operational faults are ours. None of these ever reach a consumer as an
    # explanation; the phrasebank pivot is all they see.
    consumer_visible: bool = False
    # Mark this merchant's credential degraded and alert ops.
    degrade_credential: bool = False
    # Open a circuit breaker on this merchant with backoff.
    open_circuit: bool = False
    # Safe to retry the same call within the remaining budget. Never true for a
    # create: a retry is exactly the double-booking risk.
    retryable: bool = False


def decide(outcome: Outcome, *, is_create: bool) -> Decision:
    """Map an adapter outcome onto a router action.

    ``is_create`` matters because a write we cannot confirm is a different
    problem from a read we cannot complete.
    """
    match outcome:
        case Outcome.OK:
            return Decision(
                action=RouterAction.USE,
                reason="candidate returned a usable result",
            )

        case Outcome.NO_AVAILABILITY:
            return Decision(
                action=RouterAction.ADVANCE,
                reason="no slots in the requested window",
            )

        case Outcome.LOW_CONFIDENCE:
            # PROPOSED, awaiting sign-off (CLAUDE.md section 4): hold it, keep
            # looking, and if it is ultimately the best we have, surface it with
            # hedged copy and an explicit confirmation. Never auto-book on it.
            return Decision(
                action=RouterAction.HOLD_AS_FALLBACK,
                reason="result returned with low confidence; kept as a fallback",
            )

        case Outcome.TIMEOUT:
            if is_create:
                return Decision(
                    action=RouterAction.RECONCILE,
                    reason="create timed out; booking state unknown, reconciling before anything else",
                )
            return Decision(
                action=RouterAction.ADVANCE,
                reason="read timed out",
                retryable=False,
            )

        case Outcome.AUTH_ERROR:
            return Decision(
                action=RouterAction.ADVANCE,
                reason="merchant credential rejected",
                degrade_credential=True,
            )

        case Outcome.RATE_LIMITED:
            return Decision(
                action=RouterAction.ADVANCE,
                reason="rate limited by the platform",
                open_circuit=True,
            )

        case Outcome.UPSTREAM_ERROR:
            return Decision(
                action=RouterAction.ADVANCE,
                reason="upstream error",
                # One retry is permitted on a read if the budget allows. Never
                # on a create.
                retryable=not is_create,
            )

        case Outcome.UNSUPPORTED:
            return Decision(
                action=RouterAction.ADVANCE,
                reason="candidate cannot perform this action",
            )

    # Unreachable while Outcome is exhaustive; kept so a new member added
    # without a policy row fails loudly rather than silently doing nothing.
    raise NotImplementedError(f"no router policy for outcome {outcome!r}")


__all__ = ["Decision", "RouterAction", "decide"]
