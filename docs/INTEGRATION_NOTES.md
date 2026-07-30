# INTEGRATION NOTES

Per-aggregator API facts, **recorded from live documentation** at the time the
adapter was written.

> **Status.** No *aggregator* adapter has been written yet. The WhatsApp inbound
> webhook landed in Phase 1 and is recorded below, including what could not be
> verified.
>
> The rule from `CLAUDE.md` section 10 is absolute: **do not guess API endpoints
> from memory.** Before writing an adapter, fetch that platform's current public
> API docs and record the findings here. If the docs cannot be reached, say so and
> stop — do not invent endpoints.

## What every entry must record

For each platform, before a line of adapter code:

- **Docs URL and the date fetched.** Everything below is a snapshot, not a
  permanent truth.
- **Auth model** — scheme, token lifetime, refresh, and whether credentials are
  per-merchant or per-platform. (Mawjood assumes per-merchant; see decision 1.)
- **Exact endpoints** for each contract method: availability search, create,
  retrieve, reschedule, cancel, health.
- **Idempotency support** — whether the platform honours an idempotency key on
  create. If it does not, say so loudly: the timeout-reconciliation path in
  `CLAUDE.md` section 5.1 exists precisely for platforms that do not.
- **Pagination** — style, page size limits, cursor stability.
- **Rate limits** — the numbers, the window, and the response shape when
  exceeded, which maps to `RATE_LIMITED`.
- **Error shapes** — the actual payloads, mapped onto `Outcome`. In particular:
  what does "no availability" look like versus a genuine error?
- **Reconciliation read** — how to determine after a timeout whether a booking
  actually landed. This is the single most important entry per platform.
- **Capabilities** — can it reschedule, cancel, pick staff? These populate
  `Capabilities` and drive `UNSUPPORTED`.
- **Anything the docs are vague about**, written plainly. Vagueness discovered
  now is a design input; vagueness discovered in production is an incident.

## Messaging providers (BSP)

### WhatsApp inbound webhook — Phase 1, **partially verified**

> **Documentation was not reachable.** Both
> `developers.facebook.com/docs/graph-api/webhooks/getting-started` and
> `docs.360dialog.com/docs/waba-messaging/webhooks` returned **HTTP 403** to
> automated fetching on 2026-07-29. What follows is corroborated from secondary
> sources, not read from the vendor's own documentation. **Confirm all of it
> against live docs and a real sandbox before production.**

**What was implemented** (`mawjood/services/bsp/`):

| Aspect | Implemented as | Confidence |
|---|---|---|
| Signature header | `X-Hub-Signature-256` | corroborated, unverified |
| Algorithm | HMAC-SHA256 over the **raw** request body, keyed with the app secret | corroborated, unverified |
| Header format | `sha256=<lowercase hex>` | corroborated, unverified |
| Comparison | `hmac.compare_digest` (constant time) | our choice, correct regardless |
| Subscription handshake | `GET` with `hub.mode` / `hub.verify_token` / `hub.challenge`, echo the challenge | corroborated, unverified |
| Inbound envelope | `entry[].changes[].value.messages[]`, sender in `messages[].from`, id in `messages[].id`, text in `messages[].text.body`, profile name in `value.contacts[].profile.name` | corroborated, unverified |
| Statuses | `value.statuses[]` arrives on the same endpoint and carries no message | corroborated, unverified |

**Risk containment.** The header name, prefix and digest are a `SignatureScheme`
dataclass, not constants, so correcting any of them is a one-line change. The
parser tolerates missing and null fields rather than raising, so an envelope that
differs from the assumption degrades to "no actionable messages" instead of a 500.

**Before production, confirm:** the exact header name and casing; whether
360dialog re-signs with its own secret or forwards Meta's signature; the retry
schedule and backoff for non-2xx responses; whether message ids are unique
across accounts (the idempotency index assumes unique per tenant + channel); and
the behaviour when a payload carries several messages for different consumers.

### Wati — **not implemented, deliberately**

`mawjood/services/bsp/wati.py` is a documented stub that refuses to parse. Wati's
envelope and auth scheme differ from Meta's, its documentation was not reachable,
and a parser invented from memory would silently drop or mangle real consumer
messages. The module docstring lists exactly what to confirm.

## Aggregator platforms

### Zenoti — **BLOCKED: documentation unreachable** (attempted 2026-07-29)

Zenoti was to be the first live adapter. It is **not implemented**, because
CLAUDE.md section 10 is absolute: if the docs cannot be reached, say so and stop.

| Route attempted | Result |
|---|---|
| `https://docs.zenoti.com/` | HTTP 403 |
| `https://docs.zenoti.com/docs/overview` | HTTP 403 |
| `https://docs.zenoti.com/docs/service-booking-apis` | HTTP 403 |
| `https://docs.zenoti.com/reference/retrieve-available-slots-for-a-service-booking` | HTTP 403 |
| `https://api.zenoti.com/` | HTTP 403 |
| direct `curl`, browser user-agent | connection blocked |

**What a web search did establish** — from documentation page titles only, and
therefore *not* sufficient to build against: the Service Booking APIs are used in
the order *create a service booking → retrieve available slots → reserve a slot →
confirm the booking → reschedule*. There is an endpoint that returns available
slots for a booking on a given day and falls forward to the next available day in
the same week.

**What remains unknown, and is required:** base URL, authentication header and
token format, every endpoint path, request and response shapes, pagination, rate
limits, the error taxonomy, whether "no availability" is distinguishable from a
failure, and — most importantly — **whether reserve/confirm honours an idempotency
key, and how to determine after a timeout whether a booking landed.** The
double-booking defence in section 5.1 depends on that last answer.

Knowing the sequence of operations is not knowing the API. Writing an adapter from
this would produce code that looks finished, passes fixtures written from the same
guesses, and fails the first time it meets the real service.

**Status:** `mawjood/core/aggregators/zenoti/` registers cleanly, declares no
capabilities, and returns `UNSUPPORTED` for every method — so the router advances
past it and the consumer never notices, which the all-aggregators-down demo shows.
The architecture is proven end to end against `core/aggregators/fake/`, which
exercises the identical contract.

**To unblock:** someone with browser access records the answers above here, then
replaces the stub. Nothing outside that one file changes.

---

## Deliveroo — Phase 3: implemented to the contract, blocked on two things

Re-verified 2026-07-30. Both blockers are recorded because they are *different
kinds of blocker*, and treating them the same would waste somebody's quarter.

### Blocker 1 — the documentation is unreachable

| Attempt | Result |
|---|---|
| `https://api-docs.deliveroo.com/docs/introduction` | HTTP 403 (2026-07-30) |
| `https://developers.deliveroo.com/docs` | HTTP 403 (2026-07-30) |
| `https://api-docs.deliveroo.com/ (direct curl, browser UA)` | connection refused |
| `https://api-docs.deliveroo.com/v2.0/reference (direct curl)` | connection refused |
| `https://developers.deliveroo.com/ (direct curl)` | connection refused |

Per CLAUDE.md section 10 nothing was written from memory.
`mawjood/core/aggregators/deliveroo/` names no endpoint, no host and no header,
and a test asserts that.

### Blocker 2 — the Order API is not shaped for what an outside assistant needs

This is the more important finding, and it does **not** clear with a partnership.

Deliveroo's Order API is **merchant-side**: it is the interface a restaurant's
point-of-sale system uses to *receive* orders that Deliveroo has already taken
from a consumer through Deliveroo's own apps — to accept them, mark them ready,
reconcile them. Orders flow *into* it.

Mawjood is an outside assistant acting for a consumer, so the direction it needs
— *place* an order on someone's behalf — is not what that API does. A partnership
would grant access to the merchant side of a restaurant we do not operate.

So the adapter records support **per method**, distinguishing "we lack the
contract" from "this API does not do this":

| Method | Support | Clears with a partnership? | Why |
|---|---|---|---|
| `search_availability` | `blocked` | yes | Restaurant open/closed and delivery-zone data plausibly exists, but the contract is unknown and the docs are unreachable. |
| `create_booking` | `not_what_this_api_is_for` | **no** | The Order API receives orders Deliveroo has already taken from a consumer through its own apps. It does not place them. An outside assistant cannot originate an order through it, with or without a partnership. |
| `get_booking` | `not_what_this_api_is_for` | **no** | A merchant can read orders it received. Mawjood never originates one, so there is nothing of ours to read back. |
| `find_by_idempotency_key` | `not_what_this_api_is_for` | **no** | Reconciliation presupposes a create we performed. See create_booking. |
| `reschedule` | `not_what_this_api_is_for` | **no** | Food delivery has no reschedule in the sense a booking does. |
| `cancel` | `blocked` | yes | Merchant-side cancellation exists, but only for orders the merchant owns. Unusable until the two above resolve. |
| `health` | `blocked` | yes | Needs a base URL and a credential; both unknown. |

**The consequence for planning:** pursuing a Deliveroo partnership will not
unlock consumer food ordering. If food delivery matters for v1, the question to
answer first is whether *any* aggregator in this market exposes consumer-side
ordering to a third party — see the Talabat checklist below, which asks exactly
that before any integration work is funded.

### Status

`DeliverooAdapter` implements every method of the contract with correct
signatures and typed outcomes, never raises into the router, and returns
`UNSUPPORTED` throughout. The router advances past it; if nothing else can serve,
the conversation reaches a human. No consumer ever learns any of this.

### To activate

1. Obtain readable API documentation (browser access, or a partner portal login).
2. Record base URL, auth model, endpoints, pagination, rate limits and error
   shapes here.
3. Confirm whether any consumer-side ordering capability exists for third
   parties. If not, mark this platform permanently `NOT_WHAT_THIS_API_IS_FOR`
   for `create_booking` and re-scope it to search-only or drop it.
4. Confirm idempotency on create and the post-timeout reconciliation path —
   CLAUDE.md section 5.1 depends on it.

Nothing outside `mawjood/core/aggregators/deliveroo/` changes.

---

## Partner-gated platforms — activation checklists

All five register cleanly, declare no capabilities, and return `UNSUPPORTED`.
None ever synthesises a success. The checklists below live in
`mawjood/core/aggregators/partners.py` as data and this section is generated from
them, so the two cannot drift.


### opentable

Restaurant reservations. Closest fit to Mawjood's flow of the five.

- **Blocker:** affiliate approval required — the reservation API is gated behind an affiliate agreement
- **Categories:** restaurant
- **Docs:** OpenTable partner/affiliate portal (requires an approved account)

**To activate:**

1. Apply to the OpenTable affiliate or partner programme as the operating entity.
2. Obtain the restaurant-availability and reservation API contract; record endpoints, auth, pagination, rate limits and error shapes in docs/INTEGRATION_NOTES.md.
3. Confirm whether reservations can be created on a consumer's behalf by a third party, or only deep-linked. If only deep-linked, this platform can never complete a booking and should be re-scoped to search-only.
4. Confirm idempotency on create, and how to determine after a timeout whether a reservation landed (CLAUDE.md 5.1 depends on this).
5. Confirm UAE market coverage — the UAE presence is thinner than the US.


### foodics

GCC restaurant POS. Merchant-side, like Deliveroo — check direction first.

- **Blocker:** merchant app registration required — needs an app registered against each merchant's Foodics account
- **Categories:** restaurant, food_delivery
- **Docs:** Foodics developer portal (requires a developer account)

**To activate:**

1. Register a developer account and create an application.
2. Establish the per-merchant authorisation flow — Foodics is merchant-side, so each venue authorises us separately. Confirm this fits the merchant_credentials model (it should: that is what decision 1 is for).
3. Determine whether the API exposes *reservations* or only orders and menu management. If orders only, the same constraint as Deliveroo applies and an outside assistant cannot originate one.
4. Record endpoints, auth, rate limits and error shapes.
5. Confirm idempotency on create and the post-timeout reconciliation path.


### booksy

Salon and barber bookings. Second-best salon fit after Zenoti.

- **Blocker:** partner API licence required — no public booking API; access is by commercial agreement
- **Categories:** salon, spa
- **Docs:** none public — obtained under agreement

**To activate:**

1. Contact Booksy partnerships as the operating entity and establish whether a third-party booking API exists at all. This is the open question; if the answer is no, close this adapter out rather than leaving it hopeful.
2. If yes, obtain the contract and record it in docs/INTEGRATION_NOTES.md.
3. Confirm UAE coverage and how venues map to merchant credentials.
4. Confirm idempotency on create and the post-timeout reconciliation path.


### talabat

UAE food delivery. Expect the Deliveroo constraint to repeat here.

- **Blocker:** partnership required — no public consumer ordering API
- **Categories:** food_delivery
- **Docs:** none public — Delivery Hero partner channels

**To activate:**

1. Approach Talabat/Delivery Hero partnerships as the operating entity.
2. Establish first, before anything else, whether a consumer-side ordering API exists for third parties. Deliveroo's does not; assume the same until shown otherwise, and do not spend on integration work before this answer.
3. If ordering is merchant-side only, mark this platform not_what_this_api_is_for, as core/aggregators/deliveroo does.
4. Otherwise record the contract and confirm idempotency on create.


### careem

Rides and food, UAE. The ride category has no other candidate.

- **Blocker:** partnership required — Everything App APIs are partner-gated
- **Categories:** ride, food_delivery
- **Docs:** none public — Careem partner channels

**To activate:**

1. Approach Careem partnerships as the operating entity.
2. Scope which vertical is in play. Rides and food are different products with different contracts; do not assume one agreement covers both.
3. For rides, confirm whether a booking can be made on a consumer's behalf or only deep-linked into the Careem app.
4. Note that ride is the only category with no second candidate configured, so until this exists every ride request goes to a human. That is correct behaviour, not a bug, but it is worth knowing before launch.
5. Record the contract and confirm idempotency on create.


---

## WhatsApp message templates — Phase 3

**Status: nothing submitted, therefore nothing approved.** This is the single
constraint that most limits what Mawjood can do today, and it is external lead
time rather than engineering.

### The constraint

WhatsApp permits free-form business messages only inside a **24-hour session
window**, measured from the consumer's last inbound message. Outside that window
a business may send only a **pre-approved message template**, approved by Meta
per template per language.

Approval requires the operator's Meta Business Manager and a WhatsApp Business
Account. Neither exists yet (see the Phase 4 prerequisites in `PLAN.md`), so no
template has been submitted.

### What that costs, concretely

Most scheduled messages land **outside** the window. A booking is usually made in
one conversation; the reminder fires hours later, by which time the consumer has
been quiet. So today:

| Notification | Typical timing | Can it be sent? |
|---|---|---|
| Reminder (3h before) | usually outside the window | **No** — blocked |
| Follow-up (2h after) | usually outside | **No** — blocked |
| Satisfaction (24h after) | always outside | **No** — blocked |

Anything that happens to fall inside the window sends normally as phrasebank
copy.

### A promise the product currently cannot keep

`booking.confirmed` says *"I'll send you a reminder beforehand."* For most
bookings that reminder cannot currently be delivered. This is recorded here
rather than quietly worked around, and there are three ways to resolve it, in
descending order of preference:

1. **Get the templates approved.** The only real fix. It is on the critical path
   and should be filed as soon as the Business Manager account exists.
2. **Soften the copy** until approval lands, so Mawjood does not promise what it
   cannot do.
3. Accept the gap knowingly, on the basis that a consumer who is not messaged
   suffers less than one who is misled.

Left as a decision for the operator, because it is a product call rather than a
technical one.

### How the code handles it

`mawjood/core/notifications/` treats this as a first-class outcome, not an error:

- `TemplateRegistry` declares all three templates with status `not_submitted`.
  The names below are **proposed, not registered** — nothing has been submitted,
  so nothing can be confirmed.
- The scheduler returns `Verdict.BLOCKED_NO_APPROVED_TEMPLATE`, writes a
  `notification.blocked` audit row with the reason, and **does not write to
  `messages`** — `messages` records what a consumer actually received, and a
  blocked send is not that.
- The ops console shows the backlog at `/console/notifications`.

| Proposed template name | Mirrors phrasebank key | Variables |
|---|---|---|
| `booking_reminder` | `notify.reminder` | venue, time |
| `booking_follow_up` | `notify.follow_up` | venue |
| `booking_satisfaction` | `notify.satisfaction` | venue |

### To unblock

1. Create the Meta Business Manager account and WhatsApp Business Account.
2. Submit the three templates above, in English, with bodies matching the
   phrasebank entries in `mawjood/core/conversation/phrases/en.toml`. They must
   say the same thing — a consumer inside the window and one outside it should
   not receive different messages.
3. Flip the status in `_V1_TEMPLATES` to `approved`. No other code changes; a
   test already proves the out-of-window path works once a template is approved.
4. Confirm the window length against Meta's current policy and adjust
   `MAWJOOD_WHATSAPP_SESSION_WINDOW_HOURS` if it has changed. It is configuration
   for exactly this reason.

**Verification status:** the 24-hour window and the template requirement are
stated from widely-corroborated secondary knowledge of WhatsApp Business policy.
Meta's own documentation (`developers.facebook.com`) returns HTTP 403 to every
automated fetch from this environment, so — as with the webhook signature scheme
above — this is **corroborated but not confirmed against the vendor's own docs**.
The window length is configuration and the template gate is a data flip, so
correcting either is a config change rather than a rewrite.
