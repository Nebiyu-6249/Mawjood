# RUNBOOK

Operating Mawjood: routine procedures, incident response, backup and restore.

For getting a server running in the first place, see `DEPLOY.md`. This file
assumes it is already running and something needs doing.

> **Honest scope.** Mawjood has never operated against live WhatsApp traffic or a
> live aggregator. The procedures below are exercised against the local stack,
> the test suite and the load harness; the incident playbooks in §5 are written
> from the code's actual behaviour, not from having lived through each one. Expect
> to correct them after the first month, and please do correct them here.

## Contents

1. [On-call basics](#1-on-call-basics)
2. [Backup and restore](#backup-and-restore)
3. [Monitoring](#monitoring)
4. [Handoff](#handoff)
5. [Incident playbooks](#5-incident-playbooks)
6. [Routine procedures](#6-routine-procedures)
7. [Consumer data deletion (UAE PDPL)](#7-consumer-data-deletion-uae-pdpl)
8. [Database users](#8-database-users)

---

## 1. On-call basics

### What you are protecting

One property, above everything else: **a consumer is never told there are no
vendors, no availability, no options, or that something failed.** Mawjood is named
after it. Every incident below is judged first by whether that held.

It holds mechanically — consumer copy can only come from the phrasebank, and the
phrasebank is scanned against a failure-language blocklist that gates the build.
So during an incident you are almost never protecting the consumer's *experience*;
you are protecting the booking itself and the operator's ability to see what
happened. Consumers whose cascade fails reach a person. That is working as
designed, not an outage.

### The one thing that pages

`alert_kind=reconciliation_inconclusive`. A booking timed out and we cannot tell
whether it landed. Somebody may be expected at a salon, or may not, and only a
phone call resolves it. Everything else can wait for business hours.

### First five minutes of anything

```bash
curl -s https://your-host/readyz | jq          # is the database reachable
docker logs --since 15m mawjood | grep -c ERROR
```

Then open `/console/health`. It answers, in one page: stalled conversations,
handoff queue age, deferred notifications, failed sends in the last 24 hours, and
which adapters are live rather than stubbed. If the console is up and the queue is
draining, the product is working even if something else is on fire.

### Where the truth is

| Question | Where |
|---|---|
| What happened in this conversation? | `/console/trace/<conversation_id>` — every aggregator tried, its outcome, latency, why the cascade advanced, and exactly what the consumer saw |
| Is this platform failing generally? | `/console/aggregators` — success rate and median latency per platform |
| Who is waiting for a human? | `/console/handoff`, or `uv run python tools/handoff.py list` |
| What did the system do at 14:32? | `audit_log`, by `correlation_id`. Append-only, enforced by a database trigger. |

The audit log is the record of last resort and it cannot be rewritten: `UPDATE` is
refused outright, and `DELETE` only works inside a transaction that has explicitly
set `mawjood.allow_purge` — which only the PDPL erasure path and the retention
sweep do.

### Escalation

1. **Ops** — queue work, credential re-issue, template submissions.
2. **Platform** — anything in the table in §5.
3. **The operator's legal contact** — consumer data incidents only, per §9 of
   `PDPL.md`. Do not wait for engineering to finish investigating; the PDPL
   notification clock does not.

---

## Backup and restore

### Daily backup

`scripts/backup.sh` takes a custom-format `pg_dump` — compressed, and restorable
selectively with `pg_restore`, which a plain SQL dump cannot do. That matters
during an incident when you want one table back rather than the whole database.

```bash
./scripts/backup.sh --dir /var/backups/mawjood
```

Cron, daily at 02:30 Gulf time (22:30 UTC the previous day):

```cron
30 22 * * * MAWJOOD_DATABASE_URL=... /srv/mawjood/scripts/backup.sh --dir /var/backups/mawjood
```

Two behaviours worth knowing:

- **The dump is verified before it is trusted.** `pg_restore --list` must succeed,
  because finding out a dump is unreadable during an incident is the worst
  possible time.
- **Pruning happens after a successful dump, never before.** Otherwise a failing
  backup quietly erodes the history it was supposed to be adding to.

Retention is 30 days (`--retention-days`).

### Restore

```bash
./scripts/restore.sh --file /var/backups/mawjood/mawjood-20260730T223000Z.dump \
                    --into mawjood_restore_check
```

It restores into a **separate** database and refuses to touch the one named by
`MAWJOOD_DATABASE_URL` unless `--force-into-live` is passed. Destroying live data
should be something you have to type.

It then verifies, because `pg_restore` exiting 0 is not the same as the data
being there — a dump taken against an empty database restores perfectly and tells
you nothing. It prints row counts per table, the Alembic version, and checks that
the `audit_log` append-only trigger came back. That trigger is schema rather than
data; without it a restored database silently accepts audit deletions.

### Restore drill — performed 2026-07-30

Run against PostgreSQL 16 with a database carrying a completed booking, its
conversation, its consent events and its audit trail.

| Step | Result |
|---|---|
| `scripts/backup.sh` | 68 KB dump, 14 tables with data, verified by `pg_restore --list` |
| `scripts/restore.sh --into mawjood_restore_check` | restored and verified |
| Row counts after restore | tenants 1, leads 1, conversations 1, messages 9, bookings 1, consents 2, audit_log 23, scheduled_notifications 4 |
| Alembic version | `bd6290b7f0ee` — matches the source database |
| `audit_log` append-only trigger | present |
| Overwrite guard | `--into mawjood_dev` refused, as designed |
| Elapsed | under 5 seconds at this data volume |

**Caveat, stated plainly:** this drill was performed at a data volume of tens of
rows. It proves the scripts, the schema round-trip and the guards are correct. It
does **not** establish a restore time at production volume, and the elapsed
figure above must not be read as an RTO. Re-run it against a
production-sized database before launch and record the real number here.

---

## Monitoring

### Uptime checks

Both endpoints are unauthenticated so an external monitor can poll them.

| Endpoint | Meaning | Monitor as |
|---|---|---|
| `/healthz` | the process is alive; touches nothing else | keyword `ok`, 60s interval |
| `/readyz` | this instance should receive traffic; probes PostgreSQL | HTTP 200, 60s interval |

`/healthz` deliberately stays green when the database is down. A liveness probe
that fails on a database outage causes an orchestrator to kill healthy pods and
turns a database incident into an availability incident.

`/readyz` returns 503 when PostgreSQL is unreachable, and names the region and
data residency so a monitor pointed at the wrong region can tell.

### Alerts

`mawjood/observability/alerts.py` raises Sentry events for the conditions a
person must act on. Alert rules should be written against the `alert_kind` tag:

| `alert_kind` | Severity | What to do |
|---|---|---|
| `reconciliation_inconclusive` | critical | **Page.** A booking timed out and we cannot tell whether it landed. Contact the venue, confirm, then reply to the consumer from `tools/handoff.py`. Never guess. |
| `credential_degraded` | error | A merchant rejected our credential. Every consumer routed there is degraded until it is re-issued, and nothing outside this system will notice. |
| `aggregator_circuit_open` | warning | A platform is rate limiting us. Usually self-healing; investigate if it repeats. |
| `notification_backlog` | warning | Scheduled messages piling up. Expected non-zero until WhatsApp templates are approved — a *rising* number is the signal. |
| `configuration_fault` | error | Routing config names a platform with no adapter, or similar. |

Alerts carry no message bodies and no phone numbers. Sentry is not a declared
processor for consumer content (CLAUDE.md §11), so payload fields are redacted by
name and request bodies and stack-frame locals are stripped before send.

### What the console answers

`/console/health` is the human view: stalled conversations, queue age, deferred
notifications, failed sends in the last 24 hours, and which adapters are
implemented against a live contract rather than stubbed.

---

## Handoff

The console is read-only. Working the queue is a CLI:

```bash
uv run python tools/handoff.py list
uv run python tools/handoff.py claim <id> --operator dana
uv run python tools/handoff.py reply <id> --operator dana --text "..."
uv run python tools/handoff.py release <id> --operator dana --notes "sorted by phone"
```

`release` is the one people forget. Until it runs, the assistant stays muted on
that conversation and the consumer is talking to nobody.

---

## 5. Incident playbooks

### 5.1 `reconciliation_inconclusive` — a booking may or may not exist

**Severity: page.** The only one.

`create_booking` timed out, the reconciliation read could not determine whether it
landed, and the cascade correctly refused to try anywhere else — retrying would
book the consumer twice.

State: the booking row is `UNCERTAIN`, the conversation is in the handoff queue,
the assistant is muted on it, and the consumer has a holding message. Nothing is
degrading while you work.

1. `uv run python tools/handoff.py list` — find it, `claim` it.
2. Open `/console/trace/<conversation_id>`. Note the merchant, the slot start, and
   the idempotency key.
3. **Phone the venue.** This is the step. There is no API answer or the
   reconciliation read would have found one.
4. If the booking exists: `tools/handoff.py reply` confirming it, then `release`.
5. If it does not: reply offering to rebook, then `release`. The conversation
   resumes normally.
6. Record which it was. A pattern of inconclusive timeouts on one platform means
   its `find_by_idempotency_key` implementation is too weak — see
   `ADD_AN_AGGREGATOR.md` §5.

**Never guess, and never let the assistant resume before you know.** A consumer
told "you're booked" who is not is worse than any outage in this document.

### 5.2 `credential_degraded` — a merchant rejected our key

**Severity: error. Business hours, but same day.**

Every consumer routed to that merchant is silently degraded — the cascade advances
past it and they get a different venue. It works, and **nothing outside this
system will notice**, which is exactly why the alert exists.

1. `/console/aggregators` — confirm the merchant and how long it has been failing.
2. Re-issue the credential at the platform.
3. Follow the no-outage rotation in `KEYS.md` §2: add the new value under a *new*
   `secret_ref`, repoint the `merchant_credentials` row, verify on the next call.
   Credentials resolve per call — no restart, no cache.
4. Clear `credential_degraded_at` on the row.
5. Watch one live conversation route to it in the trace before you close it out.

### 5.3 Cascade exhaustion spike — the handoff queue filling faster than it drains

**Severity: warning, escalating with queue age.**

`alert_kind=cascade_exhausted` is expected occasionally. A *spike* means a
platform is down, a credential broke, or routing config is wrong.

1. `/console/aggregators` — one platform at 0% success is a platform outage; all
   platforms degraded is more likely ours.
2. Check for `routing.configured_platform_has_no_adapter` in the logs. A slug in
   `routing_config` with no registered adapter is a config fault that silently
   shortens every cascade.
3. If it is one platform: disable it in `routing_config` (`is_enabled = false`).
   The cascade stops wasting budget on it and the remaining candidates get the
   whole 2.5s. No deploy needed.
4. Staff the handoff queue. Consumers are reaching people, which is the invariant
   working — but a queue nobody works is a dead end with extra steps.
5. Re-enable when the platform recovers, at the **end** of the priority list
   first.

### 5.4 LLM provider outage

**Severity: error.**

Symptom: `llm.request_failed` in the logs, understanding quality collapses, or
turns slow to the deadline.

The LLM sits behind a provider-agnostic interface (`services/llm.py`), and the
state machine — not the model — decides what happens next, so an LLM outage
degrades understanding rather than causing wrong bookings. No booking is confirmed
without an explicit consumer confirmation, and that transition is in
`core/conversation/states.py`, not in a prompt.

1. Confirm it is the provider, not us: check their status page and the
   `latency_ms` on recent `llm.*` audit entries.
2. If it is a sustained outage, set `MAWJOOD_LLM_PROVIDER=deterministic` and
   restart. The rule-based understudy handles simple, well-formed requests and
   sends the rest to handoff. It is markedly worse and it is much better than
   nothing.
3. Announce to whoever is staffing handoff that volume is about to rise.
4. Switch back and restart when the provider recovers.

Changing provider entirely is configuration, not a refactor (`CLAUDE.md`
decision 6) — but do not do it mid-incident against an unfamiliar API.

### 5.5 Database failover or outage

**Severity: critical.**

`/readyz` returns 503 and the orchestrator stops routing traffic to the instance.
`/healthz` stays green on purpose — a liveness probe that fails on a database
outage causes healthy instances to be killed and turns a database incident into an
availability incident.

1. Confirm with the provider. Managed failover is usually 30–90 seconds.
2. Inbound webhooks during the outage: the BSP retries. Some messages will be
   delayed, not lost. Do not attempt manual replay.
3. After recovery, check `/console/health` for stalled conversations and
   `scheduled_notifications` for anything now past its grace window
   (`MAWJOOD_NOTIFY_GRACE_HOURS`, default 2h). Notifications past grace are
   skipped by design — a reminder for an appointment that already started is worse
   than silence.
4. If the outage exceeded the grace window, tell ops which reminders did not go.

### 5.6 Slow turns / latency regression

Median turn latency must stay under 3s.

**Check the pool first.** Load testing found this to be the bottleneck, not the
cascade: at concurrency 15 on the degraded path, the shipped default pool (5 + 5)
gave p95 of 3.83s; raising it to 20 + 10 gave 2.21s with no other change. If
someone deployed with `.env.example` defaults, that is your answer.

```bash
MAWJOOD_DATABASE_POOL_SIZE=20
MAWJOOD_DATABASE_MAX_OVERFLOW=10
```

Then, in order: LLM latency (`llm.*` audit entries — the most likely production
regression, and the one the published load numbers do not cover), aggregator
latency (`/console/aggregators`, median and p95 per platform), and finally
database query time.

Remember what the deadline budget already does for you: at 2.5s the turn emits a
holding pivot and finishes the cascade in the background. A slow cascade shows up
as *more holding messages*, not as a consumer waiting. If holding-message volume
is rising, that is your early warning.

### 5.7 `notification_backlog` — scheduled messages piling up

Expected non-zero until Meta approves templates. **A rising number is the signal.**

Outside WhatsApp's 24-hour window, with no approved template, a scheduled message
is *held* — retried, never silently dropped, never recorded as sent. That is the
correct behaviour and it produces a backlog that only template approval clears.

1. Check `MAWJOOD_APPROVED_TEMPLATES`. Empty means nothing is approved yet.
2. When a template is approved, add its name to that list. The out-of-window path
   opens with no code change and no deploy — template names are configuration for
   exactly this reason.
3. If templates *are* approved and the backlog is still growing, the scheduler is
   not running. Check the cron in `DEPLOY.md` §6.

### 5.8 Suspected webhook forgery

Symptoms: `webhook.signature_invalid` at volume, or conversations from numbers
that never messaged.

1. **Do not** set `MAWJOOD_BSP_REQUIRE_SIGNATURE=false` to "see what is coming
   in". Production refuses to start with it false, and that refusal is load
   bearing.
2. Rate limiting runs *before* the signature check specifically so forged traffic
   cannot make us do HMAC work for free. Confirm it is engaging
   (`ratelimit.rejected` in the logs).
3. Rotate `MAWJOOD_BSP_WEBHOOK_SECRET` per `KEYS.md` §2. Set it at the provider
   and here in the same window — legitimate inbound is rejected in between, so
   pick a quiet hour.
4. If any forged message reached a consumer, that is a data incident: `PDPL.md`
   §9.

---

## 6. Routine procedures

| Task | Command | Cadence |
|---|---|---|
| Retention sweep | `uv run python tools/pdpl.py retention` | Daily, cron |
| Backup | `scripts/backup.sh --dir /var/backups/mawjood` | Daily 02:30 GST |
| Notification scheduler | `python -m mawjood.core.notifications.scheduler` | Every 5 min, **one host** |
| Dependency audit | `pip-audit` + `uv run python tools/licences.py` | Monthly and before releases |
| Restore drill | `scripts/restore.sh --into mawjood_restore_check` | Quarterly |
| Credential rotation | `KEYS.md` §3 | Per schedule |

Run the retention sweep with `--dry-run` first on any environment where it has
never run. It reports what it *would* remove without removing it, which is how you
find out that a misconfigured `MAWJOOD_DATA_RETENTION_DAYS` was about to delete a
year of consumers.

---

## 7. Consumer data deletion (UAE PDPL)

A consumer asks to be forgotten. This is the procedure, and it produces evidence.

```bash
# What would this remove? Deletes nothing.
uv run python tools/pdpl.py plan --wa-id 971501234567

# Do it.
uv run python tools/pdpl.py erase --wa-id 971501234567 --confirm --json > receipt.json
```

`--confirm` is required because erasure is irreversible and reaches the
append-only audit log. It should not be one typo away from happening.

**Check `plan` before you run `erase`.** It surfaces `live_bookings` — bookings
that have not happened yet. Erasing someone with a confirmed appointment tomorrow
leaves a venue expecting a person nobody can now identify. Cancel the booking with
the consumer first, or tell them their data goes after the appointment.

The receipt records a salted SHA-256 hash of the identifier — not the number
itself, because a deletion record containing the number is not a deletion — plus
per-table before and after counts. File it against the request.

**What it reaches:** every table carrying that consumer, including `audit_log`,
`messages`, `consents`, `attribution`, `feedback`, `scheduled_notifications` and
`handoff_queue`. Consent rows die with the data they authorise — a consent record
is itself personal data, and retaining it after everything it evidenced is gone
holds personal data with nothing left to justify it.

**What it does not reach:** backups. Those age out within 30 days. Say so to the
consumer — the honest answer is "removed from all live systems today, and from
backups within 30 days". Do not restore an old backup into production without
re-running erasure for everyone erased since it was taken.

Full detail, including the residue check that proves it worked, is in `PDPL.md`
§6–7.

---

## 8. Database users

Two roles, separated by privilege (`CLAUDE.md` §11):

- **Application user** (`mawjood_app`) — least privilege. DML on application
  tables only. No DDL, no `CREATE`, no `DROP`.
- **Migration user** (`mawjood_migrate`) — DDL rights, used exclusively by
  `alembic upgrade`, never by the running application.

```bash
psql "$MIGRATION_DSN" \
  -v app_role=mawjood_app \
  -v migration_role=mawjood_migrate \
  -f scripts/grants.sql
```

Verified live: the application role cannot `CREATE`, cannot `DROP`, and cannot
`UPDATE` or `DELETE` from `audit_log`. That last one is a **database trigger**,
not a grant — so it survives someone mistakenly widening the role's privileges
later. `UPDATE` on `audit_log` is refused outright; `DELETE` requires a
transaction that has explicitly set `mawjood.allow_purge`, which only the PDPL
erasure path and the retention sweep do.

### Health and readiness

- `GET /healthz` — liveness. No dependencies. Never fails because the database is
  down, so an outage does not cause an orchestrator to kill healthy instances.
- `GET /readyz` — readiness. Probes PostgreSQL and returns **503** when it is
  unreachable, with the failing check named in the response body, plus the region
  and data residency so a monitor pointed at the wrong region can tell.

```console
$ curl -s localhost:8000/readyz
{"status":"ready","checks":{"database":{"healthy":true,"latency_ms":9.68,"error":null}}}
```
