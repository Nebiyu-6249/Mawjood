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
    """Why a platform is not usable yet, in terms an operator can act on."""

    summary: str
    blocker: str


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


class DeliverooAdapter(_PartnerStub):
    """Deliveroo — **blocked on documentation and on partnership.**

    Two separate blockers, and both must clear:

    1. ``api-docs.deliveroo.com`` and ``developers.deliveroo.com`` both returned
       HTTP 403 to automated fetching (2026-07-30), so the contract is unknown.
    2. The Order API is merchant-side and full production access needs a
       partnership, which does not exist.
    """

    slug = "deliveroo"
    categories: ClassVar[list[Category]] = [Category.FOOD_DELIVERY, Category.RESTAURANT]
    gate = GateReason(
        summary="documentation unreachable and partnership required",
        blocker="HTTP 403 on api-docs.deliveroo.com; Order API needs a signed partnership",
    )


class OpenTableAdapter(_PartnerStub):
    slug = "opentable"
    categories: ClassVar[list[Category]] = [Category.RESTAURANT]
    gate = GateReason(
        summary="affiliate approval required",
        blocker="restaurant booking API is gated behind an affiliate agreement",
    )


class FoodicsAdapter(_PartnerStub):
    slug = "foodics"
    categories: ClassVar[list[Category]] = [Category.RESTAURANT, Category.FOOD_DELIVERY]
    gate = GateReason(
        summary="merchant app registration required",
        blocker="needs a registered app against a merchant's Foodics account",
    )


class BooksyAdapter(_PartnerStub):
    slug = "booksy"
    categories: ClassVar[list[Category]] = [Category.SALON, Category.SPA]
    gate = GateReason(
        summary="partner API licence required",
        blocker="no public booking API; access is by commercial agreement",
    )


class TalabatAdapter(_PartnerStub):
    slug = "talabat"
    categories: ClassVar[list[Category]] = [Category.FOOD_DELIVERY]
    gate = GateReason(
        summary="partnership required",
        blocker="no public ordering API in the UAE market",
    )


class CareemAdapter(_PartnerStub):
    slug = "careem"
    categories: ClassVar[list[Category]] = [Category.RIDE, Category.FOOD_DELIVERY]
    gate = GateReason(
        summary="partnership required",
        blocker="Careem Everything App APIs are partner-gated",
    )


PARTNER_ADAPTERS: tuple[type[_PartnerStub], ...] = (
    DeliverooAdapter,
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
    "DeliverooAdapter",
    "FoodicsAdapter",
    "GateReason",
    "OpenTableAdapter",
    "TalabatAdapter",
]
