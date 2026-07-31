# ADD AN AGGREGATOR

How to connect a new booking platform to Mawjood, end to end.

This is the page that matters most in the operator pack. Every gated integration
— OpenTable, Foodics, Booksy, Talabat, Careem, and Zenoti itself — lands *after*
launch, when whoever is holding this repo may not be whoever built it. So this is
written for that person: someone competent, with the credentials in hand, who has
never read the rest of the codebase.

**The claim you are testing:** adding a platform is *one new file and one config
row*. Core logic contains zero aggregator-specific branches. If you find yourself
editing `core/routing/`, `core/conversation/`, `api/` or `db/` to make your
platform work, stop — something is wrong, and `tests/adapters/test_plugin_claim.py`
will fail before you can commit it.

Budget: **half a day to a day** for a platform with reachable docs and a working
sandbox. Most of that is step 1, and most of the rest is step 4.

---

## Contents

1. [Step 1 — Read the docs. Actually read them.](#step-1--read-the-docs-actually-read-them)
2. [Step 2 — Check the direction of the API](#step-2--check-the-direction-of-the-api)
3. [Step 3 — Write the file](#step-3--write-the-file)
4. [Step 4 — Map upstream failures onto `Outcome`](#step-4--map-upstream-failures-onto-outcome)
5. [Step 5 — Idempotency and the reconciliation read](#step-5--idempotency-and-the-reconciliation-read)
6. [Step 6 — Register it](#step-6--register-it)
7. [Step 7 — Credentials](#step-7--credentials)
8. [Step 8 — The config rows](#step-8--the-config-rows)
9. [Step 9 — Tests](#step-9--tests)
10. [Step 10 — Drive it by hand](#step-10--drive-it-by-hand)
11. [The checklist](#the-checklist)
12. [Five mistakes that will bite you](#five-mistakes-that-will-bite-you)

---

## Step 1 — Read the docs. Actually read them.

**Do not guess endpoints from memory.** This is a hard rule (`CLAUDE.md` §10), and
it is the reason Zenoti and Deliveroo ship as stubs rather than as plausible-looking
code: their documentation hosts returned HTTP 403 from this environment on every
path, so no endpoints were written. A guessed path looks exactly like a known one
in a diff, and the difference only surfaces against a live merchant.

Before you write a line, get answers to these **eight questions** and record them
in `docs/INTEGRATION_NOTES.md` under a heading for your platform:

| # | Question | Why it matters |
|---|---|---|
| 1 | Exact endpoint paths and methods for: availability search, create, retrieve, reschedule, cancel, health. | These become your adapter's six calls. |
| 2 | Auth model — API key header, OAuth client-credentials, per-merchant token? Where does refresh happen? | Determines what goes in `secret_ref` and whether you need a token cache. |
| 3 | Is auth **per merchant** or **per platform**? | Mawjood assumes per-merchant (decision 1). Per-platform still works — every merchant row just points at the same `secret_ref`. |
| 4 | Does create accept an **idempotency key**? What header, what uniqueness window? | Sets `Capabilities.honours_idempotency` and decides how §5.1 protects you. |
| 5 | After a create times out, how do you find out whether it landed? | This is `find_by_idempotency_key`. If there is no answer, say so — that platform's timeouts go to a human every time. |
| 6 | Error shapes: status codes, error body schema, which codes are retryable. | Drives the `Outcome` mapping in step 4. |
| 7 | Rate limits — requests per second, per minute, per merchant or per app; and the 429 response shape (`Retry-After`?). | Drives `RATE_LIMITED` and the circuit breaker. |
| 8 | Pagination on search, and whether prices/deposits come back on a slot. | A slot demanding a deposit routes to handoff — v1 moves no money (decision 3). |

Write down what the docs are *vague* about too. "The docs do not say whether the
idempotency window is 24h or permanent" is a useful sentence in six months; a
silent assumption is not.

> **If you cannot reach the documentation, stop and say so.** Ship the platform as
> a stub in `partners.py` with a `GateReason` checklist and move on. That is not a
> failure — it is four of the six platforms in this repo today, and it is honest.

---

## Step 2 — Check the direction of the API

This is the check that costs an hour and saves a fortnight, and it is not obvious
until it has bitten you.

Most "booking platform" APIs are **merchant-side**: they exist so a restaurant's
own POS can receive the orders the platform has already taken, or so a salon's own
software can sync its diary. They are not consumer-side ordering APIs. A
third-party assistant cannot use them to *place* anything.

Deliveroo is the worked example, and it is documented in
`mawjood/core/aggregators/deliveroo/__init__.py`. Its Order API receives orders
Deliveroo has already taken through its own apps. It does not place them. That
distinction survives a partnership — signing a contract does not turn a webhook
receiver into an ordering endpoint. The adapter encodes this per method:

```python
class Support(StrEnum):
    BLOCKED = "blocked"  # a partnership would fix this
    NOT_WHAT_THIS_API_IS_FOR = "not_what..."  # a partnership would not

    @property
    def clears_with_a_partnership(self) -> bool:
        return self is Support.BLOCKED
```

Ask, in this order:

1. **Can a third party create a booking on a consumer's behalf at all?** If the
   answer is "no, you can only deep-link into our app", this platform can never
   complete a booking in Mawjood. It may still be worth a *search-only* adapter —
   `Capabilities(can_search=True, can_book=False)` — so the consumer gets real
   options and the handoff carries a real venue name. Say so explicitly in
   `INTEGRATION_NOTES.md` and set the capability flags honestly. Never let
   `create_booking` return `OK` for something a human still has to do.
2. **Does the merchant have to install something on their side?** Foodics-style
   platforms need an app registered against each merchant's account. That is an
   operational task per salon, not an engineering one, and it belongs on the
   activation checklist before anyone promises a launch date.
3. **Is the sandbox representative?** Sandboxes that never return 429 or 5xx will
   let you ship an `Outcome` mapping you have never exercised. Step 9 covers that
   with fault injection rather than hope.

---

## Step 3 — Write the file

One file. `mawjood/core/aggregators/<slug>/__init__.py`, or a package if the
platform is big enough to want `client.py` and `mapping.py` beside it.

There is **no base class to inherit**. `AggregatorAdapter` is a `Protocol`, and
conformance is structural — `tests/adapters/test_plugin_claim.py` asserts that a
new adapter's MRO is `(object,)`, precisely so nobody invents a helpful base class
that quietly changes behaviour for every platform at once.

Here is the whole skeleton. It compiles, it typechecks, and it is the shape every
adapter in this repo has:

```python
"""Acme Bookings — salon and spa appointments.

Docs: https://developer.acme.example/v2 (retrieved 2026-07-31, recorded in
docs/INTEGRATION_NOTES.md). Auth is a per-merchant API key plus a centre id.
"""

from __future__ import annotations

from typing import ClassVar

import httpx

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


class AcmeAdapter:
    slug = "acme"
    categories: ClassVar[list[Category]] = [Category.SALON, Category.SPA]
    capabilities = Capabilities(
        can_search=True,
        can_book=True,
        can_reschedule=True,
        can_cancel=True,
        can_pick_staff=False,
        honours_idempotency=True,
    )

    async def search_availability(
        self, ctx: CallContext, req: AvailabilityRequest
    ) -> Result[list[Slot]]:
        centre = ctx.merchant.external_ids.get("centre_id", "")
        async with self._client(ctx) as client:
            response = await client.get(
                f"/v2/centres/{centre}/availability",
                params={
                    "service": req.service,
                    "from": req.window_start.isoformat(),
                    "to": req.window_end.isoformat(),
                },
            )
        # ... map the body into Slot objects ...

    # create_booking, get_booking, find_by_idempotency_key,
    # reschedule, cancel, health — all six, all returning Result.
```

Four rules, all load-bearing:

**(a) Never raise into the router.** Every method returns a `Result`, always,
including on `httpx.ConnectError`, a malformed body, a `KeyError` on a field the
docs promised. An exception escaping an adapter is how a consumer ends up seeing a
failure instead of a graceful pivot — which is the one thing this product may not
do. `tests/adapters/test_conformance.py` fault-injects the transport and asserts
this for every registered adapter, so a leak fails the build rather than a booking.

The idiom is a single wrapper per call:

```python
    async def _call(self, ctx: CallContext, thunk: Callable[[], Awaitable[T]]) -> Result[T]:
        started = time.monotonic()
        try:
            data = await thunk()
        except httpx.TimeoutException:
            return Result(outcome=Outcome.TIMEOUT, latency_ms=_ms(started))
        except httpx.HTTPError as exc:
            return Result(outcome=Outcome.UPSTREAM_ERROR, raw_error=str(exc), latency_ms=_ms(started))
        except Exception as exc:  # a field the docs promised and did not deliver
            return Result(outcome=Outcome.UPSTREAM_ERROR, raw_error=repr(exc), latency_ms=_ms(started))
        return Result(outcome=Outcome.OK, data=data, latency_ms=_ms(started))
```

A bare `except Exception` is normally a smell. Here it is the contract: the
alternative is an unhandled exception reaching a consumer.

**(b) Respect the deadline.** `ctx.deadline.remaining_ms` is what is left of the
turn's ~2.5s budget across the *whole cascade*, not your call. Size your client
timeout from it — `ctx.deadline.shrink_to(800)` if you also want a per-call cap —
so a slow platform gets cut off rather than eating the budget of the two candidates
behind it. The cascade also bounds you externally (`_call(..., bound=True)` in
`core/routing/cascade.py`), but an adapter that manages its own timeout can report
`TIMEOUT` cleanly instead of being cancelled mid-flight.

**(c) `raw_error` is operator-only.** It goes to the audit log and never to a
consumer. You cannot leak it by accident even if you try — consumer text can only
be produced by the phrasebank, and the BSP send interface accepts a
`RenderedMessage` and nothing else. But put something useful in it: `raw_error` is
what the routing-trace page shows an operator at 2am.

**(d) Never read the secret store.** Credentials arrive in `ctx.credentials`,
resolved just-in-time by the router. Importing `services/secrets.py` from an
adapter breaks least privilege and makes the adapter untestable without a backend.
There is no test enforcing this one, so it is on you.

---

## Step 4 — Map upstream failures onto `Outcome`

This is the real engineering in an adapter. Everything else is data shuffling.

The eight outcomes are not severity levels — they are **instructions to the
router**, and the router's response to each is fixed (`CLAUDE.md` §4). Getting the
mapping wrong is how you get a double booking or a consumer waiting on a cascade
that already gave up.

| Your upstream says | Return | The router will |
|---|---|---|
| 200 with slots | `OK` | Use it, subject to the materiality re-confirm rule (§5.3). |
| 200 with an empty list; "fully booked" | `NO_AVAILABILITY` | Advance to the next merchant, then the next platform. |
| 200, but you are not confident it matches what was asked (fuzzy service match, wrong area, ambiguous venue) | `LOW_CONFIDENCE` | Hold as a fallback, keep looking. If it wins, it is offered with hedged copy and needs explicit confirmation. **Never auto-booked.** |
| Timeout, connect error, deadline exceeded | `TIMEOUT` | On a read: advance. **On `create_booking`: stop and reconcile — see step 5.** |
| 401, 403, expired token, revoked key | `AUTH_ERROR` | Advance, mark that merchant credential degraded, alert ops. Our problem, never the consumer's. |
| 429 | `RATE_LIMITED` | Advance and open a circuit breaker on that merchant with backoff. |
| 500, 502, 503, malformed body, a field the docs promised that is missing | `UPSTREAM_ERROR` | Advance. One retry only if the deadline allows, and never on create. |
| This platform cannot do this action at all | `UNSUPPORTED` | Advance only if another candidate can serve it; otherwise human handoff. Never synthesised into success. |

Three judgement calls you will actually face:

- **A 404 on search is `NO_AVAILABILITY`, not `UPSTREAM_ERROR`.** Some platforms
  404 an empty result set. Read your notes from step 1; if the docs are silent,
  probe the sandbox and write down what you found.
- **A 400 is almost always `UPSTREAM_ERROR`**, and is almost always *your* bug —
  a malformed request. It advances, so a consumer never sees it, which means it
  will hide from you unless you watch the audit log. Check the routing trace after
  your first live day.
- **`LOW_CONFIDENCE` is underused and shouldn't be.** If the platform's search is
  fuzzy — matched "haircut" to "Hair Cut & Blow Dry (Ladies)" at a venue in a
  different neighbourhood — that is exactly the case it exists for. The cost of
  using it is one extra confirmation. The cost of returning `OK` is booking
  somebody into the wrong salon.

---

## Step 5 — Idempotency and the reconciliation read

If `create_booking` times out, **you do not know whether the booking landed**.
Falling through to the next aggregator books the consumer twice, in two different
salons, and only one of them is on their calendar.

The rule (`CLAUDE.md` §5.1): on a create timeout the cascade does **not** advance.
It calls `find_by_idempotency_key` first, and:

| Reconciliation returns | Meaning | What happens |
|---|---|---|
| `OK` with a record | It landed | Treated as a successful booking. |
| `NO_AVAILABILITY` | It provably did not land | Cascade advances normally. |
| **anything else** | Unknown | **Human handoff.** Booking recorded as `UNCERTAIN`. Never retried elsewhere. |

Your job is to make that middle row reachable. Two ways, in order of preference:

1. **The platform honours an idempotency key.** Send
   `req.idempotency_key` in whatever header they specify, set
   `honours_idempotency=True`, and implement `find_by_idempotency_key` as a lookup
   by that key. A repeated create is then safe by construction.
2. **The platform does not.** Set `honours_idempotency=False` and implement
   `find_by_idempotency_key` as a *recent-bookings search* — list this merchant's
   bookings in a tight window around the slot, and match on consumer phone and
   slot start. It is coarser, and it will occasionally be inconclusive, and
   inconclusive means a human. That is the correct trade.

If the platform offers neither — no idempotency key and no way to list recent
bookings — then say so plainly in `INTEGRATION_NOTES.md`. `find_by_idempotency_key`
returns `UPSTREAM_ERROR`, every create timeout goes to a human, and the operator
should know that before they route volume to it.

The reconciliation read is deliberately **not** bounded by the turn deadline
(`cascade._call(..., bound=False)`). The consumer already has a holding message;
finding out whether we booked them matters more than the 2.5s budget.

---

## Step 6 — Register it

One import and one line in `mawjood/core/aggregators/registry.py`:

```python
from mawjood.core.aggregators.acme import AcmeAdapter

...
registry.register(AcmeAdapter())
```

If you are replacing a stub, delete its entry from `PARTNER_ADAPTERS` in
`partners.py` in the same commit — two adapters claiming one slug raises
`DuplicateAdapter` at startup, which is the intended behaviour and an unpleasant
way to find out.

That is the *only* file outside your adapter's own directory that changes.

---

## Step 7 — Credentials

Adapters never read the secret store. The chain is:

```
merchant_credentials.secret_ref  →  SecretStore.get(ref)  →  ctx.credentials
        "acme_marina_01"              env or managed store      Mapping[str, str]
```

With the env backend, `secret_ref = "acme_marina_01"` reads the environment
variable `MAWJOOD_MERCHANT_ACME_MARINA_01`, whose **value is a JSON object**:

```bash
MAWJOOD_MERCHANT_ACME_MARINA_01='{"api_key":"...","centre_id":"c-4471"}'
```

JSON rather than one variable per field because a merchant needs several values
together, and splitting them invites a half-configured merchant that fails at the
third call rather than at startup.

Platform-side identifiers that are *not* secret — centre ids, location ids, org
ids — belong in `merchant_credentials.external_ids` (JSONB), which arrives as
`ctx.merchant.external_ids`. Keep them out of the secret so an operator can see
which centre a row points at without decrypting anything.

Add your keys to `docs/KEYS.md` — name, purpose, owner, rotation steps — and to
the reserved section of `.env.example`. Never a value in either.

---

## Step 8 — The config rows

Two inserts, no code.

**`merchant_credentials`** — one row per venue on this platform:

| Column | Example |
|---|---|
| `platform_slug` | `acme` |
| `display_name` | `Marina Beauty Lounge` |
| `area` | `Dubai Marina` |
| `external_ids` | `{"centre_id": "c-4471"}` |
| `secret_ref` | `acme_marina_01` |
| `priority` | `0` — lower is tried first within the platform |
| `is_active` | `true` |

**`routing_config`** — where this platform sits in the priority list for a
category:

| Column | Example |
|---|---|
| `category` | `salon` |
| `platform_slug` | `acme` |
| `position` | `1` — lower is tried first; unique per (tenant, category) |
| `is_enabled` | `true` |

`platform_slug` is free text with no foreign key on purpose: the registry is the
authority on which slugs exist, this table only decides their order. A configured
slug with no registered adapter is logged as
`routing.configured_platform_has_no_adapter` and skipped — it must never become a
dead end for a consumer.

`tools/seed_dev.py` is where the local rows are written; add yours there so
`uv run python tools/seed_routing.py` sets up a working demo.

**Roll out at `position` last.** A new adapter goes on the *end* of the priority
list for its first week. If it misbehaves it is a fallback nobody reaches, and the
routing trace shows you exactly how it behaved before you promote it.

---

## Step 9 — Tests

`CLAUDE.md` §13: tests before implementation for anything in `core/aggregators/`.
Four things to write, in this order.

**(a) Add yourself to the conformance suite.** One line in
`tests/adapters/test_conformance.py`:

```python
ADAPTER_FACTORIES = [
    HappyFake,
    EmptyFake,
    TimeoutFake,
    InconclusiveTimeoutFake,
    FlakyFake,
    AuthFailFake,
    UnsupportedFake,
    RateLimitedFake,
    LowConfidenceFake,
    ZenotiAdapter,
    AcmeAdapter,  # <- here
]
```

That gets you, for free: every method returns a `Result`; nothing raises under
transport fault injection; capabilities are consistent with behaviour; the slug
is unique.

**(b) Transport tests with `respx`.** **No real network calls, ever** — the suite
blocks sockets, and a test that needs the network is a broken test. Mock the
routes you recorded in step 1:

```python
@respx.mock
async def test_search_maps_slots(...):
    respx.get(url__regex=r".*/availability").mock(
        return_value=httpx.Response(200, json=SANDBOX_AVAILABILITY_BODY)
    )
    result = await AcmeAdapter().search_availability(ctx, req)
    assert result.outcome is Outcome.OK
    assert result.data[0].venue_name == "Marina Beauty Lounge"
```

Use **real captured sandbox bodies** as fixtures, not bodies you wrote from the
schema. The gap between the two is where integrations break.

**(c) One test per row of your step-4 mapping.** 401 → `AUTH_ERROR`, 429 →
`RATE_LIMITED`, 500 → `UPSTREAM_ERROR`, empty list → `NO_AVAILABILITY`, timeout →
`TIMEOUT`, malformed body → `UPSTREAM_ERROR` (and not an exception). This is the
part the sandbox will not exercise for you.

**(d) The create-timeout path.** Make create time out, then assert what
`find_by_idempotency_key` returns in all three cases, and that exactly one booking
exists at the end. `tests/cascade/` has the pattern.

Then run the gates:

```bash
uv run pytest tests/adapters -q      # conformance
uv run pytest tests/cascade -q       # the invariant
uv run pytest -q                     # everything
uv run ruff check . && uv run mypy mawjood
```

`tests/adapters/test_plugin_claim.py` is the one to watch. It greps
`core/routing/`, `core/conversation/`, `api/` and `db/` for every known slug and
fails if any appears in executable code. **Add your slug to its `KNOWN_SLUGS`
tuple** — otherwise the guard silently does not cover you.

---

## Step 10 — Drive it by hand

```bash
uv run python tools/chat_sim.py
```

The terminal chat drives **the same core pipeline as the WhatsApp webhook** — same
handlers, different transport. It needs no WhatsApp credentials. Have a
conversation that reaches your platform, then read the routing trace in the ops
console (`/console/trace/<conversation_id>`) and check:

- your adapter appears as a candidate, with the latency you expect;
- the outcome recorded is the one you intended;
- the audit log says *why* the cascade advanced or stopped;
- what the consumer saw is phrasebank copy, and reads like a person wrote it.

Then break it deliberately: point `secret_ref` at a bad key and confirm the
consumer never learns anything went wrong. That is the invariant, checked by hand
on the one path the tests cannot fully simulate — your platform's real behaviour.

---

## The checklist

Copy this into the pull request.

**Before writing code**
- [ ] Live docs fetched and read; the eight questions from step 1 answered in `docs/INTEGRATION_NOTES.md`
- [ ] Direction of the API confirmed — a third party can genuinely create a booking
- [ ] Sandbox credentials working, at least one real response body captured
- [ ] Idempotency and post-timeout reconciliation understood, or their absence recorded

**The adapter**
- [ ] One new file/package under `mawjood/core/aggregators/<slug>/`
- [ ] All seven methods implemented; every one returns `Result`, none raise
- [ ] `capabilities` honest — `can_book=False` if it cannot actually book
- [ ] `honours_idempotency` set truthfully
- [ ] `ctx.deadline` respected; client timeout sized from `remaining_ms`
- [ ] `raw_error` populated with something an operator can act on
- [ ] `ctx.credentials` used; the secret store never imported

**Wiring**
- [ ] Registered in `registry.py`; stub removed from `partners.py` if replacing one
- [ ] Slug added to `KNOWN_SLUGS` in `tests/adapters/test_plugin_claim.py`
- [ ] Added to `ADAPTER_FACTORIES` in `tests/adapters/test_conformance.py`
- [ ] Credentials documented in `docs/KEYS.md`; placeholder in `.env.example`
- [ ] `merchant_credentials` and `routing_config` rows written, `position` last

**Proof**
- [ ] `respx` tests for every recorded endpoint, using captured bodies
- [ ] One test per outcome-mapping row
- [ ] Create-timeout reconciliation tested in all three branches
- [ ] Full suite, ruff and mypy green
- [ ] A conversation driven through `chat_sim.py` and its routing trace read
- [ ] Deliberate credential failure confirms the consumer sees a clean pivot

**Nothing else changed**
- [ ] `git diff --stat` touches only your adapter, the registry, tests, config and docs

---

## Five mistakes that will bite you

**1. Letting an exception escape.** The most common and the most damaging. It
turns a graceful pivot into a broken conversation. The conformance suite catches
it for the paths it fault-injects; it cannot catch a `KeyError` on a field only a
particular merchant omits. Wrap the whole call, not just the request.

**2. Returning `OK` for something a human still has to finish.** A "reservation
request" that a restaurant may decline is not a booking. If the platform's create
is a request rather than a confirmation, this is either `LOW_CONFIDENCE` — offered
with hedged copy and explicit confirmation — or a handoff. It is never `OK`.
Synthesising success is the worst failure this system can have.

**3. Advancing on a create timeout.** Reconcile first. Always. If you have added a
retry anywhere near `create_booking`, remove it.

**4. Writing copy.** Adapters produce no consumer-facing text, ever. Not an error
message, not a venue description passed through verbatim. All copy comes from the
phrasebank, keyed by `(intent, locale)`, and is scanned against a failure-language
blocklist that gates the build. If you need new wording, add a phrasebank entry
and let the scan check it.

**5. Trusting the sandbox's error behaviour.** Sandboxes are cheerful. They return
200s, they rarely 429, and they never have the outage your merchant will have at
6pm on a Thursday. Your `Outcome` mapping is tested by fault injection or it is
untested.

---

## If you get stuck

- **The contract** — `mawjood/core/aggregators/base.py`, and `CLAUDE.md` §4. It is
  pinned; do not change it without asking.
- **A minimal working adapter** — `FreshaAdapter` in
  `tests/adapters/test_plugin_claim.py`. Roughly 60 lines, no inheritance, and the
  test suite drives a real cascade through it.
- **A stub done properly** — `mawjood/core/aggregators/partners.py`.
- **A stub with a subtle blocker** — `mawjood/core/aggregators/deliveroo/`, which
  distinguishes "a partnership fixes this" from "a partnership does not".
- **Router behaviour per outcome** — `mawjood/core/routing/cascade.py`, and the
  table in `CLAUDE.md` §4.
- **What good looks like end to end** — `tests/cascade/`, the no-empty-shelves
  suite. If your change makes anything in there fail, the change is wrong.
