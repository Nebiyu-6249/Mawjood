# KEYS

Every credential Mawjood needs, and every third party that processes consumer
data. This file names things; it **never contains a value**.

> **Status: stub.** Populated as each integration lands. Phase 4 completes it as
> the processor register.

## Rules

1. Secrets come from the environment or a managed secret store. Never from code,
   never from a committed file. `.env` is gitignored; `.env.example` holds only
   `CHANGEME` placeholders.
2. `alembic.ini` keeps `sqlalchemy.url` blank on purpose — it is committed, and a
   DSN in it would be a committed credential.
3. A pre-commit `gitleaks` scan plus a CI secret-scan job block accidental
   commits. See `.pre-commit-config.yaml` and `.gitleaks.toml`.
4. Consumer data is **never** used to train models and never leaves the declared
   processors below.

## Credentials

| Key | Purpose | Phase | Held |
|---|---|---|---|
| `MAWJOOD_DATABASE_URL` | Application database, least-privilege user | 0 | ✅ local |
| *(migration DB user)* | DDL rights, `alembic upgrade` only | 1 | ⬜ |
| `MAWJOOD_SENTRY_DSN` | Error reporting | 0 | ⬜ optional |
| `MAWJOOD_OPENAI_API_KEY` | NLU and slot filling | 2 | ⬜ **not provisioned** |
| Zenoti API credentials | Salon/spa bookings, per merchant | 2 | ⬜ **sandbox not provisioned** |
| `MAWJOOD_BSP_WEBHOOK_SECRET` | Inbound webhook HMAC verification. **Required in production** — a Settings validator refuses to start without it. | 1 | ⬜ **not provisioned** |
| `MAWJOOD_BSP_VERIFY_TOKEN` | Provider subscription handshake | 1 | ⬜ **not provisioned** |
| `MAWJOOD_BSP_API_KEY` | Outbound send. Until set, replies persist as `queued` and nothing is delivered. | 1 | ⬜ **not provisioned** |
| Deliveroo API credentials | Food delivery, merchant-side | 3 | ⬜ **not provisioned** |

Merchant-level aggregator credentials are **not** environment variables. They will
live in a `merchant_credentials` table as references into the secret store,
resolved just-in-time and handed to adapters in `CallContext`; adapters never read
the secret store themselves. See `CLAUDE.md` section 4 and decision 1.

That table is **deferred to Phase 2**, when the adapter layer that consumes it
arrives. Phase 1 ships `routing_config` with `platform_slug` as free text and no
foreign key, which is all the routing order needs.

## Declared data processors

To be completed as each is contracted. For every entry: what data it sees, where
it processes it, the retention setting, and the no-training term.

| Processor | Data | Region | Retention | No-training term |
|---|---|---|---|---|
| *(LLM provider)* | Consumer message text | TBD | TBD | Required before Phase 2 ships |
| *(WhatsApp BSP)* | Phone number, message content | TBD | TBD | TBD |
| *(Aggregators)* | Name, phone, booking details | TBD | TBD | TBD |
| Sentry | Error context, no PII (`send_default_pii=False`) | TBD | TBD | n/a |

## Outstanding access

Nothing is provisioned yet. Three applications have external lead time and sit on
the critical path — see the top of `PLAN.md`:

1. Meta / WhatsApp message template approval (longest).
2. WhatsApp BSP account and number.
3. Zenoti developer sandbox, then Deliveroo.
