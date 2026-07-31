# DEPLOY

Clean machine to a running Mawjood.

Written for someone who has the repository, a server, and no prior context.
Follow it top to bottom and you will have a service taking WhatsApp webhooks.

> **Scope note.** Mawjood has never run against live WhatsApp or a live
> aggregator — no credentials exist yet (see `KEYS.md`). Everything below is
> exercised locally and in the test suite; §8 marks the steps that are the first
> ones to be performed for real, so you know where you are on new ground.

---

## Contents

1. [What you are deploying](#1-what-you-are-deploying)
2. [Local, in five minutes](#2-local-in-five-minutes)
3. [Provisioning a server](#3-provisioning-a-server)
4. [The database](#4-the-database)
5. [Configuration](#5-configuration)
6. [Running the app](#6-running-the-app)
7. [Sizing — read this before you scale](#7-sizing--read-this-before-you-scale)
8. [Connecting WhatsApp](#8-connecting-whatsapp)
9. [Going live](#9-going-live)
10. [Upgrades and rollback](#10-upgrades-and-rollback)

---

## 1. What you are deploying

One stateless Python process and one PostgreSQL database. That is the whole
system.

- **Image**: a plain OCI image from the repository `Dockerfile`. Python 3.12
  slim, non-root (UID 10001, no login shell), no cloud SDKs, no baked
  configuration, no assumed region. It runs unchanged wherever a container runs.
- **State**: PostgreSQL 16. Everything persistent lives here — conversations,
  bookings, the audit log, scheduled notifications. The app holds nothing across
  restarts by design (§7 has the one exception).
- **Egress**: the WhatsApp BSP, the LLM provider, and each aggregator. All over
  TLS, all outbound only.
- **Ingress**: one webhook endpoint and the ops console. Nothing else needs to be
  reachable from the internet.

**Residency is enforced in code, not documentation.** `MAWJOOD_DATA_RESIDENCY`
accepts `gcc`, `eu` or `india` — there is no US member, so a US-only deployment
is not representable — and `MAWJOOD_REGION` is rejected at startup if it looks
like a US region code (`us-east-1`, `eastus`, `westus3`,
`northamerica-northeast1`, …). Covered by `tests/test_config.py`. Pick a region
in one of the three permitted areas before you start; `me-central-1` is the
obvious default for the UAE.

**The target cloud is deliberately not chosen here.** Nothing in the codebase
depends on one. Any provider offering a container runtime and managed PostgreSQL
in a permitted region works, and the reasoning is in `CLAUDE.md` decision 5:
portability was worth more than provider-specific convenience for a product whose
operator is not yet known.

---

## 2. Local, in five minutes

Needs Docker and `uv`. No WhatsApp credentials, no aggregator access.

```bash
git clone <repo> mawjood && cd mawjood
cp .env.example .env          # edit POSTGRES_PASSWORD and MAWJOOD_CONSOLE_PASSWORD
make up                       # app + postgres, migrations and seed on start
curl -s localhost:8000/readyz
```

Then talk to it:

```bash
make simulate                 # terminal chat, same pipeline as the webhook
```

`chat_sim.py` drives **the same core handlers as the WhatsApp webhook** — a
different transport, never a second code path. If a conversation works here it
works on WhatsApp, modulo the BSP itself.

Two demos worth running before you trust anything:

```bash
make seed-all-down            # every aggregator configured to fail
make simulate                 # the consumer still never hears "no availability"
```

```bash
make check                    # lint, typecheck, full suite — what CI runs
```

---

## 3. Provisioning a server

Minimum viable production: **2 vCPU, 4 GB RAM** for the app, plus managed
PostgreSQL. Mawjood is I/O-bound — it waits on aggregators, the LLM and the BSP —
so cores matter less than the connection pool (§7).

```bash
# Debian 12 / Ubuntu 24.04
apt-get update && apt-get install -y ca-certificates curl git
install -m 0755 -d /etc/apt/keyrings
curl -fsSL https://download.docker.com/linux/debian/gpg -o /etc/apt/keyrings/docker.asc
echo "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/docker.asc] \
  https://download.docker.com/linux/debian $(. /etc/os-release && echo $VERSION_CODENAME) stable" \
  > /etc/apt/sources.list.d/docker.list
apt-get update && apt-get install -y docker-ce docker-ce-cli containerd.io docker-compose-plugin
```

Firewall: allow 443 in, everything else closed. The app listens on 8000 behind a
reverse proxy; it must never be exposed directly.

```bash
ufw default deny incoming && ufw allow 22 && ufw allow 443 && ufw enable
```

**TLS terminates at the proxy.** Caddy is the least effort — two lines and
certificates renew themselves:

```caddyfile
mawjood.example.com {
    reverse_proxy 127.0.0.1:8000
}
```

With nginx, set `proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;`
and read §7 on the rate limiter before you put a second proxy in front of it.

---

## 4. The database

Managed PostgreSQL 16, in the same region as the app, with TLS required and
encryption at rest enabled. Take the provider's defaults for both and verify
them — `PDPL.md` lists encryption at rest as unverified precisely because it is a
provider setting nobody has checked yet.

### Two roles, not one

`CLAUDE.md` §11 requires a least-privilege application user separate from the
migration user. `scripts/grants.sql` provisions both:

```bash
psql "$MIGRATION_DSN" \
  -v app_role=mawjood_app \
  -v migration_role=mawjood_migrate \
  -f scripts/grants.sql
```

Afterwards the application role can `SELECT`/`INSERT`/`UPDATE`/`DELETE` on
application tables and nothing else. Verified live: it cannot `CREATE`, cannot
`DROP`, and cannot `UPDATE` or `DELETE` from `audit_log` — that last one is a
database trigger, not a grant, so it survives a mistaken grant later.

### Migrations are a deliberate step

```bash
docker run --rm --env-file .env \
  -e MAWJOOD_DATABASE_URL="$MIGRATION_DSN" \
  mawjood:<tag> alembic upgrade head
```

`MAWJOOD_RUN_MIGRATIONS_ON_START` defaults to `false` and should stay false
outside local development. Migrations that run on boot race each other across
instances and turn a rollout into an outage.

`alembic.ini` keeps `sqlalchemy.url` blank on purpose — it is committed, and a
DSN in it would be a committed credential. `env.py` injects the URL from
settings.

### Backups

Set up the daily backup on day one, not after the first incident:

```cron
30 22 * * * MAWJOOD_DATABASE_URL=... /srv/mawjood/scripts/backup.sh --dir /var/backups/mawjood
```

02:30 Gulf time. 30-day retention. `RUNBOOK.md` has the restore procedure and the
drill results — **run the drill yourself against production-sized data before
launch**; the recorded drill was at tens of rows and establishes correctness, not
an RTO.

---

## 5. Configuration

Everything comes from the environment with the `MAWJOOD_` prefix. `.env.example`
is the complete list and a test asserts it stays complete — if a setting exists
in `config.py` and not in `.env.example`, the suite fails.

The settings that decide whether a deployment is sane:

| Setting | Production value | Why |
|---|---|---|
| `MAWJOOD_ENVIRONMENT` | `prod` | Turns on the refusals below. |
| `MAWJOOD_DEBUG` | `false` | — |
| `MAWJOOD_ENABLE_FAKE_ADAPTERS` | `false` | **Refused outright when environment is `prod`.** A fake in production confirms bookings that do not exist — the worst failure this system could have. |
| `MAWJOOD_BSP_REQUIRE_SIGNATURE` | `true` | Refused if false in prod. Unsigned webhooks are anyone's webhooks. |
| `MAWJOOD_BSP_WEBHOOK_SECRET` | set | Startup fails without it in prod. |
| `MAWJOOD_CONSOLE_PASSWORD` | set, strong | Startup fails without it in prod. The console exposes every consumer's transcript. |
| `MAWJOOD_DATA_RESIDENCY` | `gcc` \| `eu` \| `india` | No US member exists. |
| `MAWJOOD_REGION` | a non-US region code | US-looking values rejected at startup. |
| `MAWJOOD_LOG_FORMAT` | `json` | Structured logs are the audit trail's neighbour. |
| `MAWJOOD_LLM_PROVIDER` | `openai` once a key exists | `deterministic` is the credential-free understudy; it ships as the default so the suite and `chat_sim` run for free. |
| `MAWJOOD_DATABASE_POOL_SIZE` | **20** | See §7. The shipped default of 5 is a local-development value. |
| `MAWJOOD_DATABASE_MAX_OVERFLOW` | **10** | See §7. |

These refusals are not advisory — they are `Settings` validators, and the process
will not start. That is deliberate: a misconfigured Mawjood that starts is worse
than one that does not.

**Secrets never land in a file on the server.** The env backend
(`MAWJOOD_SECRETS_BACKEND=env`) is what exists today;
`mawjood/services/secrets.py` is an interface with a documented seam for a
managed store, and swapping it is one class with no caller changes. If your
provider offers a secret manager, write that class before launch — an `.env` file
on disk is the weakest link in this deployment.

---

## 6. Running the app

```bash
docker build -t mawjood:$(git rev-parse --short HEAD) .
docker run -d --name mawjood --restart unless-stopped \
  --env-file /etc/mawjood/env -p 127.0.0.1:8000:8000 \
  mawjood:<tag>
```

Bind to `127.0.0.1`. The proxy reaches it; the internet does not.

The image runs `uvicorn mawjood.main:create_app --factory`. Factory mode is
deliberate: `mawjood.main` has no module-level `app`, so importing it has no side
effects and a test or a CLI can import the module without building an
application.

### Probes

| Endpoint | Meaning | Configure as |
|---|---|---|
| `/healthz` | the process is alive; touches nothing | liveness, 30s |
| `/readyz` | this instance should take traffic; probes PostgreSQL | readiness, 30s |

`/healthz` deliberately stays green when the database is down. A liveness probe
that fails on a database outage causes an orchestrator to kill healthy instances
and turns a database incident into an availability incident. Point your external
monitor (UptimeRobot or similar) at both — they are unauthenticated for exactly
that reason.

### The scheduler

Reminders, follow-ups and satisfaction requests are **rows, not timers** — the
schedule survives a restart because it was never in memory. Something has to walk
those rows:

```cron
*/5 * * * * docker exec mawjood python -m mawjood.core.notifications.scheduler
```

Run this on **one** host only. The `UniqueConstraint("booking_id", "kind")` means
a double run cannot double-send, but two schedulers racing is wasted work and
confusing logs.

---

## 7. Sizing — read this before you scale

### The connection pool is the bottleneck, not the cascade

This is the single most useful thing the load testing found, and it is worth
stating bluntly because it looks like an application problem and is not.

At concurrency 15 on the degraded path (every aggregator slow, full cascade,
900 ms upstreams) with the **shipped default pool of 5 + 5 overflow**, p95 turn
latency was **3.83 s** — a clear miss against the sub-3s target. The cascade was
not slow. Turns were queuing for a database connection while the cascade held one
open across its upstream calls.

Raising the pool to **20 + 10** brought the same workload to **2.21 s p95**, with
no other change.

```bash
MAWJOOD_DATABASE_POOL_SIZE=20
MAWJOOD_DATABASE_MAX_OVERFLOW=10
```

`.env.example` ships 5 + 5 because that is right for a laptop running the suite
next to a browser. **It is wrong for production.** Set the pool before you tune
anything else, and make sure PostgreSQL's `max_connections` covers
`(pool_size + max_overflow) × instances` with headroom — 30 per instance means
four instances need 120 connections plus whatever your migrations and console
sessions use.

Published numbers, from `docs/evidence/load-*.json`:

| Scenario | Upstream latency | Turns | p50 | p95 | p99 |
|---|---|---|---|---|---|
| Happy path, 200 conversations / 25 concurrent | 0 ms | 800 | 0.345 s | 0.491 s | 0.575 s |
| Happy path, 100 / 20 | 400 ms | 400 | 0.375 s | 0.695 s | 0.712 s |
| Cascade exhausted, 60 / 15 | 400 ms | 180 | 0.182 s | 1.153 s | 1.156 s |
| Cascade exhausted, 60 / 15 | 900 ms | 180 | 0.182 s | 2.172 s | 2.176 s |

`ACCEPTANCE.md` states what these numbers exclude — most importantly a real LLM
call, which is the most likely route to missing the budget in production. Re-run
the harness against your own deployment once the OpenAI key exists:

```bash
uv run python tools/loadtest.py --scenario happy --conversations 200 --concurrency 25
```

### Scaling out

The app is stateless and horizontal scaling works, with two caveats:

1. **Rate-limiter buckets are per process.** `MAWJOOD_WEBHOOK_RATE_PER_MINUTE`
   and `MAWJOOD_CONSUMER_RATE_PER_MINUTE` are enforced in memory, so two
   instances mean twice the effective rate. Either divide the configured values
   by the instance count, or move the limiter to a shared store before scaling —
   the interface in `mawjood/api/ratelimit.py` is small enough to make that a
   contained change.
2. **Run the notification scheduler on one host** (§6).

Nothing else in the process holds state between requests.

### If a proxy sits in front

`client_address()` reads the **last** hop of `X-Forwarded-For`, not the first.
Taking the first entry — the common mistake — lets anyone choose their own rate
limit key by sending the header themselves. If you add a second proxy layer,
check that assumption still holds or the limiter will bucket everyone together.

---

## 8. Connecting WhatsApp

**This section has never been performed against a live account.** It is written
from the integration design, and the BSP documentation was not reachable from the
build environment (`INTEGRATION_NOTES.md`). Expect to correct it the first time
you do it, and please do correct it here.

Three external dependencies, each with lead time, and they are on the critical
path:

1. A WhatsApp BSP account and number (360dialog or Wati — both are supported
   behind one interface).
2. Meta message template approval. **The longest.** Submit early.
3. Aggregator sandbox access.

Configure:

```bash
MAWJOOD_BSP_PROVIDER=360dialog
MAWJOOD_BSP_WEBHOOK_SECRET=<from the provider>
MAWJOOD_BSP_VERIFY_TOKEN=<a value you choose, echoed at subscription>
MAWJOOD_BSP_API_BASE=<provider send endpoint>
MAWJOOD_BSP_API_KEY=<provider key>
```

Point the provider's webhook at `https://your-host/webhooks/whatsapp`. The
subscription handshake is a `GET` that echoes `MAWJOOD_BSP_VERIFY_TOKEN`; inbound
messages are `POST`s verified by HMAC against `MAWJOOD_BSP_WEBHOOK_SECRET`.

**Until templates are approved**, `MAWJOOD_APPROVED_TEMPLATES` stays empty and
scheduled messages degrade gracefully: session messages inside WhatsApp's 24-hour
window, and outside it they are *held* — retried, never silently dropped, never
recorded as sent. Add an approved name to that list and the out-of-window path
opens with no code change and no deploy. Template names are configuration for
exactly this reason: a resubmission that needs a new name must not be an
engineering task.

Verify before announcing the number:

```bash
curl -s https://your-host/readyz          # 200, correct region
# send a WhatsApp message to the number, then:
docker logs mawjood | grep webhook.received
```

Watch `/console/trace/<conversation_id>` for the first few real conversations.
That page shows every aggregator tried, its outcome, its latency, why the engine
advanced, and exactly what the consumer saw. It is the fastest way to catch a
mapping mistake in a new adapter.

---

## 9. Going live

Work this list in order.

- [ ] Region is GCC, EU or India; `/readyz` reports the one you expect
- [ ] `MAWJOOD_ENVIRONMENT=prod`, fakes off, signature verification on
- [ ] Database roles split; `scripts/grants.sql` applied and spot-checked
- [ ] TLS enforced on the database connection, encryption at rest confirmed with
      the provider
- [ ] Pool set to 20 + 10; PostgreSQL `max_connections` sized for your instances
- [ ] Daily backup cron installed; **restore drill performed at production
      volume** and the real RTO recorded in `RUNBOOK.md`
- [ ] Sentry DSN set; alert rules written against the `alert_kind` tag
      (`RUNBOOK.md` has the table). `reconciliation_inconclusive` pages a human.
- [ ] External uptime monitor on `/healthz` and `/readyz`
- [ ] Notification scheduler cron on exactly one host
- [ ] Log shipping configured with 90-day retention
- [ ] Consent wording reviewed by a lawyer for the operating entity
      (`PDPL.md` §3 — this is the one compliance item code cannot close)
- [ ] `KEYS.md` filled in with owners and rotation dates
- [ ] Routing config seeded; every configured slug has a registered adapter
      (a missing one logs `routing.configured_platform_has_no_adapter`)
- [ ] A real conversation completed end to end and its routing trace read

---

## 10. Upgrades and rollback

```bash
docker build -t mawjood:$(git rev-parse --short HEAD) .
docker run --rm --env-file /etc/mawjood/env \
  -e MAWJOOD_DATABASE_URL="$MIGRATION_DSN" mawjood:<new> alembic upgrade head
docker stop mawjood && docker rm mawjood
docker run -d --name mawjood ... mawjood:<new>
curl -s localhost:8000/readyz
```

Migrate first, then deploy. Every migration so far is additive, which is what
makes that order safe — the old code keeps running against the new schema. If you
write a destructive migration, split it: deploy code that tolerates both shapes,
then drop the old one in a later release.

**Rollback** is the previous image tag. It works as long as the migration was
additive; if it was not, restore from backup into a fresh database and repoint
`MAWJOOD_DATABASE_URL` rather than trying to reverse a migration under pressure.

Tag images with the git SHA, and set `MAWJOOD_SERVICE_VERSION` to match — it tags
Sentry events, so an alert names the build it came from.
