# INTEGRATION NOTES

Per-aggregator API facts, **recorded from live documentation** at the time the
adapter was written.

> **Status: empty by design.** No adapter has been written yet.
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

## Platforms

### Zenoti — Phase 2
Not started. Salons and spas, full lifecycle, built first because it validates the
whole architecture end to end.

### Deliveroo — Phase 3
Not started. Merchant-side Order API; full production access needs a partnership.

### OpenTable, Foodics, Booksy, Talabat, Careem — Phase 3
Partner-gated. Ship as documented stubs returning `UNSUPPORTED` that register
cleanly, so they drop in the day credentials arrive.
