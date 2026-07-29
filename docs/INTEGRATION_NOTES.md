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

### Zenoti — Phase 2
Not started. Salons and spas, full lifecycle, built first because it validates the
whole architecture end to end.

### Deliveroo — Phase 3
Not started. Merchant-side Order API; full production access needs a partnership.

### OpenTable, Foodics, Booksy, Talabat, Careem — Phase 3
Partner-gated. Ship as documented stubs returning `UNSUPPORTED` that register
cleanly, so they drop in the day credentials arrive.
