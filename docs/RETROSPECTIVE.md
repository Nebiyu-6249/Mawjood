# RETROSPECTIVE

What I would do differently with more time. Honest, ordered by how much it would
have mattered, no padding.

This is not a list of features that were out of scope — those are in `CLAUDE.md`
§14 and they were correctly excluded. It is a list of things that are *wrong or
weak in what was built*, and what I now think the right call was.

---

## 1. Apply for external access on day one

**The single biggest cost of this build.**

Four vendor documentation hosts were unreachable from the build environment —
Meta, 360dialog, Zenoti and Deliveroo, all HTTP 403 on every path, re-verified
2026-07-30. The rule was not to invent endpoints, so no live adapter exists.
Zenoti and Deliveroo are documented stubs. Every claim about aggregator behaviour
in this repository is proven against `core/aggregators/fake/`.

`PLAN.md` flagged sandbox access, a BSP account and Meta template approval as
external lead time on the critical path. It flagged them and then the build
carried on without them, because there was always something else that could be
built. That was the mistake: **the applications should have gone out before the
first line of code**, when the lead time would have run in parallel with
everything in Phases 0–3 instead of after all of it.

The architecture absorbed this well — the adapter layer is a plugin interface,
adding a platform is one file and one config row, and there is a test that proves
it. But an architecture that is *ready* for integrations is not the same as a
product that *has* them, and I would rather be at the end of Phase 4 with one
real Zenoti booking than with an excellent stub.

## 2. Prove the "same code path" claim in Phase 1, not Phase 4

`chat_sim.py` and the WhatsApp webhook were required to be the same pipeline —
"same handlers, different transport. It must never become a second code path."

They were, structurally. But `handle_inbound` was called **without `settings`**
from the webhook, and with them from `chat_sim`. So on the live path the adapter
registry was empty, the LLM provider silently reverted to the default, and the
turn budget and timezone reverted too. `chat_sim` worked perfectly. WhatsApp would
have found no aggregators at all — the invariant would have held, every consumer
would have gone to a human, and the product would have looked like it worked while
booking nothing.

It was found in Phase 4 by an AST parity test comparing the two call sites. That
test takes about twenty lines. **It should have been written in Phase 1, in the
same commit as the claim it defends.** A property that is asserted in a document
and enforced by nothing is a property you do not have.

The general lesson, which the rest of this build then applied: the highest-value
tests here were the ones that check a *structural* property — no mutation routes
in the console package, no platform slug in core logic, `.env.example` complete
against `config.py`, the two call sites agreeing. They cost twenty lines each,
they never go stale, and three of them caught real bugs. I would write more of
them, earlier, and I would write one for every architectural claim in `CLAUDE.md`
rather than for the ones I happened to think of.

## 3. Load test in Phase 2

The load harness was Phase 4 work and it found, in one afternoon:

- **A real booking bug.** On a repeat confirmation the adapter correctly returned
  the same `BookingRef`; the pipeline blindly `INSERT`ed and hit the unique
  constraint, crashing the turn. No unit test covered a consumer confirming
  twice — which is an entirely ordinary thing for a person to do on WhatsApp.
- **The connection pool ceiling.** p95 of 3.83 s at concurrency 15, which looked
  like a cascade problem and was not: turns were queuing for a database connection
  while the cascade held one open across its upstream calls. Pool of 20 + 10 →
  2.21 s, no other change.
- **Two harness bugs that were themselves informative** — reused `wa_id`s
  measuring muted post-handoff conversations at a meaningless 85 ms, and structlog
  binding `sys.stdout` at configure time so JSON output was corrupted by log
  lines.

Replaying realistic traffic turned out to be a better correctness test than
several of the correctness tests. I would build a small version of it as soon as
the pipeline could complete one booking, and run it every phase.

## 4. Test against real PostgreSQL in CI, not opt-in

`make test-integration` needs `MAWJOOD_TEST_DATABASE_URL` and is skipped
otherwise. That means the guarantees that live *in the database* — the append-only
`audit_log` trigger, the least-privilege grants, cascade-delete behaviour, the
`UniqueConstraint` that makes notifications idempotent — are only checked when
somebody remembers to check them.

These are exactly the guarantees a reviewer would ask about, and they are the ones
with the weakest automated coverage. A PostgreSQL service container in CI is
fifteen minutes of configuration. I would spend it.

## 5. Ship one Arabic phrase

The language-readiness decision (`CLAUDE.md` §6) says adding Arabic must be a data
change, never a refactor. The phrasebank is keyed by `(key, locale)`, copy is
never inline, and `RenderedMessage` is unforgeable. Structurally, the claim holds.

But **`ar` has never been loaded**. Only `en` exists, so the test asserting "every
key exists in every shipped locale" is vacuously true, and nothing has ever
exercised locale selection, fallback to default, or the blocklist scan against a
non-English phrasebank. The claim is architecturally sound and empirically
untested.

The right move was one `ar.yaml` with a handful of real entries and a test that
renders them — enough to prove the seam, not enough to be Arabic support. It would
also have surfaced the questions nobody has asked yet: how the blocklist works in
a language where the failure phrases are different words, and whether RTL text
survives the BSP intact.

## 6. Offsite backups

`scripts/backup.sh` writes to local disk. It verifies the dump with
`pg_restore --list` before trusting it, prunes only after a successful dump, and
the restore path has been drilled. All good — and all on **the same machine as the
database**.

A server loss loses the backups with it. `MAWJOOD_BACKUP_BUCKET` is reserved in
`.env.example` and nothing reads it. This is a twenty-line addition (upload after
verify, fail loudly if the upload fails) and it is the difference between a backup
strategy and a backup script.

## 7. Model the whole schema in Phase 1

`scheduled_notifications` was added in Phase 3, against `CLAUDE.md` §13's "ask
before altering the schema after Phase 1". I flagged it and proceeded, because
"persistent scheduling that survives restarts" and "never double-remind" cannot be
done without a table, and the `UniqueConstraint("booking_id", "kind")` *is* the
idempotency guarantee.

The decision was right; the timing was avoidable. Reminders, follow-ups and
satisfaction capture were all in the brief from the start. The table was knowable
in Phase 1 and could have been created empty, which would have kept the rule
intact instead of requiring an exception to it.

## 8. Give the handoff CLI real operator identity

`tools/handoff.py` takes `--operator dana` as a **string**. Anyone with shell
access can claim, reply as, and release on behalf of anyone. The audit trail
records what that string said, which is not the same as recording who did it.

The console has a proper session with a signed cookie and constant-time credential
comparison. The CLI, which is the tool that actually *sends messages to
consumers*, has none. That asymmetry is backwards. Operators should authenticate,
and the audit row should carry an identity the system verified rather than one it
was told.

## 9. Record real LLM latency

The published latency numbers (`ACCEPTANCE.md`) exclude the LLM call entirely —
`MAWJOOD_LLM_PROVIDER=deterministic`, so understanding costs approximately zero
milliseconds. This is stated plainly there, and it is the biggest gap in the
numbers: **a real GPT-4o-mini call is the most likely route to missing the 3s
budget in production**, and every figure in that table is missing it.

Without a key there is no way to measure it. But there was a way to *bound* it: a
provider shim that sleeps for a configurable, realistic distribution — say
300–900 ms with a long tail — would have produced numbers that are still estimates
but honest ones, and would have shown whether the deadline budget's holding-pivot
path fires at the rate it should under realistic understanding latency. That path
is tested for correctness and has never been exercised at its actual trigger rate.

## 10. Make the rate limiter shared before anyone scales out

Token buckets are per process. Two instances mean twice the effective rate. This
is documented in `.env.example`, `DEPLOY.md` and the module itself, which is the
right thing to do about a limitation you are keeping — but it is a limitation that
will be discovered by someone scaling out under load, which is the worst moment.

The interface in `mawjood/api/ratelimit.py` is small. A Postgres-backed
implementation would have been an hour, would have used a database that is already
there, and would have removed a footnote from three documents.

## 11. Golden transcripts test the state machine, not the understanding

The transcripts in `tests/transcripts/` are genuinely valuable — they pin
conversation flow, they catch regressions in state transitions, and they read like
conversations rather than assertions. But they run against the deterministic
understudy, so what they prove is that **the state machine does the right thing
given a correct `Understanding`**.

Nothing tests that the real model *produces* a correct `Understanding` from
"need a haircut in Marina tomorrow evening". The prompt in `nlu_v1.md` has never
been run against the model it was written for. When a key exists, the first thing
to build is a small evaluation set of real UAE phrasings — including the ambiguous
ones the persona section calls out, like Al Nahda being in both Dubai and Sharjah —
scored against expected `Understanding` objects.

## 12. Smaller things I noticed and left

- **`Result.__bool__` raises `TypeError`** to stop anyone writing `if result:` and
  silently treating `NO_AVAILABILITY` as failure. I like it, but it is surprising
  enough that it should be in `ADD_AN_AGGREGATOR.md` and it is not.
- **The blocklist lives in the test suite**, correctly. But the runtime filter for
  free LLM prose and the test-time blocklist are two lists that must agree and
  nothing checks that they do.
- **The console has no pagination.** At a few hundred conversations it is fine. At
  a few hundred thousand, `/console/conversations` will time out.
- **Circuit breaker state is in-process**, same as the rate limiter, with the same
  consequence when scaled out and no note about it anywhere.

---

## What I would not change

Worth stating, because a retrospective that only lists regrets misrepresents the
build.

**The invariant is enforced mechanically, not by discipline.** Consumer copy can
only be produced by the phrasebank; `RenderedMessage` is unforgeable; the blocklist
scan enumerates every entry and gates the build; and there is a test that plants a
blocklisted phrase to prove the scan can fail. That combination is why the property
is a safety property rather than an aspiration, and it cost very little.

**One mechanism serves both the invariant and the latency target.** The graceful
pivot and the holding message under deadline pressure are the same code path. It
would have been easy to build two and end up with two sets of bugs.

**The LLM never drives control flow.** It returns a structured `Understanding` and
every decision is an explicit transition. Under an LLM outage the product degrades
in understanding rather than confirming bookings nobody agreed to, and that is
worth the extra machinery.

**Refusing to guess endpoints was right, and it was expensive.** Four platforms
are stubs because their docs were unreachable. A stub that returns `UNSUPPORTED`
and routes to a human is honest; an adapter full of plausible-looking paths would
have looked like progress for months and failed on the first real merchant.
