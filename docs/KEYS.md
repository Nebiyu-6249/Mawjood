# KEYS

Every credential Mawjood needs, where it lives, who owns it, and how to rotate
it. Plus every third party that processes consumer data.

**This file names things. It never contains a value.** If you find one here,
treat it as leaked: rotate it, then remove it from git history with
`git filter-repo`, not by deleting the line in a new commit.

---

## 1. The rules

1. Secrets come from the environment or a managed secret store. Never from code,
   never from a committed file. `.env` is gitignored; `.env.example` holds only
   `CHANGEME` placeholders.
2. `alembic.ini` keeps `sqlalchemy.url` blank on purpose — it is committed, and a
   DSN in it would be a committed credential. `env.py` injects the URL from
   settings.
3. A pre-commit `gitleaks` scan blocks accidental commits
   (`.pre-commit-config.yaml`, `.gitleaks.toml`). The **full git history** has
   been scanned clean — see `docs/evidence/gitleaks-history.json` — and the scan
   has been proven able to fail by planting a real-shaped key and watching it
   catch it.
4. Merchant-level aggregator credentials are **not** environment variables in the
   application's own namespace. They are referenced by `secret_ref` on the
   `merchant_credentials` row, resolved just-in-time by the router, and handed to
   the adapter in `CallContext`. Adapters never read the secret store.
5. Consumer data is **never** used to train models and never leaves the declared
   processors in §4.

---

## 2. The credential register

`Owner` is a role, not a person — fill in names for your operating entity before
launch. Nothing below is provisioned today; the `Status` column is honest about
that.

### Application

| Key | What it is | Where it lives | Owner | Rotation | Status |
|---|---|---|---|---|---|
| `MAWJOOD_DATABASE_URL` | Application DSN, least-privilege role (`mawjood_app`) | Secret store / deploy env | Platform | Change the role password, update the secret, restart. No downtime with a rolling restart. | ✅ local only |
| *(migration DSN)* | DDL-capable role (`mawjood_migrate`). Used only by `alembic upgrade`, never by the running app. | Operator's hands / CI secret | Platform | As above. Rotate on any change of who can deploy. | ⬜ |
| `MAWJOOD_CONSOLE_PASSWORD` | Ops console login. **The session cookie key is derived from it**, so rotating it invalidates every outstanding session — which is the point. | Secret store | Ops lead | Change the value, restart. Everyone signs in again. Rotate on any team change. | ⬜ |
| `MAWJOOD_SENTRY_DSN` | Error reporting | Secret store | Platform | Issue a new DSN in Sentry, swap, revoke the old. | ⬜ optional |

### WhatsApp (BSP)

| Key | What it is | Where it lives | Owner | Rotation | Status |
|---|---|---|---|---|---|
| `MAWJOOD_BSP_WEBHOOK_SECRET` | HMAC key verifying inbound webhooks. **Production refuses to start without it.** | Secret store | Platform | Set the new secret at the provider *and* here in the same window — inbound is rejected in between. Do it during a quiet hour. | ⬜ **not provisioned** |
| `MAWJOOD_BSP_VERIFY_TOKEN` | Echoed during the provider's subscription handshake. A value you choose. | Secret store | Platform | Change here, then re-verify the subscription in the provider console. | ⬜ **not provisioned** |
| `MAWJOOD_BSP_API_KEY` | Outbound send. Until it is set, replies persist as `queued` and nothing is delivered. | Secret store | Platform | Issue new key at the provider, swap, revoke old. Queued messages drain on the next send. | ⬜ **not provisioned** |
| `MAWJOOD_BSP_API_BASE` | Provider send endpoint. Not secret, but provider-specific. | Deploy env | Platform | n/a | ⬜ |

### LLM

| Key | What it is | Where it lives | Owner | Rotation | Status |
|---|---|---|---|---|---|
| `MAWJOOD_OPENAI_API_KEY` | NLU and slot filling | Secret store | Platform | Create a second key, deploy, delete the first. Zero downtime — OpenAI allows concurrent keys. | ⬜ **not provisioned** |

With no key set, `MAWJOOD_LLM_PROVIDER=deterministic` runs a rule-based
understudy. It needs no credentials and is what lets the test suite and
`chat_sim.py` run for free. It is not a production NLU.

### Aggregators — per merchant, not per platform

Aggregator APIs are merchant-side (`CLAUDE.md` decision 1): each salon has its own
centre ids and key. The chain is:

```
merchant_credentials.secret_ref  →  SecretStore.get(ref)  →  ctx.credentials
     "zenoti_marina_01"              env or managed store     Mapping[str, str]
```

With the env backend, `secret_ref = "zenoti_marina_01"` reads
`MAWJOOD_MERCHANT_ZENOTI_MARINA_01`, whose **value is a JSON object** of the
fields that merchant needs:

```bash
MAWJOOD_MERCHANT_ZENOTI_MARINA_01='{"api_key":"…","centre_id":"…"}'
```

The `MAWJOOD_MERCHANT_` prefix exists so a secret scan and an ops review can both
find them by pattern.

**Nothing secret is in the database.** `merchant_credentials` holds a *reference*
and the non-secret platform identifiers, so a database dump is not a credential
leak.

| Platform | Status | Rotation |
|---|---|---|
| Zenoti | ⬜ **sandbox not provisioned** — documentation unreachable, adapter is a stub | Per merchant: re-issue in that merchant's Zenoti account, update the secret ref's value, no restart needed (resolved per call). |
| Deliveroo | ⬜ **not provisioned** — and see the note below | As above |
| OpenTable, Foodics, Booksy, Talabat, Careem | ⬜ partner-gated stubs | — |

> **Deliveroo will not become a booking integration by acquiring credentials.**
> Its Order API is merchant-side: it receives orders Deliveroo has already taken
> through its own apps, and does not place them. A partnership does not change
> that. Recorded per method in the adapter and in `INTEGRATION_NOTES.md` so
> nobody signs a contract expecting otherwise.

### Rotating a merchant credential without an outage

1. Add the new value under a **new** `secret_ref` (`zenoti_marina_02`).
2. Update the `merchant_credentials` row to point at it.
3. Watch the routing trace for that merchant — the next call resolves the new
   ref. Credentials are resolved per call, so there is no cache to clear and no
   restart.
4. Revoke the old value at the platform, then delete the old ref.

If a credential is rejected upstream, the router marks that merchant
`credential_degraded_at`, alerts ops (`alert_kind=credential_degraded`), and
advances to the next candidate. The consumer sees nothing. **Nothing outside this
system will notice a degraded credential**, so the alert is the only signal —
make sure it is routed somewhere a person reads.

---

## 3. Rotation schedule

| Credential | Interval | Also rotate when |
|---|---|---|
| Console password | 90 days | Anyone with access leaves |
| Database roles | 180 days | Anyone with deploy access leaves; after any restore into a shared environment |
| BSP webhook secret | 180 days | Suspected webhook forgery |
| BSP API key | 180 days | Provider account access changes |
| OpenAI key | 90 days | — |
| Merchant credentials | Per platform policy | `credential_degraded` alert; merchant ownership changes |

Record the date of each rotation. An unrotated credential with no record is
indistinguishable from a forgotten one.

---

## 4. Declared data processors

Every third party that sees consumer data. `PDPL.md` §5 is the compliance-facing
version of this table; this one is for whoever administers the accounts.

| Processor | Data it sees | Region | Retention | No-training term |
|---|---|---|---|---|
| WhatsApp BSP (360dialog or Wati) | Phone number, full message content | **Must be contracted in a permitted region** | Provider policy — obtain in writing | Required in the contract |
| OpenAI | Message text sent for understanding. No phone number, no name. | API region per contract | **Zero-retention terms required before production use** | Required: no training on API data |
| Aggregators (per platform) | Consumer name, phone, booking details — **only after recorded consent** | Platform-dependent | Platform policy | Required in the partnership agreement |
| Sentry | Error context. `send_default_pii=False`; payload fields redacted by name; request bodies and stack-frame locals stripped. **Not a processor for message content.** | Configurable | Project setting | n/a |
| Cloud provider (TBD) | Everything at rest | GCC, EU or India — never US-only | Per §4 of `PDPL.md` | n/a |

**Three of these five rows are contract work, not engineering work**, and none of
them can be closed from inside this repository. They are the gate on production
use, and they should be started before the code is finished:

1. BSP contract with a permitted processing region.
2. OpenAI zero-retention / no-training terms in writing.
3. Each aggregator's data-processing terms, obtained with the API access.

---

## 5. Outstanding access

Nothing is provisioned. Three applications have external lead time and sit on the
critical path:

1. **Meta / WhatsApp message template approval** — longest. Until it lands,
   `MAWJOOD_APPROVED_TEMPLATES` stays empty, scheduled messages degrade to session
   messages inside the 24-hour window, and outside it they are held rather than
   sent. Submit early.
2. **WhatsApp BSP account and number.**
3. **Zenoti developer sandbox**, then Deliveroo. Note that Zenoti's public
   documentation returned HTTP 403 on every path from the build environment
   (re-verified 2026-07-30), so the sandbox is needed for the contract itself, not
   just for testing. See `INTEGRATION_NOTES.md`.

---

## 6. If a key leaks

1. **Revoke first, investigate second.** A revoked key costs an outage; a live
   leaked key costs consumer data.
2. Rotate per §2, using the no-outage procedure where one exists.
3. Check the audit log for use you did not make. Every routing decision and API
   call is recorded with a correlation id.
4. If consumer data may have been reached, follow `PDPL.md` §9 — the UAE PDPL
   breach path, including the notification obligation.
5. Remove the value from git history with `git filter-repo`. A follow-up commit
   deleting the line does not remove it from history, and the history is what a
   scanner reads.
6. Record what happened and what changed so the same shape of mistake is harder
   next time.
