"""Performing what the state machine decided.

``states.advance`` decides; this executes. Keeping them apart means the decision
logic stays pure and exhaustively testable, and the side effects — a cascade run,
a booking row, a handoff — live in one place that can be read top to bottom.

Three behaviours worth reading closely:

* **Search.** Runs the cascade. Found → offer the slot and wait for an explicit
  yes. Exhausted → a human, and a pivot. Budget blown → a holding pivot now and
  the rest of the cascade handed back as a deferred coroutine, which is the same
  mechanism (CLAUDE.md section 5.2).
* **Book.** Only ever reached from an explicit confirmation. Uncertain → a human,
  never a retry elsewhere.
* **Failover after confirmation.** If the confirmed slot cannot be booked cleanly,
  the replacement is measured against it: inside tolerance, booked and stated;
  outside, re-confirmed (decision 2).
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any
from zoneinfo import ZoneInfo

from mawjood.core.aggregators.base import (
    AvailabilityRequest,
    Slot,
)
from mawjood.core.conversation.phrasebank import PhraseKey
from mawjood.core.conversation.states import ConversationState, PhraseSpec, Transition, TurnAction
from mawjood.core.enums import Category
from mawjood.core.routing.cascade import (
    BookStatus,
    Candidate,
    Cascade,
    SearchOutcome,
    SearchStatus,
)
from mawjood.core.routing.materiality import Tolerances, compare
from mawjood.core.routing.when import resolve_window
from mawjood.observability.logging import get_logger

log = get_logger(__name__)


@dataclass(slots=True)
class ActionResult:
    """What performing an action produced."""

    phrases: tuple[PhraseSpec, ...] = ()
    state: ConversationState | None = None
    pending_offer: dict[str, Any] | None = None
    clear_pending_offer: bool = False
    handoff_reason: str | None = None
    booking: dict[str, Any] | None = None
    # Set when the turn budget expired: the caller sends the pivot now and either
    # awaits this or fires it as a task, delivering the result as a follow-up.
    deferred: Callable[[], Awaitable[ActionResult]] | None = None


class TurnEngine:
    """Executes a transition's action."""

    def __init__(
        self,
        *,
        cascade: Cascade,
        candidates_for: Callable[[Category, str | None], Awaitable[list[Candidate]]],
        timezone: str = "Asia/Dubai",
        tolerances: Tolerances | None = None,
    ) -> None:
        self._cascade = cascade
        self._candidates_for = candidates_for
        self._tz = ZoneInfo(timezone)
        self._tolerances = tolerances or Tolerances()

    # -- dispatch -----------------------------------------------------------

    async def perform(self, transition: Transition) -> ActionResult:
        match transition.action:
            case TurnAction.SEARCH:
                return await self._search(transition)
            case TurnAction.BOOK:
                # Booking needs the pending offer, which lives on the conversation
                # row rather than in the transition. The pipeline calls
                # book_pending directly.
                return ActionResult()
            case TurnAction.HANDOFF:
                return ActionResult(
                    state=ConversationState.HANDOFF,
                    handoff_reason=transition.handoff_reason or "consumer_request",
                    clear_pending_offer=True,
                )
            case _:
                return ActionResult()

    # -- search -------------------------------------------------------------

    async def _search(self, transition: Transition) -> ActionResult:
        collected = transition.collected
        category = _category_of(collected.category, collected.service)
        window_start, window_end = resolve_window(
            collected.date_text, collected.time_text, tz=self._tz
        )
        request = AvailabilityRequest(
            category=category,
            service=collected.service or "appointment",
            window_start=window_start,
            window_end=window_end,
            area=collected.area,
        )

        candidates = await self._candidates_for(category, collected.area)
        outcome = await self._cascade.search(candidates, request)
        return self._from_search(outcome, request, category)

    def _from_search(
        self, outcome: SearchOutcome, request: AvailabilityRequest, category: Category
    ) -> ActionResult:
        if outcome.status is SearchStatus.FOUND and outcome.slot is not None:
            key = PhraseKey.OFFER_SLOT_HEDGED if outcome.low_confidence else PhraseKey.OFFER_SLOT
            return ActionResult(
                phrases=(PhraseSpec(key, _offer_variables(outcome.slot, self._tz)),),
                state=ConversationState.CONFIRMATION,
                pending_offer=_offer_payload(outcome.slot, category, outcome.low_confidence),
            )

        if outcome.status is SearchStatus.DEADLINE_EXCEEDED:
            remaining = outcome.remaining_candidates

            async def finish() -> ActionResult:
                # Fresh budget: the consumer already has the holding pivot, so the
                # follow-up is allowed to take longer than one turn.
                resumed = await self._cascade.search(remaining, request)
                return self._from_search(resumed, request, category)

            return ActionResult(
                phrases=(PhraseSpec(PhraseKey.PIVOT_HOLDING),),
                state=ConversationState.SLOT_PRESENTATION,
                deferred=finish,
            )

        # Exhausted. A human takes over and the consumer sees a pivot, never an
        # empty shelf.
        return ActionResult(
            phrases=(PhraseSpec(PhraseKey.HANDOFF_CONNECTING),),
            state=ConversationState.HANDOFF,
            handoff_reason=outcome.handoff_reason or "cascade_exhausted",
            clear_pending_offer=True,
        )

    # -- book ---------------------------------------------------------------

    async def book_pending(
        self,
        *,
        pending_offer: Mapping[str, Any],
        candidates: Sequence[Candidate],
        consumer_name: str | None = None,
        consumer_phone: str | None = None,
    ) -> ActionResult:
        """Book the slot the consumer just confirmed."""
        slot = _slot_from_payload(pending_offer, candidates)
        if slot is None:
            log.error("booking.pending_offer_unresolvable", offer=dict(pending_offer))
            return ActionResult(
                phrases=(PhraseSpec(PhraseKey.HANDOFF_CONNECTING),),
                state=ConversationState.HANDOFF,
                handoff_reason="system_error",
                clear_pending_offer=True,
            )

        category = Category(pending_offer.get("category", Category.OTHER))
        chosen = next((c for c in candidates if c.slug == slot.merchant.platform_slug), None)
        if chosen is None:
            # The offer named a platform that is no longer a candidate — routing
            # config changed, or a merchant was deactivated, between the offer and
            # the consumer's yes. Refusing and handing to a human is correct:
            # booking on a platform ops has since removed is worse than a delay.
            #
            # Logged because the sibling branch above logs, and a silent branch
            # leaves an operator with a system_error on the dashboard and nothing
            # to look at.
            log.error(
                "booking.offer_platform_no_longer_configured",
                platform=slot.merchant.platform_slug,
                candidates=[c.slug for c in candidates],
                category=str(category),
            )
            return ActionResult(
                phrases=(PhraseSpec(PhraseKey.HANDOFF_CONNECTING),),
                state=ConversationState.HANDOFF,
                handoff_reason="system_error",
                clear_pending_offer=True,
            )

        result = await self._cascade.book(
            chosen,
            slot,
            category=category,
            consumer_name=consumer_name,
            consumer_phone=consumer_phone,
        )

        if result.status is BookStatus.BOOKED and result.ref is not None:
            return ActionResult(
                phrases=(
                    PhraseSpec(
                        PhraseKey.BOOKING_CONFIRMED,
                        _confirmation_variables(slot, result.ref.confirmation_code, self._tz),
                    ),
                ),
                state=ConversationState.CONFIRMED,
                clear_pending_offer=True,
                booking={
                    "external_id": result.ref.external_id,
                    "platform_slug": result.ref.platform_slug,
                    "confirmation_code": result.ref.confirmation_code,
                    "venue_name": slot.venue_name,
                    "slot_start": slot.start.isoformat(),
                    "slot_end": slot.end.isoformat(),
                    "category": str(category),
                    "price_amount": slot.price_amount,
                    "price_currency": slot.price_currency,
                },
            )

        if result.status is BookStatus.UNCERTAIN:
            # We do not know whether it landed. A person sorts it out; we do not
            # guess and we do not try elsewhere.
            return ActionResult(
                phrases=(PhraseSpec(PhraseKey.HANDOFF_CONNECTING),),
                state=ConversationState.HANDOFF,
                handoff_reason=result.handoff_reason or "reconciliation_inconclusive",
                clear_pending_offer=True,
            )

        # Clean failure: safe to look elsewhere, then apply the materiality rule.
        return await self._failover_after_confirmation(slot, category, candidates, chosen)

    async def _failover_after_confirmation(
        self,
        confirmed: Slot,
        category: Category,
        candidates: Sequence[Candidate],
        already_tried: Candidate,
    ) -> ActionResult:
        """The confirmed slot fell through. Find another and decide whether the
        consumer needs asking again (decision 2)."""
        remaining = [c for c in candidates if c is not already_tried]
        request = AvailabilityRequest(
            category=category,
            service=confirmed.service_name,
            window_start=confirmed.start,
            window_end=confirmed.end,
            area=confirmed.area,
        )
        outcome = await self._cascade.search(remaining, request)

        if outcome.status is not SearchStatus.FOUND or outcome.slot is None:
            return ActionResult(
                phrases=(PhraseSpec(PhraseKey.HANDOFF_CONNECTING),),
                state=ConversationState.HANDOFF,
                handoff_reason="cascade_exhausted",
                clear_pending_offer=True,
            )

        verdict = compare(confirmed, outcome.slot, self._tolerances)
        if verdict.needs_reconfirmation:
            log.info(
                "booking.reconfirmation_required",
                reasons=list(verdict.reasons),
                from_venue=confirmed.venue_name,
                to_venue=outcome.slot.venue_name,
            )
            return ActionResult(
                phrases=(
                    PhraseSpec(PhraseKey.OFFER_SLOT, _offer_variables(outcome.slot, self._tz)),
                ),
                state=ConversationState.CONFIRMATION,
                pending_offer=_offer_payload(outcome.slot, category, outcome.low_confidence),
            )

        # Inside tolerance: book it, and say plainly what was booked.
        replacement = next(
            (c for c in remaining if c.slug == outcome.slot.merchant.platform_slug), None
        )
        if replacement is None:
            return ActionResult(
                phrases=(PhraseSpec(PhraseKey.HANDOFF_CONNECTING),),
                state=ConversationState.HANDOFF,
                handoff_reason="system_error",
                clear_pending_offer=True,
            )
        return await self.book_pending(
            pending_offer=_offer_payload(outcome.slot, category, outcome.low_confidence),
            candidates=[replacement],
        )


# ---------------------------------------------------------------------------
# Serialising an offer across turns
# ---------------------------------------------------------------------------


def _offer_payload(slot: Slot, category: Category, low_confidence: bool) -> dict[str, Any]:
    """A pending offer must survive a restart, so it is stored as plain JSON."""
    return {
        "slot_id": slot.slot_id,
        "start": slot.start.isoformat(),
        "end": slot.end.isoformat(),
        "venue_name": slot.venue_name,
        "service_name": slot.service_name,
        "staff_name": slot.staff_name,
        "price_amount": slot.price_amount,
        "price_currency": slot.price_currency,
        "area": slot.area,
        "platform_slug": slot.merchant.platform_slug,
        "merchant_id": str(slot.merchant.merchant_id),
        "merchant_name": slot.merchant.display_name,
        "category": str(category),
        "low_confidence": low_confidence,
    }


def _slot_from_payload(payload: Mapping[str, Any], candidates: Sequence[Candidate]) -> Slot | None:
    platform = payload.get("platform_slug")
    owner = next((c for c in candidates if c.slug == platform), None)
    if owner is None:
        return None
    try:
        return Slot(
            slot_id=str(payload["slot_id"]),
            start=datetime.fromisoformat(str(payload["start"])),
            end=datetime.fromisoformat(str(payload["end"])),
            venue_name=str(payload["venue_name"]),
            service_name=str(payload["service_name"]),
            merchant=owner.merchant,
            staff_name=payload.get("staff_name"),
            price_amount=payload.get("price_amount"),
            price_currency=payload.get("price_currency"),
            area=payload.get("area"),
        )
    except (KeyError, ValueError):
        return None


def _offer_variables(slot: Slot, tz: ZoneInfo) -> dict[str, str]:
    local = slot.start.astimezone(tz)
    return {
        # The offer opens with this, so it starts the sentence.
        "service": slot.service_name[:1].upper() + slot.service_name[1:],
        "venue": slot.venue_name,
        "area": slot.area or "",
        "day": _day_phrase(local, tz),
        "time": local.strftime("%-I:%M %p").lower().replace(":00", ""),
        "price": _price_phrase(slot),
    }


def _confirmation_variables(slot: Slot, code: str | None, tz: ZoneInfo) -> dict[str, str]:
    local = slot.start.astimezone(tz)
    return {
        "venue": slot.venue_name,
        "area": slot.area or "",
        "day": _day_phrase(local, tz),
        "time": local.strftime("%-I:%M %p").lower().replace(":00", ""),
        "code": code or "—",
    }


def _day_phrase(local: datetime, tz: ZoneInfo) -> str:
    today = datetime.now(UTC).astimezone(tz).date()
    delta = (local.date() - today).days
    if delta == 0:
        return "today"
    if delta == 1:
        return "tomorrow"
    if 2 <= delta <= 6:
        return local.strftime("%A")
    return local.strftime("%A %-d %B")


def _price_phrase(slot: Slot) -> str:
    if slot.price_amount is None:
        return "price confirmed at the venue"
    currency = slot.price_currency or "AED"
    return f"{currency} {slot.price_amount:.0f}"


def _category_of(category: str | None, service: str | None) -> Category:
    if category:
        try:
            return Category(category)
        except ValueError:
            pass
    return Category.OTHER


__all__ = ["ActionResult", "TurnEngine"]
