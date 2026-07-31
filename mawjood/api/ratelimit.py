"""Rate limiting for the inbound webhook.

A token bucket per key, held in process memory.

## The threat this addresses, and the one it does not

The webhook is the only unauthenticated write path Mawjood exposes. Signature
verification rejects forged bodies, but rejection still costs a TCP connection,
a TLS handshake and an HMAC — cheap individually, not free in volume. So there
are two limits, applied at different points:

* **Per source address, before the signature is checked.** Bounds the cost of
  traffic that will be rejected anyway. Generous, because a legitimate BSP
  delivers everything from a handful of addresses and must never be throttled.
* **Per consumer, after the payload is parsed.** Bounds the cost of one
  conversation flooding — a stuck client resending, or someone with a script.
  Tighter, because no real person sends thirty messages a minute.

**What this does not address:** a distributed flood from many addresses. That is
an edge concern — a CDN or WAF in front of the deployment — and pretending an
in-process bucket solves it would be worse than saying so.

## The honest limitation

State is per process. Two application instances mean two buckets and therefore
twice the configured rate. That is acceptable at v1's single-instance footprint
and it is documented in DEPLOY.md rather than hidden. The interface below takes
a store, so moving to Redis is one implementation, not a rewrite.

Rate limiting never produces a consumer-visible message. A throttled request is
answered with 429 to the *provider*, which retries. The consumer sees nothing —
the invariant holds here as everywhere.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Protocol


@dataclass(frozen=True, slots=True)
class Limit:
    """A rate, expressed the way an operator thinks about it."""

    # Sustained rate.
    per_minute: int
    # How much can arrive at once. Defaults to the per-minute rate, which allows a
    # legitimate batch delivery through while still bounding sustained volume.
    burst: int | None = None

    @property
    def capacity(self) -> float:
        return float(self.burst if self.burst is not None else self.per_minute)

    @property
    def refill_per_second(self) -> float:
        return self.per_minute / 60.0

    def __post_init__(self) -> None:
        if self.per_minute <= 0:
            raise ValueError("per_minute must be positive")
        if self.burst is not None and self.burst <= 0:
            raise ValueError("burst must be positive")


@dataclass(slots=True)
class _Bucket:
    tokens: float
    updated_at: float


class RateLimitStore(Protocol):
    """Where bucket state lives.

    A Protocol so a Redis-backed store drops in for a multi-instance deployment
    without touching the caller.
    """

    def take(self, key: str, limit: Limit, *, now: float) -> bool: ...


class InMemoryStore:
    """Per-process token buckets.

    Idle keys are swept lazily rather than on a timer: a background task would
    need a lifecycle, and a webhook that has been quiet long enough for a bucket
    to matter has no memory pressure to speak of.
    """

    def __init__(self, *, max_keys: int = 10_000) -> None:
        self._buckets: dict[str, _Bucket] = {}
        self._max_keys = max_keys

    def take(self, key: str, limit: Limit, *, now: float) -> bool:
        bucket = self._buckets.get(key)
        if bucket is None:
            if len(self._buckets) >= self._max_keys:
                self._sweep(now, limit)
            bucket = _Bucket(tokens=limit.capacity, updated_at=now)
            self._buckets[key] = bucket

        elapsed = max(0.0, now - bucket.updated_at)
        bucket.tokens = min(limit.capacity, bucket.tokens + elapsed * limit.refill_per_second)
        bucket.updated_at = now

        if bucket.tokens < 1.0:
            return False
        bucket.tokens -= 1.0
        return True

    def _sweep(self, now: float, limit: Limit) -> None:
        """Drop buckets that have refilled completely — they carry no state."""
        full_after = limit.capacity / limit.refill_per_second
        stale = [k for k, b in self._buckets.items() if now - b.updated_at > full_after]
        for key in stale:
            del self._buckets[key]
        if not stale:
            # Everything is live. Drop the oldest half rather than growing without
            # bound; the cost is that some callers get a fresh full bucket, which
            # is the safe direction to be wrong in.
            oldest = sorted(self._buckets.items(), key=lambda kv: kv[1].updated_at)
            for key, _ in oldest[: len(oldest) // 2]:
                del self._buckets[key]


@dataclass(slots=True)
class RateLimiter:
    """Applies named limits against a store."""

    store: RateLimitStore = field(default_factory=InMemoryStore)

    def allow(self, scope: str, key: str, limit: Limit, *, now: float | None = None) -> bool:
        """Whether this request may proceed. ``scope`` namespaces the key."""
        return self.store.take(
            f"{scope}:{key}", limit, now=now if now is not None else time.monotonic()
        )


def client_address(headers: dict[str, str], fallback: str | None) -> str:
    """The caller's address, honouring a single trusted proxy hop.

    ``X-Forwarded-For`` is attacker-controlled when nothing strips it, so only
    the **last** entry is used — the one the nearest proxy appended. Taking the
    first entry, which is the common mistake, lets anyone choose their own rate
    limit key by sending a header.
    """
    forwarded = headers.get("x-forwarded-for")
    if forwarded:
        hops = [hop.strip() for hop in forwarded.split(",") if hop.strip()]
        if hops:
            return hops[-1]
    return fallback or "unknown"


__all__ = [
    "InMemoryStore",
    "Limit",
    "RateLimitStore",
    "RateLimiter",
    "client_address",
]
