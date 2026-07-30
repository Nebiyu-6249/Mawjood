# RUNBOOK

Operating Mawjood: routine procedures, incident response, backup and restore.

> **Status:** backup, restore and monitoring are operational as of Phase 3 and
> the restore drill below has been performed. The remaining sections are Phase 4.

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

### Database users

Two roles, separated by privilege:
- **Application user** — least privilege: DML on application tables only, no DDL.
- **Migration user** — DDL rights, used exclusively by `alembic upgrade`.

Provisioning SQL goes here in Phase 1 when the schema exists.

### Consumer data deletion (UAE PDPL) — Phase 4
The procedure for purging a consumer across every table *including logs*, and the
residue check that proves it worked.

### Incident response
- Aggregator credential failure (`AUTH_ERROR` on a merchant).
- Cascade exhaustion spike — the handoff queue filling faster than it drains.
- LLM provider outage.
- Database failover.

### Health and readiness
Already live as of Phase 0:
- `GET /healthz` — liveness. No dependencies. Never fails because the database is
  down, so an outage does not cause an orchestrator to kill healthy instances.
- `GET /readyz` — readiness. Probes PostgreSQL and returns **503** when it is
  unreachable, with the failing check named in the response body.

```console
$ curl -s localhost:8000/readyz
{"status":"ready","checks":{"database":{"healthy":true,"latency_ms":9.68,"error":null}}}
```
