"""Partner-gated platforms: documented stubs.

OpenTable, Foodics, Booksy, Talabat and Careem all need an affiliate approval, a
paid API licence, or a signed partnership before their APIs are usable. None of
that exists yet.

They ship as stubs on purpose (CLAUDE.md section 10). Each registers cleanly,
declares no capabilities, and returns ``UNSUPPORTED`` for every method — so the
router advances past it and, if nothing else can serve, hands to a human. None of
them ever synthesises a success.

That is the whole point of the plugin architecture: the day a partnership closes,
one of these files gains real methods and **nothing else in the system changes**.
``tests/adapters/test_plugin_claim.py`` proves that by adding a platform without
touching core.

> **No endpoints appear here.** Their documentation was not reachable from this
> environment either — see docs/INTEGRATION_NOTES.md. A stub that carries guessed
> paths is worse than one that carries none, because the guesses look like
> knowledge.
"""

from __future__ import annotations

from dataclasses import dataclass
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


@dataclass(frozen=True, slots=True)
class GateReason:
    """Why a platform is not usable yet, and what would change that.

    ``checklist`` is the part that earns its keep. "Partner-gated" is not
    actionable; a numbered list of what to obtain, in order, is. It lives here
    rather than only in a document so it cannot drift from the adapter it
    describes — a test renders INTEGRATION_NOTES.md's table from these values.
    """

    summary: str
    blocker: str
    # What must be true before this adapter can be written, in order. Written so
    # a non-engineer can work the list.
    checklist: tuple[str, ...] = ()
    # Where the contract would come from, once access exists.
    docs: str | None = None


class _PartnerStub:
    """Registers, declares nothing, performs nothing."""

    slug = "partner"
    categories: ClassVar[list[Category]] = [Category.OTHER]
    capabilities = Capabilities(
        can_search=False,
        can_book=False,
        can_reschedule=False,
        can_cancel=False,
        can_pick_staff=False,
        honours_idempotency=False,
    )
    implemented = False
    gate: ClassVar[GateReason] = GateReason(
        summary="partner access required", blocker="no agreement in place"
    )

    @property
    def blocked_reason(self) -> str:
        return f"{self.slug}: {self.gate.summary} ({self.gate.blocker})"

    async def search_availability(
        self, ctx: CallContext, req: AvailabilityRequest
    ) -> Result[list[Slot]]:
        return Result(outcome=Outcome.UNSUPPORTED, raw_error=self.blocked_reason)

    async def create_booking(self, ctx: CallContext, req: BookingRequest) -> Result[BookingRef]:
        return Result(outcome=Outcome.UNSUPPORTED, raw_error=self.blocked_reason)

    async def get_booking(self, ctx: CallContext, ref: BookingRef) -> Result[BookingRecord]:
        return Result(outcome=Outcome.UNSUPPORTED, raw_error=self.blocked_reason)

    async def find_by_idempotency_key(
        self, ctx: CallContext, idempotency_key: str
    ) -> Result[BookingRecord]:
        return Result(outcome=Outcome.UNSUPPORTED, raw_error=self.blocked_reason)

    async def reschedule(self, ctx: CallContext, ref: BookingRef, slot: Slot) -> Result[BookingRef]:
        return Result(outcome=Outcome.UNSUPPORTED, raw_error=self.blocked_reason)

    async def cancel(self, ctx: CallContext, ref: BookingRef, reason: str | None) -> Result[None]:
        return Result(outcome=Outcome.UNSUPPORTED, raw_error=self.blocked_reason)

    async def health(self, ctx: CallContext) -> Result[None]:
        return Result(outcome=Outcome.UNSUPPORTED, raw_error=self.blocked_reason)


class OpenTableAdapter(_PartnerStub):
    """Restaurant reservations. Closest fit to Mawjood's flow of the five."""

    slug = "opentable"
    categories: ClassVar[list[Category]] = [Category.RESTAURANT]
    gate = GateReason(
        summary="affiliate approval required",
        blocker="the reservation API is gated behind an affiliate agreement",
        docs="OpenTable partner/affiliate portal (requires an approved account)",
        checklist=(
            "Apply to the OpenTable affiliate or partner programme as the operating entity.",
            "Obtain the restaurant-availability and reservation API contract; record "
            "endpoints, auth, pagination, rate limits and error shapes in "
            "docs/INTEGRATION_NOTES.md.",
            "Confirm whether reservations can be created on a consumer's behalf by a "
            "third party, or only deep-linked. If only deep-linked, this platform can "
            "never complete a booking and should be re-scoped to search-only.",
            "Confirm idempotency on create, and how to determine after a timeout "
            "whether a reservation landed (CLAUDE.md 5.1 depends on this).",
            "Confirm UAE market coverage — the UAE presence is thinner than the US.",
        ),
    )


class FoodicsAdapter(_PartnerStub):
    """GCC restaurant POS. Merchant-side, like Deliveroo — check direction first."""

    slug = "foodics"
    categories: ClassVar[list[Category]] = [Category.RESTAURANT, Category.FOOD_DELIVERY]
    gate = GateReason(
        summary="merchant app registration required",
        blocker="needs an app registered against each merchant's Foodics account",
        docs="Foodics developer portal (requires a developer account)",
        checklist=(
            "Register a developer account and create an application.",
            "Establish the per-merchant authorisation flow — Foodics is merchant-side, "
            "so each venue authorises us separately. Confirm this fits the "
            "merchant_credentials model (it should: that is what decision 1 is for).",
            "Determine whether the API exposes *reservations* or only orders and menu "
            "management. If orders only, the same constraint as Deliveroo applies and "
            "an outside assistant cannot originate one.",
            "Record endpoints, auth, rate limits and error shapes.",
            "Confirm idempotency on create and the post-timeout reconciliation path.",
        ),
    )


class BooksyAdapter(_PartnerStub):
    """Salon and barber bookings. Second-best salon fit after Zenoti."""

    slug = "booksy"
    categories: ClassVar[list[Category]] = [Category.SALON, Category.SPA]
    gate = GateReason(
        summary="partner API licence required",
        blocker="no public booking API; access is by commercial agreement",
        docs="none public — obtained under agreement",
        checklist=(
            "Contact Booksy partnerships as the operating entity and establish whether "
            "a third-party booking API exists at all. This is the open question; if the "
            "answer is no, close this adapter out rather than leaving it hopeful.",
            "If yes, obtain the contract and record it in docs/INTEGRATION_NOTES.md.",
            "Confirm UAE coverage and how venues map to merchant credentials.",
            "Confirm idempotency on create and the post-timeout reconciliation path.",
        ),
    )


class TalabatAdapter(_PartnerStub):
    """UAE food delivery. Expect the Deliveroo constraint to repeat here."""

    slug = "talabat"
    categories: ClassVar[list[Category]] = [Category.FOOD_DELIVERY]
    gate = GateReason(
        summary="partnership required",
        blocker="no public consumer ordering API",
        docs="none public — Delivery Hero partner channels",
        checklist=(
            "Approach Talabat/Delivery Hero partnerships as the operating entity.",
            "Establish first, before anything else, whether a consumer-side ordering "
            "API exists for third parties. Deliveroo's does not; assume the same until "
            "shown otherwise, and do not spend on integration work before this answer.",
            "If ordering is merchant-side only, mark this platform "
            "not_what_this_api_is_for, as core/aggregators/deliveroo does.",
            "Otherwise record the contract and confirm idempotency on create.",
        ),
    )


class CareemAdapter(_PartnerStub):
    """Rides and food, UAE. The ride category has no other candidate."""

    slug = "careem"
    categories: ClassVar[list[Category]] = [Category.RIDE, Category.FOOD_DELIVERY]
    gate = GateReason(
        summary="partnership required",
        blocker="Everything App APIs are partner-gated",
        docs="none public — Careem partner channels",
        checklist=(
            "Approach Careem partnerships as the operating entity.",
            "Scope which vertical is in play. Rides and food are different products "
            "with different contracts; do not assume one agreement covers both.",
            "For rides, confirm whether a booking can be made on a consumer's behalf "
            "or only deep-linked into the Careem app.",
            "Note that ride is the only category with no second candidate configured, "
            "so until this exists every ride request goes to a human. That is correct "
            "behaviour, not a bug, but it is worth knowing before launch.",
            "Record the contract and confirm idempotency on create.",
        ),
    )


PARTNER_ADAPTERS: tuple[type[_PartnerStub], ...] = (
    OpenTableAdapter,
    FoodicsAdapter,
    BooksyAdapter,
    TalabatAdapter,
    CareemAdapter,
)


__all__ = [
    "PARTNER_ADAPTERS",
    "BooksyAdapter",
    "CareemAdapter",
    "FoodicsAdapter",
    "GateReason",
    "OpenTableAdapter",
    "TalabatAdapter",
]
