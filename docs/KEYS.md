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
| `MAWJOOD_BSP_API_KEY` | WhatsApp send/receive | 3 | ⬜ **not provisioned** |
| `MAWJOOD_BSP_WEBHOOK_SECRET` | Inbound webhook signature check | 3 | ⬜ **not provisioned** |
| Deliveroo API credentials | Food delivery, merchant-side | 3 | ⬜ **not provisioned** |

Merchant-level aggregator credentials are **not** environment variables. They live
in the `merchant_credentials` table (Phase 1) as references into the secret store,
resolved just-in-time and handed to adapters in `CallContext`. Adapters never read
the secret store themselves. See `CLAUDE.md` section 4.

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
