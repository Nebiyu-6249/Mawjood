# RUNBOOK

Operating Mawjood: routine procedures, incident response, backup and restore.

> **Status: stub.** Phase 0 created this file so it has a home and the README can
> link to it. It is filled in during Phase 4 (hardening and launch readiness) and
> must not be treated as operational guidance until then.

## To be written

### Backup and restore — Phase 4
- Daily database backup, 30-day retention.
- **A restore drill that has actually been performed**, with the measured elapsed
  time recorded here. An untested backup is not a backup.

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
