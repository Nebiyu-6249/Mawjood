"""Zenoti — **blocked on documentation, deliberately not implemented.**

Zenoti was to be the first live adapter. It is not built, because the rule in
CLAUDE.md section 10 is absolute:

> Do not guess API endpoints from memory. Before writing an adapter, fetch that
> platform's current public API docs... **If you cannot reach the docs, say so and
> stop. Do not invent endpoints.**

## What was attempted (2026-07-29)

| Route | Result |
|---|---|
| `docs.zenoti.com/` | HTTP 403 |
| `docs.zenoti.com/docs/overview` | HTTP 403 |
| `docs.zenoti.com/docs/service-booking-apis` | HTTP 403 |
| `docs.zenoti.com/reference/retrieve-available-slots-for-a-service-booking` | HTTP 403 |
| `api.zenoti.com/` | HTTP 403 |
| direct `curl` with a browser user-agent | connection blocked |

Every path on the documentation host refuses automated access. A web search
returned page *titles* confirming a booking workflow in this order — create a
service booking, retrieve available slots, reserve a slot, confirm, reschedule —
but no endpoint paths, no authentication header, no request or response shapes,
and no error taxonomy.

**That is not enough to write an adapter with.** Knowing the sequence of
operations is not knowing the API. Inventing paths and payloads would produce code
that looks finished, passes fixtures written from the same guesses, and fails the
first time it meets the real service — while the fixtures assert it works.

## What is needed to finish this

Someone with browser access should record in `docs/INTEGRATION_NOTES.md`:

1. Base URL per region, and the authentication header (name and token format).
2. Exact method and path for: guest lookup/create, list services, list staff,
   retrieve availability, reserve, confirm, retrieve booking, reschedule, cancel.
3. Whether reserve/confirm honours an idempotency key, and if not, **how to
   determine after a timeout whether a booking landed** — the most important entry
   of all, because the double-booking defence depends on it.
4. Error response shapes, specifically how "no availability" is distinguished from
   a genuine failure.
5. Rate limits and pagination.

Then replace the methods below. Nothing outside this file changes.

## Meanwhile

This adapter registers cleanly and returns `UNSUPPORTED` for everything, which the
router sends to human handoff — it never synthesises success. The architecture is
proven end to end against `core/aggregators/fake/`, which exercises the identical
contract.
"""

from __future__ import annotations

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

# Corroborated from documentation page titles only. Paths, auth and payloads are
# NOT known and are deliberately absent — there is nothing here that could be
# mistaken for a verified endpoint.
DOCUMENTED_WORKFLOW = (
    "create a service booking",
    "retrieve available slots for the booking",
    "reserve a slot",
    "confirm the booking",
    "reschedule or cancel",
)


class ZenotiAdapter:
    """Registers, declares no capability, performs nothing.

    The same treatment as the partner-gated platforms: present in the registry so
    it drops into the live system the day the contract is known, and honest about
    being unable to act until then.
    """

    slug = "zenoti"
    categories: ClassVar[list[Category]] = [Category.SALON, Category.SPA]
    capabilities = Capabilities(
        can_search=False,
        can_book=False,
        can_reschedule=False,
        can_cancel=False,
        can_pick_staff=False,
        honours_idempotency=False,
    )
    implemented = False
    blocked_reason = "Zenoti API documentation is unreachable (HTTP 403 on every path)"

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


__all__ = ["DOCUMENTED_WORKFLOW", "ZenotiAdapter"]
