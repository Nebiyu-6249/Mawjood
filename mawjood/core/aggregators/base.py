"""The adapter contract. Everything hangs off this.

Every aggregator is an independent plugin implementing ONE interface. Core logic
contains zero aggregator-specific branches: adding a platform is one new file and
one config row.

**The critical rule: adapters do not raise into the router.** Every method returns
a typed ``Result``. The router decides what an outcome *means*; the adapter only
reports. A conformance test fault-injects the transport and asserts that adapters
still return a Result rather than propagating an exception — because an exception
escaping into the cascade is how a consumer ends up seeing a stack trace instead
of a graceful pivot.

Pinned in CLAUDE.md section 4. Do not change without asking.
"""

from __future__ import annotations

import time
import uuid
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Protocol, runtime_checkable

from mawjood.core.enums import Category, Outcome

# ---------------------------------------------------------------------------
# Deadline
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Deadline:
    """The remaining budget for a turn.

    Monotonic, so a clock adjustment mid-cascade cannot make a deadline jump.
    Threaded into every adapter call so an adapter can size its own timeout
    rather than being killed halfway through a write it cannot report on.
    """

    expires_at_monotonic: float

    @classmethod
    def in_ms(cls, milliseconds: int) -> Deadline:
        return cls(expires_at_monotonic=time.monotonic() + milliseconds / 1000)

    @classmethod
    def expired_now(cls) -> Deadline:
        return cls(expires_at_monotonic=time.monotonic())

    @property
    def remaining_ms(self) -> int:
        return max(0, int((self.expires_at_monotonic - time.monotonic()) * 1000))

    @property
    def expired(self) -> bool:
        return self.remaining_ms <= 0

    def shrink_to(self, milliseconds: int) -> Deadline:
        """The tighter of this deadline and a per-call cap."""
        candidate = time.monotonic() + milliseconds / 1000
        return Deadline(expires_at_monotonic=min(self.expires_at_monotonic, candidate))


# ---------------------------------------------------------------------------
# Identity and context
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class MerchantRef:
    """One merchant on one platform.

    Zenoti-style APIs are merchant-side: each salon has its own tenant/centre ids
    and key (decision 1). The cascade therefore iterates platform → merchants
    within that platform, not platform → platform.
    """

    platform_slug: str
    merchant_id: uuid.UUID
    display_name: str
    external_ids: Mapping[str, str] = field(default_factory=dict)
    area: str | None = None

    def __str__(self) -> str:
        return f"{self.platform_slug}:{self.display_name}"


@dataclass(frozen=True, slots=True)
class CallContext:
    """Everything an adapter needs for one call, and nothing more.

    Credentials are resolved by the router and handed over here. Adapters never
    read the secret store themselves — that keeps least privilege intact and
    makes every adapter testable without a secrets backend.
    """

    tenant_id: uuid.UUID
    conversation_id: uuid.UUID
    merchant: MerchantRef
    credentials: Mapping[str, str]
    deadline: Deadline
    correlation_id: str

    @property
    def platform_slug(self) -> str:
        return self.merchant.platform_slug


# ---------------------------------------------------------------------------
# Result
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Result[T]:
    """What every adapter method returns. Never an exception."""

    outcome: Outcome
    data: T | None = None
    # Operator-facing only. Never rendered to a consumer; the audit trail keeps
    # it and the phrasebank keeps consumers away from it.
    raw_error: str | None = None
    latency_ms: int = 0
    request_id: str | None = None

    @property
    def ok(self) -> bool:
        return self.outcome is Outcome.OK

    def __bool__(self) -> bool:
        raise TypeError(
            "Result has no truth value. Check .ok or match on .outcome — an "
            "implicit bool would silently treat NO_AVAILABILITY as a failure "
            "and TIMEOUT as a success."
        )


# ---------------------------------------------------------------------------
# Domain payloads
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Slot:
    """A bookable appointment slot."""

    slot_id: str
    start: datetime
    end: datetime
    venue_name: str
    service_name: str
    merchant: MerchantRef
    staff_name: str | None = None
    price_amount: float | None = None
    price_currency: str | None = None
    area: str | None = None
    # Some upstreams demand a deposit. v1 moves no money (decision 3), so these
    # route to handoff rather than being offered.
    requires_deposit: bool = False

    @property
    def platform_slug(self) -> str:
        return self.merchant.platform_slug


@dataclass(frozen=True, slots=True)
class AvailabilityRequest:
    category: Category
    service: str
    window_start: datetime
    window_end: datetime
    area: str | None = None
    party_size: int = 1
    staff_preference: str | None = None

    def as_audit_inputs(self) -> dict[str, Any]:
        return {
            "category": str(self.category),
            "service": self.service,
            "area": self.area,
            "window_start": self.window_start.isoformat(),
            "window_end": self.window_end.isoformat(),
            "party_size": self.party_size,
            "staff_preference": self.staff_preference,
        }


@dataclass(frozen=True, slots=True)
class BookingRequest:
    """A request to book a specific slot.

    ``idempotency_key`` is derived from (conversation, category, slot start,
    aggregator) and is the anchor of the double-booking defence: on a create
    timeout the cascade reconciles against this key rather than blindly
    retrying elsewhere (CLAUDE.md section 5.1).
    """

    slot: Slot
    idempotency_key: str
    consumer_name: str | None = None
    consumer_phone: str | None = None
    notes: str | None = None


@dataclass(frozen=True, slots=True)
class BookingRef:
    """Whatever the platform calls the thing it just created."""

    external_id: str
    platform_slug: str
    merchant: MerchantRef
    confirmation_code: str | None = None


@dataclass(frozen=True, slots=True)
class BookingRecord:
    ref: BookingRef
    status: str
    slot_start: datetime | None = None
    slot_end: datetime | None = None
    venue_name: str | None = None
    service_name: str | None = None


@dataclass(frozen=True, slots=True)
class Capabilities:
    """What an adapter can actually do.

    The router reads this before attempting an action; a capability it does not
    have produces UNSUPPORTED rather than a call that fails at the far end.
    """

    can_search: bool = True
    can_book: bool = True
    can_reschedule: bool = False
    can_cancel: bool = False
    can_pick_staff: bool = False
    # Whether the platform honours an idempotency key on create. False means the
    # reconciliation path is the only defence against a double booking, and the
    # router must never retry a create against it.
    honours_idempotency: bool = False


# ---------------------------------------------------------------------------
# The interface
# ---------------------------------------------------------------------------


@runtime_checkable
class AggregatorAdapter(Protocol):
    """One aggregator platform."""

    # Read-only properties rather than mutable attributes: adapters declare these
    # once as class attributes, and nothing should be reassigning them at runtime.
    @property
    def slug(self) -> str: ...

    @property
    def categories(self) -> list[Category]: ...

    @property
    def capabilities(self) -> Capabilities: ...

    async def search_availability(
        self, ctx: CallContext, req: AvailabilityRequest
    ) -> Result[list[Slot]]: ...

    async def create_booking(self, ctx: CallContext, req: BookingRequest) -> Result[BookingRef]: ...

    async def get_booking(self, ctx: CallContext, ref: BookingRef) -> Result[BookingRecord]: ...

    async def find_by_idempotency_key(
        self, ctx: CallContext, idempotency_key: str
    ) -> Result[BookingRecord]:
        """The reconciliation read.

        Called after ``create_booking`` times out, when we do not know whether the
        booking landed. Returns OK with a record if it did, NO_AVAILABILITY if it
        provably did not, and anything else to mean "still unknown" — which sends
        the conversation to a human rather than risking a second booking.
        """
        ...

    async def reschedule(
        self, ctx: CallContext, ref: BookingRef, slot: Slot
    ) -> Result[BookingRef]: ...

    async def cancel(
        self, ctx: CallContext, ref: BookingRef, reason: str | None
    ) -> Result[None]: ...

    async def health(self, ctx: CallContext) -> Result[None]: ...


def make_idempotency_key(
    *,
    conversation_id: uuid.UUID,
    category: Category | str,
    slot_start: datetime,
    platform_slug: str,
) -> str:
    """Derived, not random, so a retry of the same intent produces the same key."""
    return f"{conversation_id}:{category}:{slot_start.isoformat()}:{platform_slug}"


__all__ = [
    "AggregatorAdapter",
    "AvailabilityRequest",
    "BookingRecord",
    "BookingRef",
    "BookingRequest",
    "CallContext",
    "Capabilities",
    "Deadline",
    "MerchantRef",
    "Result",
    "Slot",
    "make_idempotency_key",
]
