"""Fake adapters: the doubles the cascade is proven against.

Each isolates one thing the router must survive:

| slug                 | behaviour                                              |
|----------------------|--------------------------------------------------------|
| `fake_happy`         | always has slots, books cleanly                        |
| `fake_empty`         | always NO_AVAILABILITY — nothing free, not an error    |
| `fake_timeout`       | always TIMEOUT, including on create                    |
| `fake_inconclusive`  | times out on create *and* on the reconciliation read   |
| `fake_flaky`         | fails a fixed fraction, seeded and reproducible        |
| `fake_auth`          | always AUTH_ERROR — our problem, never the consumer's  |
| `fake_unsupported`   | registers cleanly, performs nothing                    |
| `fake_rate_limited`  | always 429                                             |
| `fake_low_confidence`| returns a slot it cannot vouch for                     |

They are real adapters, not mocks: same Protocol, same conformance suite as a
live platform. That is what makes a cascade test meaningful rather than a test of
a mock's configuration.

`fake_flaky` is seeded. A flaky test that is genuinely flaky is worse than none —
a failure has to mean something.
"""

from __future__ import annotations

import asyncio
import random
import uuid
from datetime import UTC, datetime, timedelta
from typing import ClassVar

from mawjood.core.aggregators.base import (
    AvailabilityRequest,
    BookingRecord,
    BookingRef,
    BookingRequest,
    CallContext,
    Capabilities,
    MerchantRef,
    Result,
    Slot,
)
from mawjood.core.enums import Category, Outcome

ALL_CATEGORIES = list(Category)


def _slot(
    ctx: CallContext,
    req: AvailabilityRequest,
    *,
    offset_minutes: int = 0,
    price: float = 120.0,
    staff: str | None = None,
) -> Slot:
    start = req.window_start + timedelta(minutes=offset_minutes)
    return Slot(
        slot_id=f"{ctx.merchant.platform_slug}-{start.isoformat()}",
        start=start,
        end=start + timedelta(minutes=45),
        venue_name=ctx.merchant.display_name,
        service_name=req.service,
        merchant=ctx.merchant,
        staff_name=staff,
        price_amount=price,
        price_currency="AED",
        area=ctx.merchant.area or req.area,
    )


class _BaseFake:
    """Shared plumbing. Subclasses override only what they change."""

    slug = "fake"
    categories: ClassVar[list[Category]] = ALL_CATEGORIES
    capabilities = Capabilities(
        can_search=True,
        can_book=True,
        can_reschedule=True,
        can_cancel=True,
        can_pick_staff=True,
        honours_idempotency=True,
    )

    def __init__(self) -> None:
        # Every create that succeeded, keyed by idempotency key. The
        # double-booking test sums this across all fakes and asserts it is one.
        self.created: dict[str, BookingRef] = {}
        self.calls: list[str] = []

    def _record(self, method: str) -> None:
        self.calls.append(method)

    async def search_availability(
        self, ctx: CallContext, req: AvailabilityRequest
    ) -> Result[list[Slot]]:
        self._record("search_availability")
        return Result(outcome=Outcome.OK, data=[_slot(ctx, req)], latency_ms=10)

    async def create_booking(self, ctx: CallContext, req: BookingRequest) -> Result[BookingRef]:
        self._record("create_booking")
        existing = self.created.get(req.idempotency_key)
        if existing is not None:
            # Honouring the key: a repeat is the same booking, not a second one.
            return Result(outcome=Outcome.OK, data=existing, latency_ms=5)
        ref = BookingRef(
            external_id=f"{self.slug}-{uuid.uuid4().hex[:10]}",
            platform_slug=self.slug,
            merchant=ctx.merchant,
            confirmation_code=uuid.uuid4().hex[:6].upper(),
        )
        self.created[req.idempotency_key] = ref
        return Result(outcome=Outcome.OK, data=ref, latency_ms=20)

    async def get_booking(self, ctx: CallContext, ref: BookingRef) -> Result[BookingRecord]:
        self._record("get_booking")
        for stored in self.created.values():
            if stored.external_id == ref.external_id:
                return Result(
                    outcome=Outcome.OK,
                    data=BookingRecord(ref=stored, status="confirmed"),
                    latency_ms=8,
                )
        return Result(outcome=Outcome.NO_AVAILABILITY, latency_ms=8)

    async def find_by_idempotency_key(
        self, ctx: CallContext, idempotency_key: str
    ) -> Result[BookingRecord]:
        self._record("find_by_idempotency_key")
        stored = self.created.get(idempotency_key)
        if stored is None:
            # Conclusive: it definitely did not land.
            return Result(outcome=Outcome.NO_AVAILABILITY, latency_ms=8)
        return Result(
            outcome=Outcome.OK, data=BookingRecord(ref=stored, status="confirmed"), latency_ms=8
        )

    async def reschedule(self, ctx: CallContext, ref: BookingRef, slot: Slot) -> Result[BookingRef]:
        self._record("reschedule")
        return Result(outcome=Outcome.OK, data=ref, latency_ms=15)

    async def cancel(self, ctx: CallContext, ref: BookingRef, reason: str | None) -> Result[None]:
        self._record("cancel")
        return Result(outcome=Outcome.OK, latency_ms=10)

    async def health(self, ctx: CallContext) -> Result[None]:
        self._record("health")
        return Result(outcome=Outcome.OK, latency_ms=2)


class HappyFake(_BaseFake):
    """Always has something, books cleanly. The control case."""

    slug = "fake_happy"

    async def search_availability(
        self, ctx: CallContext, req: AvailabilityRequest
    ) -> Result[list[Slot]]:
        self._record("search_availability")
        slots = [
            _slot(ctx, req, offset_minutes=0, price=120.0, staff="Rania"),
            _slot(ctx, req, offset_minutes=30, price=120.0, staff="Mei"),
            _slot(ctx, req, offset_minutes=60, price=140.0, staff="Rania"),
        ]
        return Result(outcome=Outcome.OK, data=slots, latency_ms=120)


class EmptyFake(_BaseFake):
    """Never has a slot. Not an error — just nothing free.

    The router must advance rather than treat this as a failure, and the
    consumer must never learn it happened.
    """

    slug = "fake_empty"

    async def search_availability(
        self, ctx: CallContext, req: AvailabilityRequest
    ) -> Result[list[Slot]]:
        self._record("search_availability")
        return Result(outcome=Outcome.NO_AVAILABILITY, latency_ms=90)

    async def create_booking(self, ctx: CallContext, req: BookingRequest) -> Result[BookingRef]:
        self._record("create_booking")
        return Result(outcome=Outcome.NO_AVAILABILITY, latency_ms=40)


class TimeoutFake(_BaseFake):
    """Never answers in time, including on create.

    A create timeout is the dangerous one: we do not know whether the booking
    landed, so the router must reconcile before it dares advance.
    """

    slug = "fake_timeout"

    def __init__(self, *, delay_s: float = 0.0, lands_anyway: bool = False) -> None:
        super().__init__()
        self._delay_s = delay_s
        # True models the nastiest real case: the request timed out on our side
        # but the booking *did* land upstream.
        self._lands_anyway = lands_anyway

    async def _stall(self) -> None:
        if self._delay_s:
            await asyncio.sleep(self._delay_s)

    async def search_availability(
        self, ctx: CallContext, req: AvailabilityRequest
    ) -> Result[list[Slot]]:
        self._record("search_availability")
        await self._stall()
        return Result(
            outcome=Outcome.TIMEOUT,
            raw_error="ReadTimeout after 2500ms on GET /slots",
            latency_ms=2500,
        )

    async def create_booking(self, ctx: CallContext, req: BookingRequest) -> Result[BookingRef]:
        self._record("create_booking")
        await self._stall()
        if self._lands_anyway:
            # It worked upstream; we just never heard back. Reconciliation is
            # the only way to find out.
            self.created[req.idempotency_key] = BookingRef(
                external_id=f"{self.slug}-landed-{uuid.uuid4().hex[:8]}",
                platform_slug=self.slug,
                merchant=ctx.merchant,
            )
        return Result(
            outcome=Outcome.TIMEOUT,
            raw_error="ReadTimeout after 2500ms on POST /bookings",
            latency_ms=2500,
        )


class InconclusiveTimeoutFake(TimeoutFake):
    """Times out on create *and* cannot answer the reconciliation read.

    The genuinely unknowable case. The router must hand to a human rather than
    guess: guessing either way risks a double booking or a lost one.
    """

    slug = "fake_inconclusive"

    async def find_by_idempotency_key(
        self, ctx: CallContext, idempotency_key: str
    ) -> Result[BookingRecord]:
        self._record("find_by_idempotency_key")
        return Result(
            outcome=Outcome.TIMEOUT,
            raw_error="reconciliation read also timed out",
            latency_ms=900,
        )


class FlakyFake(_BaseFake):
    """Fails a fixed fraction of calls. Seeded, so a failure is reproducible."""

    slug = "fake_flaky"

    def __init__(self, *, failure_rate: float = 0.4, seed: int = 1729) -> None:
        super().__init__()
        self._failure_rate = failure_rate
        self._random = random.Random(seed)  # noqa: S311  not cryptographic

    def _should_fail(self) -> bool:
        return self._random.random() < self._failure_rate

    async def search_availability(
        self, ctx: CallContext, req: AvailabilityRequest
    ) -> Result[list[Slot]]:
        self._record("search_availability")
        if self._should_fail():
            return Result(
                outcome=Outcome.UPSTREAM_ERROR, raw_error="502 Bad Gateway", latency_ms=300
            )
        return Result(outcome=Outcome.OK, data=[_slot(ctx, req, price=95.0)], latency_ms=180)

    async def create_booking(self, ctx: CallContext, req: BookingRequest) -> Result[BookingRef]:
        self._record("create_booking")
        if self._should_fail():
            return Result(
                outcome=Outcome.UPSTREAM_ERROR, raw_error="503 Service Unavailable", latency_ms=250
            )
        return await super().create_booking(ctx, req)


class AuthFailFake(_BaseFake):
    """Credentials wrong or expired.

    Our problem, not the consumer's: the router advances, marks the merchant
    credential degraded and alerts ops, and the consumer sees a pivot.
    """

    slug = "fake_auth"

    async def search_availability(
        self, ctx: CallContext, req: AvailabilityRequest
    ) -> Result[list[Slot]]:
        self._record("search_availability")
        return Result(
            outcome=Outcome.AUTH_ERROR,
            raw_error="401 Unauthorized: key revoked",  # gitleaks:allow — an error string, not a key
            latency_ms=60,
        )

    async def create_booking(self, ctx: CallContext, req: BookingRequest) -> Result[BookingRef]:
        self._record("create_booking")
        return Result(outcome=Outcome.AUTH_ERROR, raw_error="401 Unauthorized", latency_ms=60)


class UnsupportedFake(_BaseFake):
    """Registers cleanly, performs nothing.

    The shape every partner-gated platform ships as until credentials arrive.
    """

    slug = "fake_unsupported"
    capabilities = Capabilities(
        can_search=False, can_book=False, can_reschedule=False, can_cancel=False
    )

    async def search_availability(
        self, ctx: CallContext, req: AvailabilityRequest
    ) -> Result[list[Slot]]:
        self._record("search_availability")
        return Result(outcome=Outcome.UNSUPPORTED, latency_ms=1)

    async def create_booking(self, ctx: CallContext, req: BookingRequest) -> Result[BookingRef]:
        self._record("create_booking")
        return Result(outcome=Outcome.UNSUPPORTED, latency_ms=1)


class RateLimitedFake(_BaseFake):
    """Always rate limited. The router backs that merchant off and advances."""

    slug = "fake_rate_limited"

    async def search_availability(
        self, ctx: CallContext, req: AvailabilityRequest
    ) -> Result[list[Slot]]:
        self._record("search_availability")
        return Result(
            outcome=Outcome.RATE_LIMITED, raw_error="429 Too Many Requests", latency_ms=30
        )


class LowConfidenceFake(_BaseFake):
    """Returns a slot it cannot vouch for.

    **Semantics proposed, not signed off** (CLAUDE.md section 4): held as a
    fallback candidate, never auto-booked, surfaced only with hedged copy and an
    explicit confirmation if nothing better appears.
    """

    slug = "fake_low_confidence"

    async def search_availability(
        self, ctx: CallContext, req: AvailabilityRequest
    ) -> Result[list[Slot]]:
        self._record("search_availability")
        return Result(
            outcome=Outcome.LOW_CONFIDENCE,
            data=[_slot(ctx, req, price=110.0)],
            raw_error="fuzzy service-name match below threshold",
            latency_ms=140,
        )


def fake_merchant(
    platform_slug: str, name: str = "Test Venue", area: str | None = "Dubai Marina"
) -> MerchantRef:
    return MerchantRef(
        platform_slug=platform_slug,
        merchant_id=uuid.uuid5(uuid.NAMESPACE_DNS, f"{platform_slug}:{name}"),
        display_name=name,
        external_ids={"centre_id": "c-001"},
        area=area,
    )


def default_window() -> tuple[datetime, datetime]:
    start = datetime.now(UTC).replace(microsecond=0) + timedelta(hours=6)
    return start, start + timedelta(hours=3)


__all__ = [
    "ALL_CATEGORIES",
    "AuthFailFake",
    "EmptyFake",
    "FlakyFake",
    "HappyFake",
    "InconclusiveTimeoutFake",
    "LowConfidenceFake",
    "RateLimitedFake",
    "TimeoutFake",
    "UnsupportedFake",
    "default_window",
    "fake_merchant",
]
