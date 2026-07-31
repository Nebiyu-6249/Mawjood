# PDPL evidence

**Audience:** a compliance reviewer, or the legal team of an operator evaluating
Mawjood. It assumes no knowledge of the codebase.

**Scope:** UAE Federal Decree-Law No. 45 of 2021 on the Protection of Personal
Data (PDPL). This document records what Mawjood does, how it was verified, and
where the verification can be re-run.

**Status:** the mechanisms described here are implemented and tested. Mawjood has
not yet processed a real consumer's data — there is no live WhatsApp number.
Every figure below comes from a controlled run against a real PostgreSQL
instance, reproducible with the commands given.

**This is not legal advice.** It is an engineering record of what the system
does, written so that a lawyer can assess it. Consent wording in particular is
drafted by engineers and marked for legal review — see §3.

---

## 1. What personal data Mawjood holds

| Data | Where | Why |
|---|---|---|
| WhatsApp identifier (`wa_id`), effectively a phone number | `leads` | The only way to reply to a consumer |
| Display name, if the provider sends one | `leads` | Addressing someone by name |
| Message content, inbound and outbound | `messages` | Holding a conversation; the record of what was agreed |
| Booking details — venue, time, price | `bookings` | Making and confirming the booking |
| Consent events and the exact wording shown | `consents` | Demonstrating lawful basis |
| Source code from a QR or link | `attribution` | Knowing which campaign brought someone in |
| Routing decisions, including the consumer's words | `audit_log` | Explaining why a booking went the way it did |
| Satisfaction rating and comment | `feedback` | Service quality |

**Data minimisation.** Mawjood asks for what a booking needs and nothing else. No
email address, no date of birth, no address beyond the area of the city, no
payment details — v1 moves no money and has no PCI scope (CLAUDE.md decision 3).

**What leaves the system.** Personal data reaches exactly two classes of third
party, both declared:

1. **The messaging provider (BSP)** carries the conversation. Unavoidable — it is
   how WhatsApp works.
2. **The booking platform**, and only after recorded consent. Only what the
   booking needs: name, contact number, the slot.

The LLM provider receives message text for understanding, under zero-retention,
no-training terms (CLAUDE.md decision 6, and `KEYS.md`). Sentry receives errors
with request bodies and stack-frame locals stripped — see §5.

---

## 2. Lawful basis and consent

Consent is captured on **first inbound message**, before any personal data
reaches a booking platform.

The `consents` table is an **append-only event stream**, not a flag. Current
state is derived from the events, so the history of what a person was shown and
when survives intact. Each row records:

- the event (`notice_shown`, `granted`, `withdrawn`),
- **the exact wording displayed**, character for character,
- the policy version of that wording,
- the timestamp.

Storing the rendered string rather than a template id is deliberate: the only
reliable answer to "what was this person shown?" is the text that actually went
out.

**Verify:**
```bash
uv run pytest tests/test_consent.py tests/test_pipeline.py -q
```

**The gate.** `core/conversation/states.py` will not transition into booking
unless consent is `GRANTED`. It is a state-machine transition, not a runtime
check that can be forgotten:

```bash
uv run pytest tests/test_conversation_engine.py -q -k consent
```

> **For legal review.** The consent wording in
> `mawjood/core/conversation/phrases/en.toml` (`consent.notice`) was drafted by
> engineers as a starting point. It has **not** been reviewed by a lawyer. It
> should be reviewed, and the `policy_version` field bumped when it changes; past
> consents keep the version they were given.

---

## 3. Right to erasure

### What was done

A consumer completed a full conversation — consent, a search, a confirmed
booking, a scheduled reminder, a tagged arrival — and was then erased.

```bash
uv run python tools/pdpl.py plan  --wa-id 971505551234   # what would go
uv run python tools/pdpl.py erase --wa-id 971505551234 --confirm --json
```

### Before

| Table | Rows |
|---|---|
| leads | 1 |
| conversations | 1 |
| messages | 9 |
| bookings | 1 |
| consents | 2 |
| attribution | 1 |
| scheduled_notifications | 4 |
| audit_log | 24 |

### After

Every table: **0**. `audit_log` holds 2 rows, both compliance records with no
`lead_id` — see below.

### Independent verification

Counting rows is what the tool reports, so it was checked a second way that does
not trust the tool: dump the entire database and search for the identifier.

```
$ pg_dump "$MAWJOOD_DATABASE_URL" | grep -c "971505551234"
0
```

**Zero occurrences in a full dump of every table.**

The receipt is filed at `docs/evidence/erasure-receipt.json`.

### What is retained after an erasure, and why

Two `audit_log` rows survive: `compliance.erasure_requested` and
`compliance.erasure_completed`. They carry:

- a **salted SHA-256 hash** of the identifier, never the identifier;
- per-table counts of what was deleted;
- a `complete: true` assertion.

They contain no personal data. The hash is salted with the tenant id, so it
cannot be checked against a phone book without also knowing the tenant, and an
operator handling a follow-up can recompute it from an identifier they were
given.

This is the minimum needed to demonstrate the erasure happened. Retaining
nothing would make the obligation unauditable; retaining the number would defeat
the erasure.

### The append-only audit log

`audit_log` is append-only, enforced by a PostgreSQL trigger — a routing decision
that can be rewritten afterwards is not a record. `UPDATE` is refused outright,
with no escape hatch.

`DELETE` is refused unless the session sets `mawjood.allow_purge`, which erasure
and the retention sweep do and nothing else does. It is `SET LOCAL`, so it dies
with the transaction.

**Verify the hatch is narrow:**
```bash
uv run pytest tests/test_pdpl.py -q -k "ordinary_connection"
```

### What erasure does not do

**It does not cancel a booking that has not happened yet.** Erasing our record
leaves the venue still expecting that person. `pdpl.py plan` surfaces this before
anything is deleted:

```
ATTENTION — bookings still ahead of them:
  Marina Beauty Lounge at 31 Jul 11:30

Erasing our record does NOT cancel these. The venue is still
expecting this person. Cancel first if that is what they asked for.
```

Whether to cancel first is the operator's decision and depends on what the
consumer asked for. The system refuses to make it silently.

### Completeness over time

The risk with an erasure routine is a table added later and never wired in. A
test enumerates the **live schema** and fails if any table carrying a `lead_id`
is missing from the erasure list:

```bash
uv run pytest tests/test_pdpl.py -q -k erasure_surface
```

The failure arrives on the commit that adds the table, not during an audit.

---

## 4. Retention

| Data | Retention | Setting |
|---|---|---|
| Audit and operational log rows | 90 days | `MAWJOOD_LOG_RETENTION_DAYS` |
| Consumer data — leads, conversations, messages, bookings, consents | 365 days from last contact | `MAWJOOD_DATA_RETENTION_DAYS` |

### The sweep runs and reports

```bash
uv run python tools/pdpl.py retention --dry-run   # count without deleting
uv run python tools/pdpl.py retention --json      # run it
```

Verified run (`docs/evidence/retention-sweep.json`): two stale consumers, one
with a past booking and one with a booking still ahead of them.

```
audit older than     90d (2026-05-01)
consumers older than 365d (2025-07-30)
  audit_log 1   consents 2   messages 9   bookings 1
  conversations 1   scheduled_notifications 4   leads 1
kept 1 consumer past the cutoff who still has a booking ahead of them
```

**A consumer with a future booking is never swept.** Deleting them would leave a
venue expecting somebody the system no longer knows about.

**The sweep records itself** as a `compliance.retention_swept` audit row, so "did
retention run?" is answerable from the database rather than from a cron log
nobody keeps.

### Consent and retention

Consent rows are deleted along with everything else.

An earlier design retained them, reasoning that consent is the evidence of lawful
basis. **That reasoning was wrong and a test caught it.** A consent row is itself
personal data — keyed to a person, carrying the wording they were shown. Keeping
it after their conversation, messages and bookings are gone means holding
personal data past the retention period with nothing left for it to evidence.

Consent lives exactly as long as the data it authorises. The correction is
recorded here because a reviewer should see that the question was asked.

---

## 5. Logs

Logs leave the system — they ship to whatever aggregator a deployment uses, which
is **outside** the retention and erasure boundary. An erased consumer whose
messages sit in a log index has not been erased.

So the logging layer redacts before anything reaches a handler:

| Class | Treatment | Why |
|---|---|---|
| Message content — `body`, `text`, `consumer_text`, `comment`, `notes` | **Dropped** | Masking a sentence is meaningless; it should not be there at all |
| Identifiers — `wa_id`, `phone`, `email` | **Masked**, last 3 characters kept | An operator still has to correlate a thread; 3 characters is not dialable |
| Credentials — `password`, `api_key`, `token`, `authorization`, `cookie` | **Dropped** | — |
| Anything E.164-shaped, anywhere in any value | **Masked** | A number can arrive inside an error string under a key nobody listed |
| Values over 2000 characters | Truncated | An unbounded value is how a whole request body reaches an aggregator |

**Sentry** additionally has request bodies and stack-frame locals stripped before
send (`_scrub_event` in `mawjood/main.py`). `send_default_pii=False` covers
Sentry's automatic capture; it does **not** cover a webhook payload sitting in a
local variable of a frame in a stack trace, which is exactly where a consumer's
message would be.

**Verify:**
```bash
uv run pytest tests/test_security.py -q -k Redaction
uv run pytest tests/test_monitoring.py -q -k Scrubbing
```

---

## 6. Backups

| Property | Value |
|---|---|
| Frequency | Daily, `scripts/backup.sh` |
| Format | `pg_dump --format=custom`, compressed |
| Retention | 30 days |
| Verification | Every dump is checked with `pg_restore --list` before it is trusted |
| Restore drill | Performed; recorded in `RUNBOOK.md` |

### The honest gap: erasure and backups

**An erasure does not reach into backups already taken.** A backup written before
the request still contains that consumer, and will until it ages out.

The retention window bounds this: **a backup containing an erased consumer exists
for at most 30 days after the erasure.** After that, every backup that could
contain them has been deleted.

This is the standard position for logical backups and it is stated rather than
glossed. If an operator's legal advice requires erasure to reach backups, the
options are: shorten backup retention; or restore-erase-redump on each request,
which is expensive and itself risky. Neither is implemented, because which is
appropriate is the operator's call.

**What is guaranteed:** an erased consumer cannot come back. A restore is into a
separate database for verification, and returning a backup to production is a
deliberate incident-recovery act (`scripts/restore.sh` refuses the live database
without `--force-into-live`). Any such restore must be followed by re-running the
erasure — a step that belongs in the operator's incident procedure and is noted
in `RUNBOOK.md`.

---

## 7. Data residency

Consumer data must be able to live in a GCC, EU or India region, and **never
US-only**.

This is enforced at startup rather than documented:

- `data_residency` is a closed set of `{gcc, eu, india}`. A US-only deployment is
  **not representable** — there is no value to typo into.
- `region` is rejected at startup if it matches a US region pattern
  (`us-`, `useast`, `westus`, `northamerica-`, …).

```bash
uv run pytest tests/test_config.py -q -k residency
```

No component hard-assumes a region. There are no cloud-specific SDKs anywhere in
the dependency tree — deployment is plain Docker and 12-factor environment
variables (CLAUDE.md decision 5).

---

## 8. Security controls relevant to PDPL

| Control | Status | Evidence |
|---|---|---|
| Encryption in transit | TLS enforced; a test fails the build on `verify=False` or an unverified SSL context anywhere in the codebase | `tests/test_security.py::TestTransportSecurity` |
| Encryption at rest | Deployment responsibility — volume or managed-service encryption. Not enforceable from application code, so it is a `DEPLOY.md` checklist item rather than a claim here | `DEPLOY.md` |
| Least-privilege database access | Two roles. The application cannot create, drop or alter any object, and cannot UPDATE the audit log | `scripts/grants.sql`, verified live — see `ACCEPTANCE.md` |
| Webhook authentication | HMAC-SHA256 over the raw body, constant-time compare, refused when unconfigured, mandatory in production | `tests/test_security.py::TestSignatureVerification` |
| Secrets | Environment or managed store only. Zero hardcoded credentials; full git history scanned | `docs/evidence/gitleaks-history.json` |
| Dependency vulnerabilities | 84 packages audited, 0 known vulnerabilities | `docs/evidence/dependency-audit.json` |
| Access to consumer transcripts | Ops console requires a session; refuses to serve at all when no credential is configured; production will not start without one | `tests/test_console.py::TestAuthenticationGate` |

---

## 9. Reproducing everything here

```bash
# 1. A database
createdb mawjood_pdpl
export MAWJOOD_DATABASE_URL=postgresql+asyncpg://…/mawjood_pdpl
uv run alembic upgrade head
uv run python tools/seed_routing.py

# 2. A consumer with a complete history
uv run python tools/chat_sim.py --wa-id 971505551234 \
    --script "SRC42" "yes" "need a haircut in Marina tomorrow at 5pm" "yes"

# 3. Erase, and verify independently
uv run python tools/pdpl.py plan  --wa-id 971505551234
uv run python tools/pdpl.py erase --wa-id 971505551234 --confirm --json
pg_dump "$MAWJOOD_DATABASE_URL" | grep -c 971505551234    # expect 0

# 4. Retention
uv run python tools/pdpl.py retention --dry-run
uv run python tools/pdpl.py retention --json

# 5. The whole compliance suite
uv run pytest tests/test_pdpl.py tests/test_security.py -q
```

---

## 10. Known gaps

Stated plainly, because a reviewer will find them anyway.

1. **No live processing has occurred.** Every result here is from a controlled
   run. The mechanisms are tested; they have not met a real consumer.
2. **Consent wording is unreviewed by a lawyer.** Drafted by engineers, marked
   for review, versioned so a change is traceable.
3. **Erasure does not reach existing backups.** Bounded to 30 days by backup
   retention. See §6.
4. **The BSP holds message content independently.** Deleting from Mawjood does
   not delete from the provider's systems. An operator's data-processing
   agreement with their BSP has to cover this; it is outside what this codebase
   can enforce.
5. **No Data Protection Officer, DPIA or RoPA.** These are the operator's
   organisational obligations, not the software's. Mawjood provides the technical
   evidence they would draw on.
6. **Encryption at rest is not verified here.** It depends on the deployment
   target, which is the operator's choice.
