"""Deliveroo — implemented to the contract, honest about what it cannot do.

This adapter is not blocked on paperwork alone. It is blocked on something more
interesting, and the distinction matters enough to encode in the type system
rather than bury in a comment.

## The constraint an outside assistant actually hits

**Deliveroo's Order API is merchant-side.** It is the interface a restaurant's
point-of-sale system uses to *receive* orders that Deliveroo has already taken
from a consumer through Deliveroo's own apps — to accept them, mark them ready,
and reconcile them. Orders flow *into* it.

There is no public consumer-side ordering API. Mawjood is an outside assistant
acting for a consumer, so the direction it needs — *place* an order on someone's
behalf — is not what the Order API is shaped to do. A partnership would grant
access to the merchant side of a restaurant we do not operate.

So there are two different reasons a method here cannot run, and conflating them
would cost somebody a quarter:

* :attr:`Support.BLOCKED` — plausible, but we lack the contract or the
  credentials. Clears with documentation and a partnership.
* :attr:`Support.NOT_WHAT_THIS_API_IS_FOR` — the capability does not exist in
  this API's direction of travel. **A partnership does not clear it.**

``create_booking`` is the second kind. Recording that here means an operator
weighing a Deliveroo partnership can see, before signing anything, that it will
not unlock consumer food ordering. Marking everything a uniform "blocked" would
have implied the opposite.

## Why there are still no endpoints

``api-docs.deliveroo.com`` and ``developers.deliveroo.com`` both return HTTP 403
to automated fetching, and direct connections are refused (re-verified
2026-07-30). CLAUDE.md §10 is explicit: do not guess a platform's API from
memory. Nothing below names a path, a header or a payload.

What *is* implemented is the full contract — every method, correct signatures,
typed outcomes, never raising into the router — plus a per-method support
declaration that survives the day the docs become readable. Finishing this is
filling in method bodies, not designing an adapter.

## Behaviour today

Every method returns ``UNSUPPORTED``. The router advances to the next candidate;
if none can serve, the conversation goes to a human. Nothing is ever synthesised
as a success, and the consumer is never told a shelf is empty.
"""

from __future__ import annotations

from enum import StrEnum
from typing import ClassVar

from mawjood.core.aggregators.base import (
    AvailabilityRequest,
    BookingRecord,
    BookingRef,
    BookingRequest,
    CallContext,
    Capabilities,
    Result,
    Slot,
)
from mawjood.core.enums import Category, Outcome


class Support(StrEnum):
    """Why a method cannot run, at a resolution an operator can act on."""

    # We lack the contract, the credentials, or both. Clears with documentation
    # and a signed partnership.
    BLOCKED = "blocked"
    # The capability is not in this API's direction of travel. A partnership does
    # NOT clear it — the API is merchant-side and this action is consumer-side.
    NOT_WHAT_THIS_API_IS_FOR = "not_what_this_api_is_for"

    @property
    def clears_with_a_partnership(self) -> bool:
        return self is Support.BLOCKED


# Per method, because the answer genuinely differs per method. This table is the
# deliverable: it is what someone reads before deciding whether a Deliveroo
# partnership is worth pursuing.
SUPPORT: dict[str, tuple[Support, str]] = {
    "search_availability": (
        Support.BLOCKED,
        "Restaurant open/closed and delivery-zone data plausibly exists, but the "
        "contract is unknown and the docs are unreachable.",
    ),
    "create_booking": (
        Support.NOT_WHAT_THIS_API_IS_FOR,
        "The Order API receives orders Deliveroo has already taken from a "
        "consumer through its own apps. It does not place them. An outside "
        "assistant cannot originate an order through it, with or without a "
        "partnership.",
    ),
    "get_booking": (
        Support.NOT_WHAT_THIS_API_IS_FOR,
        "A merchant can read orders it received. Mawjood never originates one, "
        "so there is nothing of ours to read back.",
    ),
    "find_by_idempotency_key": (
        Support.NOT_WHAT_THIS_API_IS_FOR,
        "Reconciliation presupposes a create we performed. See create_booking.",
    ),
    "reschedule": (
        Support.NOT_WHAT_THIS_API_IS_FOR,
        "Food delivery has no reschedule in the sense a booking does.",
    ),
    "cancel": (
        Support.BLOCKED,
        "Merchant-side cancellation exists, but only for orders the merchant "
        "owns. Unusable until the two above resolve.",
    ),
    "health": (
        Support.BLOCKED,
        "Needs a base URL and a credential; both unknown.",
    ),
}


class DeliverooAdapter:
    """Deliveroo, to the contract. Every method typed, nothing invented."""

    slug = "deliveroo"
    categories: ClassVar[list[Category]] = [Category.FOOD_DELIVERY, Category.RESTAURANT]

    # Declared false across the board so the router never attempts a call it
    # would only have to discard. Capabilities are the router's cheap check;
    # SUPPORT above is the human-readable why.
    capabilities = Capabilities(
        can_search=False,
        can_book=False,
        can_reschedule=False,
        can_cancel=False,
        can_pick_staff=False,
        honours_idempotency=False,
    )

    implemented = False

    # Documentation hosts, and what they returned. Recorded so the next person
    # does not repeat the attempt, and so the date stays visible.
    doc_attempts: ClassVar[tuple[tuple[str, str], ...]] = (
        ("https://api-docs.deliveroo.com/docs/introduction", "HTTP 403 (2026-07-30)"),
        ("https://developers.deliveroo.com/docs", "HTTP 403 (2026-07-30)"),
        ("https://api-docs.deliveroo.com/ (direct curl, browser UA)", "connection refused"),
        ("https://api-docs.deliveroo.com/v2.0/reference (direct curl)", "connection refused"),
        ("https://developers.deliveroo.com/ (direct curl)", "connection refused"),
    )

    def _why(self, method: str) -> str:
        support, detail = SUPPORT[method]
        return f"deliveroo.{method}: {support} — {detail}"

    @property
    def blocked_reason(self) -> str:
        return (
            "deliveroo: Order API is merchant-side (receives orders, does not "
            "place them) and its documentation is unreachable"
        )

    @property
    def partnership_would_unlock_booking(self) -> bool:
        """Whether signing a partnership would make consumer booking possible.

        False, and that is the finding. The blocker on ``create_booking`` is
        architectural, not commercial.
        """
        return SUPPORT["create_booking"][0].clears_with_a_partnership

    async def search_availability(
        self, ctx: CallContext, req: AvailabilityRequest
    ) -> Result[list[Slot]]:
        return Result(outcome=Outcome.UNSUPPORTED, raw_error=self._why("search_availability"))

    async def create_booking(self, ctx: CallContext, req: BookingRequest) -> Result[BookingRef]:
        return Result(outcome=Outcome.UNSUPPORTED, raw_error=self._why("create_booking"))

    async def get_booking(self, ctx: CallContext, ref: BookingRef) -> Result[BookingRecord]:
        return Result(outcome=Outcome.UNSUPPORTED, raw_error=self._why("get_booking"))

    async def find_by_idempotency_key(
        self, ctx: CallContext, idempotency_key: str
    ) -> Result[BookingRecord]:
        return Result(outcome=Outcome.UNSUPPORTED, raw_error=self._why("find_by_idempotency_key"))

    async def reschedule(self, ctx: CallContext, ref: BookingRef, slot: Slot) -> Result[BookingRef]:
        return Result(outcome=Outcome.UNSUPPORTED, raw_error=self._why("reschedule"))

    async def cancel(self, ctx: CallContext, ref: BookingRef, reason: str | None) -> Result[None]:
        return Result(outcome=Outcome.UNSUPPORTED, raw_error=self._why("cancel"))

    async def health(self, ctx: CallContext) -> Result[None]:
        return Result(outcome=Outcome.UNSUPPORTED, raw_error=self._why("health"))


__all__ = ["SUPPORT", "DeliverooAdapter", "Support"]
