# CLAUDE.md — Mawjood

Read this before touching anything. It is written for an agent who has never seen the
conversation that started this repo. If something here conflicts with a request, say so
before you build.

---

## 1. What Mawjood is

Mawjood (موجود — "it's available") is a WhatsApp AI booking assistant for the UAE market.
One WhatsApp number is the single entry point. A consumer sends a casual message ("need a
haircut in Marina tomorrow evening"). Mawjood understands it, asks for whatever it still
needs, gets an explicit confirmation, books through one of several third-party aggregator
platforms, and sends back a confirmation.

- Repo: `mawjood`. Python package: `mawjood`.
- The assistant is named Mawjood, introduces itself as Mawjood, and holds that persona
  consistently in every consumer-facing message.
- v1 is **English only**. Arabic must be a data change later, never a refactor. See §6.

---

## 2. THE INVARIANT — no empty shelves

**A consumer is never told there are no vendors, no availability, no options, or that
something failed. Not once. Not in an edge case. Not when every upstream is down.**

The product is named after this property. Treat it as a **safety property, not a feature**.
It has its own test suite (`tests/cascade/`) and it gates the build.

How it holds:

1. An aggregator returns no slot, errors, times out, or comes back low-confidence →
   the router advances to the next candidate in that category's priority list.
2. The list is exhausted → the conversation escalates to the **human handoff queue**.
3. Either way the consumer sees a **graceful pivot** from the phrasebank
   ("Let me check a few more options for you") — never an apology for the platform being
   empty, never a dead end, never a mention of a vendor, an error, or a system.

### How the invariant is mechanically enforced

Two mechanisms, both of which must exist and stay green:

**(a) Copy is unforgeable.** Consumer-facing text can only be produced by the phrasebank.
The BSP send interface accepts a `RenderedMessage` and nothing else, and `RenderedMessage`
is constructible only by the phrasebank renderer. You cannot pass an f-string to a send
path. This is what makes (b) exhaustive rather than best-effort.

**(b) The blocklist scan.** A test enumerates every phrasebank entry across every
(intent, locale) pair and fails the build if any entry matches the failure-language
blocklist — roughly: *no availability / not available / nothing available / no options /
no vendors / no results / none found / couldn't find / unable to / can't help / sorry /
unfortunately / failed / error / try again later / down / unavailable / we don't have*.
The blocklist lives in the test suite, not in application code.

There is also a **test of the test**: planting a blocklisted phrase into a phrasebank
fixture must make the scan fail. A scan that cannot fail is not a scan.

Anything that produces consumer-visible text — LLM output included — is either rendered
through the phrasebank or passes the blocklist filter at runtime before it is sent. Free
LLM prose is never forwarded to a consumer unchecked.

---

## 3. Architecture (fixed — implement it, don't redesign it)

```
WhatsApp consumer
    ↓ (BSP webhook: 360dialog or Wati — abstracted behind one interface)
Mawjood core (FastAPI)
    ├── Conversation & NLU engine (OpenAI, GPT-4o-mini class)
    │     intent → slot filling → clarification → explicit confirmation
    ├── Routing engine
    │     category → ordered aggregator list (from a DB config table)
    ├── Fallback cascade  ← the invariant
    ├── Notification scheduler (reminders, follow-up, satisfaction 1–5)
    ├── Source attribution (wa.me/…?text=SRC12 and QR identifiers)
    └── Aggregator adapter layer  ← plugin interface, one adapter per platform
PostgreSQL: leads, conversations, messages, bookings, feedback, attribution, audit_log
```

Async everywhere on the request path.

---

## 4. The adapter contract — everything hangs off this

Every aggregator is an independent plugin implementing ONE interface. **Core logic
contains zero aggregator-specific branches.** Adding a platform later = writing one new
file and inserting one config row. There is a test that proves this.

**Critical rule: adapters do not raise into the router.** Every method returns a typed
outcome. The router decides what an outcome *means*; the adapter only reports. A conformance
test fault-injects the transport and asserts that adapters still return a `Result` rather
than propagating an exception.

```python
class Outcome(StrEnum):
    OK             = "ok"
    NO_AVAILABILITY = "no_availability"
    LOW_CONFIDENCE = "low_confidence"
    TIMEOUT        = "timeout"
    AUTH_ERROR     = "auth_error"
    RATE_LIMITED   = "rate_limited"
    UPSTREAM_ERROR = "upstream_error"
    UNSUPPORTED    = "unsupported"      # adapter cannot do this action at all

@dataclass(frozen=True, slots=True)
class Result(Generic[T]):
    outcome: Outcome
    data: T | None = None
    raw_error: str | None = None        # never shown to a consumer; audit only
    latency_ms: int = 0
    request_id: str | None = None

@dataclass(frozen=True, slots=True)
class CallContext:
    tenant_id: UUID
    conversation_id: UUID
    merchant: MerchantRef               # platform_slug + Mawjood merchant id + external ids
    credentials: Mapping[str, str]      # resolved just-in-time; never logged, never persisted
    deadline: Deadline                  # .remaining_ms / .expired
    correlation_id: str

class AggregatorAdapter(Protocol):
    slug: str
    categories: list[Category]
    capabilities: Capabilities          # can_reschedule, can_cancel, can_pick_staff, …

    async def search_availability(self, ctx: CallContext, req: AvailabilityRequest) -> Result[list[Slot]]
    async def create_booking(self, ctx: CallContext, req: BookingRequest) -> Result[BookingRef]
    async def get_booking(self, ctx: CallContext, ref: BookingRef) -> Result[BookingRecord]
    async def reschedule(self, ctx: CallContext, ref: BookingRef, slot: Slot) -> Result[BookingRef]
    async def cancel(self, ctx: CallContext, ref: BookingRef, reason: str | None) -> Result[None]
    async def health(self, ctx: CallContext) -> Result[None]
```

`BookingRequest` always carries an **idempotency key** derived from
`(conversation_id, category, slot_start, aggregator_slug)`.

Adapters are stateless singletons held in the registry. They never read the secret store
themselves — the router resolves credentials and hands them over in `CallContext`. That
keeps least-privilege and testability intact.

**Do not change this contract without asking.**

### Router policy per outcome

Every row also writes an `audit_log` entry: what was tried, what came back, why it
advanced, and what the consumer saw.

| Outcome | Router action |
|---|---|
| `OK` | Use it, subject to the materiality re-confirm rule (§5.3). |
| `NO_AVAILABILITY` | Advance: next merchant on this platform, then next platform. |
| `LOW_CONFIDENCE` | **Proposed, needs sign-off.** Hold as a fallback candidate and keep looking. If it is ultimately the best we have, surface it with hedged phrasebank copy and require explicit confirmation. Never auto-book on it. |
| `TIMEOUT` on a read | Advance. Record latency. |
| `TIMEOUT` on `create_booking` | **Stop. Do not advance.** Reconcile first — see §5.1. |
| `AUTH_ERROR` | Advance. Mark that merchant credential degraded and alert ops. This is our problem, never the consumer's. |
| `RATE_LIMITED` | Advance. Open a circuit breaker on that merchant with backoff. |
| `UPSTREAM_ERROR` | Advance. One tenacity retry only if the deadline budget allows, and never on `create_booking`. |
| `UNSUPPORTED` | Advance only if another candidate can serve this action. If no candidate in the list supports it → human handoff. Never synthesise success. |

---

## 5. Three failure modes handled deliberately

### 5.1 Double-booking on timeout

If `create_booking` times out you **do not know** whether the booking landed. Falling
through to the next aggregator books the consumer twice.

Rule: on timeout the cascade attempts a **reconciliation read** (`get_booking` by
idempotency key / recent-bookings lookup) before advancing. If reconciliation confirms the
booking → treat as `OK`. If it confirms no booking → advance. **If reconciliation is
inconclusive → human handoff. It does not silently retry elsewhere.**

Write the test before the code. The test asserts that across all fakes, exactly one
`create_booking` ever succeeded.

### 5.2 The 3-second budget vs. a slow cascade

Median consumer response must be **under 3s**, but a three-deep cascade can blow past it.

There is a **global deadline budget per turn (~2.5s soft)**. When it expires mid-cascade:
immediately emit a holding pivot from the phrasebank, continue the cascade in a background
task, and deliver the result as a follow-up message.

**The graceful pivot and the latency cover are the same mechanism.** That is the trick —
one code path serves both the invariant and the latency target. Use it; don't build two.

### 5.3 Failover that changes the offer

The consumer confirms a slot, the cascade fails over, and the next candidate's best offer
differs. **Decided: re-confirm on material change.**

A materiality rule with tolerances in config: a different venue, a start time shifted
beyond N minutes, or a price delta beyond X% requires one quick re-confirm before booking.
Inside tolerance, book and state plainly what was booked. This is a consent boundary — do
not widen the tolerances to make a test pass.

---

## 6. Two forward-compatibility decisions, made deliberately

**Tenant-ready from day one.** `tenant_id` on every core table, threaded through the
repository layer. The base repository requires a tenant scope; there is a test proving a
cross-tenant read returns nothing. **Do NOT build tenant management, billing, or an
onboarding UI** — just never paint us into a single-tenant schema.

**Language-ready.** All consumer-facing copy lives in a phrasebank keyed by
`(intent, locale)`, never inline in code. v1 ships `en` only. Adding `ar` must be a data
change. (This is also what makes the blocklist scan possible — see §2.)

---

## 7. Decisions already made — do not relitigate

| # | Decision |
|---|---|
| 1 | **Many merchants per platform.** Zenoti-style APIs are merchant-side: each salon has its own tenant/center IDs and key. A `merchant_credentials` table (tenant_id, platform, merchant_id, external ids, secret_ref) plus a `CredentialResolver`. The cascade iterates platform → merchants within that platform. |
| 2 | **Re-confirm on material change** (§5.3), tolerances in config. |
| 3 | **No money in v1** — no payment provider, no PCI scope. But `bookings` carries `price`, `currency`, `payment_status` so deposits later are a feature, not a migration. Upstream slots demanding a deposit route to handoff. |
| 4 | **Consent: notice, then opt-in before PII egress.** The first reply carries the consent wording and Mawjood keeps helping. Recorded affirmative consent is required before any personal data reaches an aggregator or a booking is created. Wording is drafted by us for the operator's legal review; store the exact text shown plus its version and timestamp. |
| 5 | **Deployment stays portable.** Plain Docker, 12-factor env, zero cloud-specific SDKs. `secrets.py` ships an env backend behind an interface with a documented seam for a managed store. Region is a config value. Target cloud is decided in Phase 4. |
| 6 | **LLM behind a provider-agnostic interface**, OpenAI as the v1 implementation, declared as a processor in `KEYS.md` with zero-retention / no-training terms documented. Changing provider or region is config, not a refactor. |
| 7 | **Handoff = queue + console reply + bot mute.** Handoff rows surface in the ops console; a human replies via the BSP from there and the bot goes silent on that conversation until released. Note this makes the console *read-only plus a reply path*, which stretches "read-only ops console" — accepted knowingly. |
| 8 | **No credentials exist yet.** Everything is built against fakes and `respx`. Live verification is Phase 4. Zenoti sandbox, BSP account, and Meta template approval should be applied for now — they are external lead time on the critical path. |

---

## 8. Stack — use exactly this. Ask before adding anything.

- Python 3.12, FastAPI, async on the whole request path
- PostgreSQL + SQLAlchemy 2.0 async + Alembic
- httpx (outbound), tenacity (retries)
- Pydantic v2 / pydantic-settings (all config from env)
- pytest + pytest-asyncio + respx. **No real network calls in tests, ever.** The suite
  blocks sockets; a test that needs the network is a broken test.
- structlog (JSON logs), Sentry (errors)
- Jinja2 + HTMX for the ops console — no separate frontend build, no npm
- Docker + docker-compose for local dev (app + postgres)
- ruff + mypy (**strict on `mawjood/core/`**)

Proposed in Phase 0, confirm before locking: `uv` as the package manager, `gitleaks` as
the pre-commit secret scanner, and a thin `Makefile` wrapping the commands below.

---

## 9. Repo layout

```
mawjood/
  main.py
  config.py                  # pydantic-settings, everything from env
  api/
    webhooks/whatsapp.py     # BSP-agnostic inbound handler
    console/                 # ops console routes
    health.py                # /healthz, /readyz
  core/
    conversation/            # state machine, slot filling, prompts, phrasebank
    routing/                 # router, cascade, policy, deadline budget
    aggregators/
      base.py                # the Protocol + result types
      registry.py            # discovery + capability declaration
      zenoti/
      deliveroo/
      fake/                  # test doubles: happy, empty, timeout, flaky, auth_fail
    notifications/
    attribution/
    handoff/
  db/
    models.py  repositories/  migrations/
  services/
    llm.py  bsp/  secrets.py
  observability/
    logging.py  audit.py  metrics.py
tests/
  transcripts/               # golden YAML conversations
  cascade/                   # the no-empty-shelves suite
  adapters/
tools/
  chat_sim.py                # local terminal chat — NO WhatsApp credentials needed
  seed_routing.py
  make_qr.py
docs/
  RUNBOOK.md  DEPLOY.md  ACCEPTANCE.md  KEYS.md  INTEGRATION_NOTES.md
```

---

## 10. Aggregator sequencing

- **Zenoti** (salons/spa) — first. Open developer sandbox and public API docs, and it covers
  the full lifecycle: guest lookup → services/staff → availability → reserve → confirm →
  retrieve → reschedule/cancel. Integrating it first validates the whole architecture end
  to end, and salon bookings are the cleanest fit for a guided WhatsApp flow.
- **Deliveroo** — second. Self-serve sandbox, but the Order API is merchant-side and full
  production access needs a partnership. Build the adapter; expect constraints.
- **OpenTable / Foodics / Booksy / Talabat / Careem** — partner-gated. Ship them as
  documented stubs that return `UNSUPPORTED` and register cleanly, so they drop into the
  live system the day credentials arrive.

> **Do not guess API endpoints from memory.** Before writing an adapter, fetch that
> platform's current public API docs and record in `docs/INTEGRATION_NOTES.md`: exact
> endpoints, auth model, pagination, rate limits, error shapes, and anything the docs are
> vague about. **If you cannot reach the docs, say so and stop. Do not invent endpoints.**

---

## 11. Compliance — non-negotiable

- **UAE PDPL**: consent capture on first inbound message, recorded with timestamp and the
  exact wording shown; data minimisation; configurable retention; a working deletion path
  that purges a consumer across **every table, including logs**.
- **Data residency**: deployable to a GCC, EU, or India region. **Never US-only.** No
  component may hard-assume a US region — there is a test for this.
- **Secrets** from env or a managed secret store only. Zero hardcoded keys. Ship
  `.env.example`. Pre-commit secret scan.
- Encryption in transit and at rest. Least-privilege DB user, separate from the migration
  user.
- Structured logs for every conversation, API call, and routing decision. 90-day retention.
- `audit_log` rows for **every routing decision**: what was tried, what came back, why it
  advanced, what the consumer saw.
- Daily DB backup, 30-day retention, plus a **restore drill actually performed** and
  documented in `RUNBOOK.md`.
- Consumer data is never used to train models and never leaves the declared providers.

---

## 12. Commands

Runner prefix assumes `uv` (Phase 0 decision — adjust if that changes).

| Purpose | Command |
|---|---|
| Run (local, full stack) | `docker compose up` |
| Run (app only) | `uv run uvicorn mawjood.main:app --reload` |
| Test | `uv run pytest -q` |
| Test — the invariant suite | `uv run pytest tests/cascade -q` |
| Test — adapter conformance | `uv run pytest tests/adapters -q` |
| Test — golden transcripts | `uv run pytest tests/transcripts -q` |
| Lint | `uv run ruff check . && uv run ruff format --check .` |
| Typecheck | `uv run mypy mawjood` |
| Migrate | `uv run alembic upgrade head` |
| New migration | `uv run alembic revision --autogenerate -m "<msg>"` |
| **Simulate a conversation (no WhatsApp creds)** | `uv run python tools/chat_sim.py` |
| Seed routing config | `uv run python tools/seed_routing.py` |
| Generate attribution QR | `uv run python tools/make_qr.py --source SRC12` |
| Secret scan | `uv run pre-commit run --all-files` |

`chat_sim.py` is the primary development loop. It must work with **no WhatsApp
credentials and no live aggregator access** — fakes only.

---

## 13. Working agreement

- Work the phases in `PLAN.md`. **At the end of each phase: stop, summarise what's done,
  list what was deferred, and give one command that demos it.**
- **Tests before implementation** for anything in `core/routing/` or `core/aggregators/`.
- Small, conventional commits. One logical unit each.
- **Ask before**: adding a dependency, changing the adapter contract, or altering the
  schema after Phase 1.
- If a requirement is ambiguous, **ask** — don't pick silently, and don't build both.
- **Never mark something done that isn't tested.**

## 14. Out of scope for v1 — do not build these

Tenant management, billing, operator onboarding UI, payments, Arabic copy, a JS frontend,
consumer accounts or login, vendor-side dashboards, recommendations/ranking beyond the
configured priority list, and any analytics beyond the audit trail.
