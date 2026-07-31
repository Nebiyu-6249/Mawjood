# Acceptance test plan and results

**Audience:** someone evaluating Mawjood who was not involved in building it — a
prospective operator, a technical buyer, a diligence reviewer.

**What this is:** one row per capability. How it was tested, what happened, and
where the evidence is. Every "how tested" is a command you can run.

**Date:** 2026-07-30 · **Suite:** 981 passed, 2 skipped · ruff and mypy clean

---

## How to read the result column

| | Meaning |
|---|---|
| **PASS** | Implemented, tested, and the test is meaningful |
| **PASS (fakes)** | Implemented and tested end to end, but against test doubles rather than a live third party — because no live credential exists yet |
| **PARTIAL** | Some of the capability is real; the rest is stated in the row |
| **BLOCKED** | Not implemented, and the reason is external |

**Nothing here is PASS on the strength of a live third-party integration,**
because Mawjood holds no third-party credentials. That is the most important
caveat in this document, so it is repeated per row rather than buried once at the
top. See [The three gaps](#the-three-gaps).

---

## Capability matrix

### Channel

| | |
|---|---|
| **Requirement** | One WhatsApp number as the single entry point |
| **How tested** | `pytest tests/test_webhook.py tests/test_bsp_conformance.py tests/test_transport_parity.py` — signature verification, envelope parsing, delivery receipts, duplicate delivery, malformed payloads |
| **Result** | **PASS (fakes)** |
| **Evidence** | 41 conformance tests. The webhook accepts a Meta/360dialog envelope, verifies HMAC-SHA256 over the raw body, and is idempotent against redelivery |
| **Caveat** | No number is provisioned. The payload shape is corroborated but **not confirmed against vendor documentation** — Meta's and 360dialog's docs return HTTP 403 to automated fetching. The scheme is a `SignatureScheme` dataclass, so correcting it is one line. See `INTEGRATION_NOTES.md` |

### Language

| | |
|---|---|
| **Requirement** | English in v1; Arabic must be a data change, not a refactor |
| **How tested** | `pytest tests/cascade/test_no_empty_shelves.py` — every phrase in every locale, plus a structural test that no consumer-facing string is built in code |
| **Result** | **PASS** |
| **Evidence** | All copy lives in `mawjood/core/conversation/phrases/en.toml`, keyed by `(key, locale)`. `RenderedMessage` is constructible only by the phrasebank and the send interface accepts nothing else, so an f-string cannot reach a consumer |
| **Caveat** | Arabic is a new `ar.toml` with the same keys. No RTL work is needed — WhatsApp handles direction |

### Conversation flow

| | |
|---|---|
| **Requirement** | Intent → slot filling → clarification → explicit confirmation → book |
| **How tested** | `pytest tests/transcripts tests/test_conversation_engine.py` — 10 golden YAML transcripts against a deterministic LLM, including named awkward cases |
| **Result** | **PASS** |
| **Evidence** | The LLM returns a structured `Understanding`; every transition is an explicit case in `core/conversation/states.py`. A model cannot confirm a booking — the only route into `BOOKING` requires `confirmation == "yes"`, recorded consent, and a pending offer |
| **Caveat** | Tested against the deterministic understudy, not OpenAI. Both implement the same `LLMProvider` protocol and return the same schema. Real-model behaviour on ambiguous phrasing is unmeasured |

### Booking actions

| | |
|---|---|
| **Requirement** | Search, book, retrieve, reschedule, cancel |
| **How tested** | `pytest tests/adapters` — every registered adapter against one conformance suite, including fault injection |
| **Result** | **PASS (fakes)** / **BLOCKED** for live platforms |
| **Evidence** | 313 adapter tests. Every adapter returns a typed `Result` and **never raises into the router**, proven by injecting exceptions into the transport. The double-booking defence is tested: on a create timeout the cascade reconciles rather than advancing, and exactly one create ever succeeds |
| **Caveat** | No live platform is connected. See [The three gaps](#the-three-gaps) |

### Fallback cascade — the invariant

| | |
|---|---|
| **Requirement** | A consumer is never told there are no vendors, no availability, or that something failed |
| **How tested** | `pytest tests/cascade` — the blocklist scan over every phrase in every locale, a test-of-the-test that plants a blocklisted phrase and asserts the scan fails, and end-to-end runs with every candidate failing |
| **Result** | **PASS** |
| **Evidence** | 92 tests. With four platforms failing in four different ways (empty, auth error, timeout, unsupported), the consumer receives: *"I'm bringing in one of our team to take this the rest of the way — they'll pick up right here in a moment."* No failure language, no dead end |
| **Caveat** | The guarantee covers phrasebank copy, which is all Mawjood can emit. An **operator's** free-text reply through `tools/handoff.py` is not scanned — a human wrote it, and it is attributed and recorded verbatim |

### Routing engine

| | |
|---|---|
| **Requirement** | Category → ordered platform list, from configuration |
| **How tested** | `pytest tests/adapters/test_plugin_claim.py` — an AST walk asserting no aggregator slug appears in `core/routing`, `core/conversation`, `api` or `db`; plus adding a brand-new platform in one file and one config row |
| **Result** | **PASS** |
| **Evidence** | Reordering a category is an UPDATE to `routing_config`. Adding a platform is one new file plus one row, touching zero core files — demonstrated by `FreshaAdapter` in the test |

### Notifications

| | |
|---|---|
| **Requirement** | Reminders, follow-up, satisfaction 1–5; survives restarts; never double-sends |
| **How tested** | `pytest tests/test_notifications.py` — 68 tests, frozen clock, nothing patched |
| **Result** | **PARTIAL** |
| **Evidence** | Reminders at 24h and 2h, follow-up, and satisfaction capture (rating **and** free-text comment) are written as **database rows** when a booking is confirmed. Restart-survival is proven by dropping every object and reading the schedule back from a fresh session. Double-sending is prevented by a unique constraint on `(booking_id, kind)` — the test violates it directly rather than trusting a code path |
| **Caveat** | **Most reminders cannot currently be sent.** Outside WhatsApp's 24-hour session window only a pre-approved template may go out, and none has been submitted — that needs the operator's Meta Business Manager. Inside the window the same copy sends as an ordinary session message. Outside it the message is **deferred**: held with a reason, retried each pass, never recorded as sent. Visible at `/console/scheduled`. Approval is a config change (`MAWJOOD_APPROVED_TEMPLATES`), not a code change |

### Attribution

| | |
|---|---|
| **Requirement** | `wa.me/…?text=SRC12` and QR identifiers, end to end |
| **How tested** | `pytest tests/test_attribution.py` — link → conversation → attribution row |
| **Result** | **PASS** |
| **Evidence** | 11 tests. The QR generator and the parser are asserted to agree, so a printed poster cannot encode a link Mawjood does not recognise. First touch survives a later campaign — which poster brought someone in is a different question from which they tapped most recently |

### Ops console

| | |
|---|---|
| **Requirement** | Read-only. Conversations, bookings, failures, per-aggregator stats, sources, health, routing trace |
| **How tested** | `pytest tests/test_console.py` — 65 tests including an AST walk over the whole console package |
| **Result** | **PASS** |
| **Evidence** | Ten views. **No mutation routes exist at all** — proven structurally by an AST walk that fails on any non-GET route, any `session.add`/`commit`/`flush`/`delete`, or any imperative SQL, plus an OpenAPI check, plus a test-of-the-test that plants a violation. The routing trace shows every aggregator tried, its outcome, its latency, why the engine advanced, and what the consumer saw, interleaved on one timeline |
| **Caveat** | This revises CLAUDE.md decision 7: the handoff reply and release paths are `tools/handoff.py`, a CLI, not console routes. The capability is unchanged |

### Schema

| | |
|---|---|
| **Requirement** | Tenant-ready from day one; audit trail reconstructable months later |
| **How tested** | `pytest tests/test_tenant_isolation.py tests/test_audit.py`; migrations are run — not `create_all` — on every integration test |
| **Result** | **PASS** |
| **Evidence** | `tenant_id` on every core table, enforced at the repository base class; a cross-tenant read returns nothing. `audit_log` is append-only by database trigger and records what was tried, what came back, its latency, why the cascade advanced, and what the consumer saw |

### Hosting and portability

| | |
|---|---|
| **Requirement** | Plain Docker, 12-factor, zero cloud-specific SDKs |
| **How tested** | `docker compose config`; dependency inspection |
| **Result** | **PASS** |
| **Evidence** | `docker-compose.yml` validates. No cloud SDK appears anywhere in the dependency tree — 84 packages, none cloud-specific. All configuration is environment variables |
| **Caveat** | `docker compose up` has **never been executed** — no Docker daemon is reachable from the build environment. The equivalent was verified with real PostgreSQL 16 and uvicorn |

### Availability

| | |
|---|---|
| **Requirement** | Health endpoints suitable for external monitoring |
| **How tested** | `pytest tests/test_health.py tests/test_monitoring.py` |
| **Result** | **PASS** |
| **Evidence** | `/healthz` (liveness, no dependencies) and `/readyz` (probes PostgreSQL, 503 when unreachable). `/healthz` deliberately stays green during a database outage — a liveness probe that fails on a database incident turns it into an availability incident |
| **Caveat** | No uptime figure exists; nothing has been deployed |

### Latency

| | |
|---|---|
| **Requirement** | Median turn latency under 3s |
| **How tested** | `uv run python tools/loadtest.py` — replays realistic conversations, reports p50/p95/p99 per turn kind |
| **Result** | **PASS** |
| **Evidence** | `docs/evidence/load-*.json` |

Measured on PostgreSQL 16, one application process, connection pool 20 + 10.

| Scenario | Upstream | Turns | p50 | p95 | p99 | max |
|---|---|---|---|---|---|---|
| Happy path, 200 consumers / 25 concurrent | 0 ms | 800 | **0.345 s** | 0.491 s | 0.575 s | 0.610 s |
| Happy path, 100 consumers / 20 concurrent | 400 ms | 400 | **0.375 s** | 0.695 s | 0.712 s | 0.724 s |
| Cascade exhausted, 60 / 15 | 400 ms | 180 | **0.182 s** | 1.153 s | 1.156 s | 1.157 s |
| Cascade exhausted, 60 / 15 | 900 ms | 180 | **0.182 s** | 2.172 s | 2.176 s | 2.178 s |

Worst single turn across all runs: **2.18 s**, on the path where four platforms
are tried and all fail. Under the 2.5 s soft deadline and under the 3 s budget.

**What these include:** the whole pipeline — database, NLU, state machine,
cascade, phrasebank rendering, persistence, audit writes.

**What they exclude, and it matters:**

1. **The WhatsApp round trip.** There is no number. Add the provider's delivery
   time on top.
2. **A real LLM call.** Measured against the deterministic understudy. A
   `gpt-4o-mini` call typically adds several hundred milliseconds to a second —
   on the 900 ms degraded path that consumes the remaining headroom. **This is
   the most likely route to missing the budget in production**, and it is why the
   turn deadline and holding pivot exist.
3. **A real aggregator.** The `--upstream-ms` figures are injected delays, not
   measured ones.

**Two findings from running this rather than reading the code:**

- **The default connection pool is the bottleneck, not the cascade.** At
  concurrency 15 with the shipped default (5 + 5 overflow), p95 on the degraded
  path was **3.83 s** — a miss. Raising the pool to 20 + 10 brought it to
  **2.21 s**. Turns were queueing for connections, not doing work. `DEPLOY.md`
  now sizes the pool against expected concurrency.
- **A booking could crash on a repeat confirmation.** Under a slow upstream the
  harness hit a unique-constraint violation: the adapter correctly honoured its
  idempotency key and returned the *same* booking, and the pipeline then tried to
  insert it again. Fixed — a repeat now recognises the existing booking.

### Data residency

| | |
|---|---|
| **Requirement** | Deployable to GCC, EU or India. Never US-only |
| **How tested** | `pytest tests/test_config.py -k residency` |
| **Result** | **PASS** |
| **Evidence** | `data_residency` is a closed set with no US member — a US-only deployment is not representable. `region` is rejected at startup if it matches a US pattern. Both fail at startup, not in an audit |

### PDPL

| | |
|---|---|
| **Requirement** | Consent capture, minimisation, retention, and a deletion path that purges everywhere including logs |
| **How tested** | `pytest tests/test_pdpl.py` (22 tests); plus a live erasure verified by dumping the database and searching for the identifier |
| **Result** | **PASS** |
| **Evidence** | A consumer with a full history — consent, conversation, 9 messages, a booking, 4 scheduled reminders, 24 audit rows — was erased. Every table returned 0. `pg_dump \| grep -c` on the identifier: **0**. Receipt: `docs/evidence/erasure-receipt.json`. Retention sweep verified, correctly keeping a consumer whose booking has not happened yet: `docs/evidence/retention-sweep.json`. Full write-up: `PDPL.md` |
| **Caveat** | Erasure does not reach backups already taken; bounded to 30 days by backup retention. Consent wording is unreviewed by a lawyer. See `PDPL.md` §10 |

### Security

| | |
|---|---|
| **Requirement** | Dependency audit, secret scan across full history, hardened webhook, rate limiting, PII redaction, least-privilege DB, verified TLS |
| **How tested** | `pytest tests/test_security.py` (52 tests); `pip-audit`; `gitleaks detect` over all 40 commits; `scripts/grants.sql` applied to a live database |
| **Result** | **PASS** |

| Control | Result | Evidence |
|---|---|---|
| Dependency audit | 84 packages, **0 known vulnerabilities** | `docs/evidence/dependency-audit.json` |
| Secret scan, full history | 40 commits, **0 leaks** | `docs/evidence/gitleaks-history.json` |
| — and the scan can still fail | A planted key is caught. Allowlists are narrow value-shapes, never `tests/` or `mawjood/` wholesale | `test_the_scan_config_allowlists_only_named_shapes` |
| Webhook signature | HMAC-SHA256 over raw body, constant-time compare, refused when unconfigured, **mandatory in production** | `TestSignatureVerification` |
| Rate limiting | Per source (600/min, applied **before** the signature check) and per consumer (30/min) | `TestRateLimiting`, `TestWebhookHardening` |
| Body size cap | 256 KB, checked from the header *and* the actual bytes | `TestWebhookHardening` |
| PII in logs | Content dropped, identifiers masked, E.164 masked anywhere in any value, values truncated | `TestLogRedaction` |
| TLS | AST test fails the build on `verify=False`, an unverified SSL context, or a plaintext `http://` literal | `TestTransportSecurity` |
| Least-privilege DB | Verified live: app role **cannot** `CREATE TABLE` (permission denied), **cannot** `UPDATE audit_log` (permission denied), **cannot** `DROP TABLE` (not owner), **can** SELECT and INSERT | `scripts/grants.sql` |
| Audit tamper-resistance | Verified live: `DELETE FROM audit_log` → *"audit_log is append-only"*. With the purge flag set → succeeds. `UPDATE` refused outright, no escape hatch | `tests/test_pdpl.py` |

**Known limitation:** rate-limit buckets are per process. Two application
instances mean twice the configured rate. Acceptable at v1's single-instance
footprint; a shared store is one implementation of `RateLimitStore`, not a
rewrite. Flagged in `DEPLOY.md` before anyone scales out.

### Logging

| | |
|---|---|
| **Requirement** | Structured logs for every conversation, API call and routing decision; 90-day retention |
| **How tested** | `pytest tests/test_logging.py tests/test_audit.py` |
| **Result** | **PASS** |
| **Evidence** | structlog JSON throughout, with request, conversation, turn and decision ids threaded so one conversation can be reconstructed from a log search. 90-day retention enforced by `tools/pdpl.py retention`, which records that it ran |

### Monitoring

| | |
|---|---|
| **Requirement** | Sentry alerts, UptimeRobot-ready endpoints |
| **How tested** | `pytest tests/test_monitoring.py` (35 tests) |
| **Result** | **PASS** |
| **Evidence** | Alerts for the conditions a person must act on, with `reconciliation_inconclusive` as **critical** — the case where we do not know whether a consumer has a booking. Alert payloads redact consumer fields; Sentry events drop request bodies and stack-frame locals. Alerting never raises, including on a broken `__repr__`, because it runs on the booking path. `/healthz` and `/readyz` are unauthenticated and keyword-checkable |
| **Caveat** | No Sentry DSN is configured, so no alert has been delivered to a real project |

### Backup and restore

| | |
|---|---|
| **Requirement** | Daily backup, 30-day retention, and a restore drill actually performed |
| **How tested** | Ran `scripts/backup.sh` then `scripts/restore.sh` against a database holding a completed booking |
| **Result** | **PASS** |
| **Evidence** | Backup: 68 KB, 14 tables, verified with `pg_restore --list`. Restore: all 8 populated tables matched, Alembic version matched, **the `audit_log` append-only trigger survived** (schema, not data — without it a restored database silently accepts audit deletions). The overwrite guard refused `--into mawjood_dev`. Recorded in `RUNBOOK.md` |
| **Caveat** | The drill ran at a data volume of tens of rows. It proves the scripts, the schema round-trip and the guards. **It does not establish a restore time at production volume** and the elapsed figure must not be read as an RTO |

---

## The three gaps

Everything above marked "fakes" traces to one of these. All three are **external
lead time, not engineering**.

### 1. No aggregator is connected

Zenoti and Deliveroo documentation is unreachable from the build environment —
HTTP 403 on every path, direct connections refused, re-verified 2026-07-30.
CLAUDE.md §10 forbids guessing an API from memory, so no endpoints were invented.

Deliveroo has a **second, more interesting blocker**: its Order API is
merchant-side. It receives orders Deliveroo already took through its own apps; it
does not place them. An outside assistant cannot originate one — **and a
partnership does not change that**. The adapter records support per method so an
operator can see this before signing anything. `INTEGRATION_NOTES.md` has the
table.

**Consequence:** every booking in every demo is against a test double. The
architecture is proven end to end — the cascade, the double-booking defence, the
audit trail and the invariant all run real code — but the far end is simulated.

### 2. No WhatsApp number

No BSP account, so no number, so no real round trip. The webhook is exercised
against fixtures whose shape is corroborated but unconfirmed.

### 3. No approved message templates

Meta template approval needs the operator's Business Manager account. Until it
lands, scheduled messages outside the 24-hour window are held rather than sent.

**All three are unblocked by paperwork.** None requires a design change, and each
lands as configuration: a credential, a number, a template name.

---

## What would change these results

| If you… | Then |
|---|---|
| Provision a WhatsApp number | Channel moves to PASS. Latency needs re-measuring with the real round trip |
| Connect one aggregator | Booking actions moves to PASS. The plugin claim stops being structural and becomes demonstrated |
| Get templates approved | Notifications moves from PARTIAL to PASS. One config change, no code |
| Switch on the OpenAI provider | Latency needs re-measuring. Most likely route to missing the 3 s budget |
| Deploy to real infrastructure | Availability gets a number. Encryption at rest becomes verifiable |
| Run the restore drill at production volume | Backup gets a real RTO |
