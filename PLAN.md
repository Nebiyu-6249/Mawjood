# PLAN.md — Mawjood build plan

Five phases. Read `CLAUDE.md` first — it holds the invariant, the adapter contract, and the
decisions that are already settled.

**Phase discipline:** at the end of each phase, stop. Summarise what's done, list what was
deferred and why, and give one command that demos it. Do not start the next phase without
a go.

**Test-first is mandatory** for anything in `core/routing/` and `core/aggregators/`.

---

## Start these now — external lead time, not code

We hold **no credentials**. Three applications have multi-day-to-multi-week turnarounds and
sit on the critical path. They should be filed in parallel with Phase 0, by a human:

1. **Meta / WhatsApp message template approval** — required for any reminder, follow-up, or
   satisfaction message outside WhatsApp's 24-hour session window. This gates Phase 3's
   notification scheduler from *shipping*, not from being *built*. Longest pole.
2. **WhatsApp BSP account + number** (360dialog or Wati) — gates the first real end-to-end
   round trip.
3. **Zenoti developer sandbox**, and **Deliveroo sandbox** behind it — gates live adapter
   verification.

Nothing in Phases 0–3 blocks on these. Everything is built against fakes and `respx`, and
live verification is explicitly Phase 4. But if these are not filed early, Phase 4 waits on
paperwork instead of engineering.

---

## Phase 0 — Scaffold ✅ complete

Get a green, boring skeleton that boots and enforces its own rules.

**Deliverables**
- Repo skeleton exactly as laid out in `CLAUDE.md` §9, package `mawjood`, Python 3.12.
- `pyproject.toml` with the pinned stack. Package manager: `uv` (confirm).
- `ruff` + `mypy` configured, **mypy strict on `mawjood/core/`**.
- `pytest` + `pytest-asyncio` + `respx`, with **socket access blocked in the test suite** so
  a real network call fails loudly rather than passing quietly.
- `Dockerfile` + `docker-compose.yml` (app + postgres), no cloud-specific anything.
- `config.py` on pydantic-settings: everything from env, including `REGION` and
  `TIMEZONE` (default `Asia/Dubai`) as values, never assumptions.
- `.env.example` with every key, no real values.
- Pre-commit with a secret scanner (`gitleaks`, confirm).
- `observability/logging.py`: structlog JSON logging wired into FastAPI.
- `api/health.py`: `/healthz` (liveness, no dependencies) and `/readyz` (checks DB).
- CI workflow: lint + typecheck + test + secret scan.

**Definition of done** — all verifiable, all automated:
- `docker compose up` boots app and postgres; `/healthz` returns 200; `/readyz` reports DB
  reachability honestly (red when postgres is stopped).
- `ruff` and `mypy` clean.
- `pytest` green, including a test that asserts outbound sockets are blocked.
- CI green on the branch.
- A planted fake key is caught by the pre-commit scan.

**Demo:** `docker compose up -d && curl -s localhost:8000/readyz`

---

## Phase 1 — Foundations ✅ complete

The load-bearing phase: schema, the adapter contract, the phrasebank, and the machinery
that makes the invariant testable. Everything after this is built on top, so the contract
freeze happens here.

**Deliverables**
- **Schema + first Alembic migration.** `tenant_id` on every core table.
  Core: `tenants`, `consumers` (leads), `conversations`, `messages`, `bookings`, `feedback`,
  `attribution`, `audit_log`, `consents`.
  Supporting: `platforms`, `merchants`, `merchant_credentials`, `routing_config`
  (category → ordered platform list), `handoff_queue`, `scheduled_notifications`.
  `bookings` carries `price`, `currency`, `payment_status` (decision 3 — schema-ready, no
  payment flow) and the `idempotency_key`.
  `consents` stores the **exact wording shown**, its version, and a timestamp.
- **Repository layer** with tenant scoping enforced at the base class — a repository
  cannot be constructed or queried without a tenant scope.
- **Phrasebank**: `(intent, locale)` → template, `en` seeded. Plus the `RenderedMessage`
  type that only the phrasebank can construct, and a send interface that accepts nothing
  else (`CLAUDE.md` §2a).
- **The no-empty-shelves copy scan** (`tests/cascade/`): every phrasebank entry across every
  locale checked against the failure-language blocklist — *plus the test of the test*, which
  plants a bad phrase and asserts the scan fails.
- **Adapter contract** in `core/aggregators/base.py`: `Outcome`, `Result`, `CallContext`,
  `MerchantRef`, `Deadline`, `Slot`, request/response types, `Capabilities`, `Category`,
  and the `AggregatorAdapter` Protocol — exactly as pinned in `CLAUDE.md` §4.
- **Registry**: discovery + capability declaration.
- **Fake adapters**: `happy`, `empty`, `timeout`, `flaky`, `auth_fail`, `unsupported`.
- **Adapter conformance suite** every registered adapter must pass, including fault
  injection proving **no adapter ever raises into the router**.
- `services/secrets.py` interface + env backend; `CredentialResolver` over
  `merchant_credentials`.
- `observability/audit.py` (the `audit_log` writer) and `metrics.py`.

**Definition of done**
- Migration applies and rolls back cleanly on a fresh database.
- Cross-tenant isolation test: tenant A cannot read tenant B's rows through any repository.
- Copy scan passes, **and provably fails** when a blocklisted phrase is planted.
- All six fakes pass the conformance suite; fault injection yields `Result`, never an
  exception.
- `mypy --strict` clean on `mawjood/core/`.
- Credentials never appear in logs — asserted by a test over captured log output.

**Demo:** `docker compose run --rm app pytest tests/cascade tests/adapters -q`

> **Schema and adapter contract freeze at the end of this phase.** Changes after this
> require asking first.

---

## Phase 2 — Conversation + routing + Zenoti ⚠️ complete except Zenoti

> **Zenoti is blocked and not implemented.** `docs.zenoti.com` returns HTTP 403 on
> every path and direct curl is blocked, so per section 10 the adapter is a
> documented stub returning `UNSUPPORTED` rather than guessed endpoints. Everything
> else in this phase shipped; the architecture is proven end to end against
> `core/aggregators/fake/`, which exercises the identical contract. See
> `docs/INTEGRATION_NOTES.md` for exactly what must be recorded to unblock it.
>
> Consequence for Phase 3: **there is still no live aggregator.** Either Zenoti
> gets unblocked by someone with browser access, or Deliveroo becomes the first
> real integration.


The end-to-end spine. This is where the invariant stops being a doc and starts being
runtime behaviour.

**Write the routing and cascade tests before the routing and cascade code.** Explicitly
including the double-booking test (`CLAUDE.md` §5.1) — that test exists before that code.

**Deliverables**
- **Conversation state machine**: intent → slot filling → clarification → **explicit
  confirmation** → book. Prompts versioned in files, not inline strings.
- `services/llm.py`: provider-agnostic interface, OpenAI implementation, strict Pydantic
  output schemas, all tests mocked through `respx`. LLM prose never reaches a consumer
  unfiltered (`CLAUDE.md` §2).
- **Routing engine**: category → ordered platform list from `routing_config`, iterating
  platform → merchants within it.
- **Fallback cascade** implementing the full outcome policy table (`CLAUDE.md` §4), with an
  `audit_log` row per decision recording what was tried, what came back, why it advanced,
  and what the consumer saw.
- **Deadline budget** (~2.5s soft): holding pivot from the phrasebank, cascade continues in
  a background task, result delivered as a follow-up message. One mechanism serving both the
  pivot and the latency cover.
- **Idempotency keys** on every `create_booking`, plus the timeout → reconciliation →
  handoff path.
- **Materiality re-confirm rule** with tolerances in config (`CLAUDE.md` §5.3).
- **Consent flow**: notice in the first reply, affirmative consent recorded before any PII
  reaches an aggregator or a booking is created. Draft wording marked for legal review.
- **Zenoti adapter** — *preceded by fetching the live public API docs and writing*
  `docs/INTEGRATION_NOTES.md` (endpoints, auth, pagination, rate limits, error shapes,
  ambiguities). **If the docs cannot be reached: stop and report. Do not invent endpoints.**
- **Golden transcript tests** (`tests/transcripts/`, YAML).
- `tools/chat_sim.py` — terminal chat, no WhatsApp credentials, fakes only.

**Definition of done** — the cascade suite covers, and passes:
- **Every upstream down** → human handoff, graceful pivot, zero blocklist words reach the
  consumer.
- **Timeout on `create_booking`** → reconciliation attempted → **exactly one successful
  create across all adapters**, asserted → handoff if reconciliation is inconclusive.
- **Budget expiry mid-cascade** → pivot emitted inside the budget, cascade continues in
  background, follow-up delivered.
- **`UNSUPPORTED`** → advances if another candidate can serve it, otherwise handoff. Never
  a synthesised success.
- **Failover changing the offer** → re-confirm outside tolerance, book silently inside it.
- Golden transcripts pass, including a full multi-turn happy path.
- Zenoti adapter passes the conformance suite against `respx` fixtures built from documented
  shapes; `INTEGRATION_NOTES.md` written.
- Median turn latency under 3s in a local benchmark test.

**Demo:** `uv run python tools/chat_sim.py` — and `--scenario all-down` to watch the
invariant hold with every upstream failing.

---

## Phase 3 — Second aggregator + notifications + console ⬜ not started

Prove the plugin claim with a real second platform, then build the operational surface.

**Deliverables**
- **Deliveroo adapter** — docs fetched first, `INTEGRATION_NOTES.md` updated. Merchant-side
  Order API; expect constraints and document them rather than working around them.
- **Partner-gated stubs**: OpenTable, Foodics, Booksy, Talabat, Careem — documented stubs
  returning `UNSUPPORTED`, registering cleanly, passing conformance.
- **BSP layer**: 360dialog and Wati behind one interface. Webhook signature verification,
  idempotent inbound handling (duplicate webhook delivery must not double-process),
  delivery receipts.
- **Notification scheduler**: reminders, follow-up, satisfaction 1–5. Explicit handling of
  WhatsApp's 24-hour session window and a template registry abstraction. Templates are not
  approved yet — the scheduler must degrade honestly, never pretend a send happened.
- **Attribution**: `wa.me/…?text=SRC12` parsing, QR generation (`tools/make_qr.py`),
  attribution rows wired end to end.
- **Handoff**: queue + ops console reply path + bot mute/release semantics + an alerting
  hook.
- **Ops console** (Jinja2 + HTMX, no npm): conversations, bookings, routing decisions /
  audit trail, handoff queue — read-only except the handoff reply. Authenticated.
- `tools/seed_routing.py`.

**Definition of done**
- **The plugin claim, proven**: a test registers a brand-new fake platform and routes to it
  by adding one file and one config row, touching zero core files. A grep-style test asserts
  no aggregator slug appears anywhere in `core/routing/` or `core/conversation/`.
- Every partner-gated stub returns `UNSUPPORTED` and routes to handoff — never a fake
  success.
- Scheduler tests with a frozen clock fire reminder / follow-up / satisfaction at the right
  offsets, and **never send outside the 24-hour window without an approved template**.
- Both BSP implementations pass one shared webhook conformance suite, including replayed
  duplicate deliveries.
- Handoff round trip: cascade exhausted → queue row → console reply → bot muted → released.
- Attribution round trip: `wa.me` link → conversation → attribution row.
- Console renders from seeded data and performs no writes other than handoff replies.

**Demo:** `docker compose up` then the console at `localhost:8000/console` with seeded data
showing a completed booking, its full routing audit trail, and a live handoff.

---

## Phase 4 — Hardening + launch readiness ⬜ not started

Compliance made real, performance proven, and live credentials wired when they land.

**Deliverables**
- **PDPL deletion path**: purge a consumer across every table *including logs*. Configurable
  retention plus the retention job.
- **Backups**: daily DB backup, 30-day retention, and a **restore drill actually performed
  and timed**, written up in `RUNBOOK.md`.
- Sentry wired. 90-day log retention configured. **Least-privilege DB user** split from the
  migration user.
- **Load and latency test** proving p50 < 3s, with a p95 target recorded.
- **Chaos test**: the cascade under sustained fault injection across all adapters, asserting
  the invariant holds.
- **Security pass**: dependency audit, secret scan in CI, TLS and at-rest encryption
  documented, and a test asserting no US-region default anywhere.
- **Deployment target chosen** (deferred here deliberately) and `DEPLOY.md` written against
  it — GCC, EU, or India region, never US-only.
- **Docs complete**: `RUNBOOK.md`, `DEPLOY.md`, `ACCEPTANCE.md`, `KEYS.md`,
  `INTEGRATION_NOTES.md`. `KEYS.md` declares every processor, including the LLM provider,
  with no-training and retention terms.
- **Live credential wiring** as accounts arrive: Zenoti sandbox round trip, BSP end-to-end
  WhatsApp exchange, approved templates attached to the scheduler.

**Definition of done**
- Deletion test proves **zero residue**: a scan across every table and the log store for the
  identifier returns nothing.
- Restore drill executed end to end, with the elapsed time recorded in `RUNBOOK.md`.
- p50 < 3s verified under load.
- CI enforces lint + typecheck + tests + secret scan + the blocklist scan, and the build
  fails if any of them fail.
- Region config swaps cleanly with no US default anywhere.
- `ACCEPTANCE.md` checklist fully green, executed against a deployed environment.
- Live verification either done, or explicitly listed as blocked on a named credential.

**Demo:** `uv run python tools/erase_consumer.py --phone +9715XXXXXXX` followed by the
residue test proving nothing remains — the invariant's compliance twin.

---

## Additions proposed, not yet approved

Flagged rather than assumed, per the working agreement:

- `uv` (package manager), `gitleaks` (pre-commit secret scan), and a thin `Makefile` — all
  Phase 0.
- `tools/erase_consumer.py` — a tool file beyond the three named in the brief, needed to
  make the PDPL deletion path operable. Phase 4.
- A test-time socket blocker (e.g. `pytest-socket`) to enforce "no real network calls,
  ever" mechanically rather than by convention. Phase 0.
- `freezegun` or equivalent for the scheduler's frozen-clock tests. Phase 3.

## Carried open question

`LOW_CONFIDENCE` semantics are **proposed, not settled, and now implemented as
proposed** (`CLAUDE.md` §4): hold as a
fallback candidate, keep looking, surface only with hedged copy and explicit confirmation,
never auto-book. The Phase 2 cascade tests and `core/routing/policy.py` encode
this reading, so changing it is now a real edit rather than a note.
