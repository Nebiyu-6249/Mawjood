# Mawjood

**موجود — "it's available"**

A WhatsApp AI booking assistant for the UAE market. One WhatsApp number is the single
entry point: a consumer sends a casual message ("need a haircut in Marina tomorrow
evening"), Mawjood works out what they mean, asks for anything missing, takes an explicit
confirmation, books through a third-party aggregator platform, and confirms back.

## The invariant

**No empty shelves.** A consumer is never told there are no vendors, no availability, no
options, or that something failed. When an aggregator has nothing, errors, or times out,
the router advances to the next one; when the list is exhausted, the conversation goes to
a human — and the consumer sees a graceful pivot, never an apology for an empty platform.

This is treated as a safety property, not a feature. It has its own test suite and it
gates the build. See [`CLAUDE.md`](CLAUDE.md) for how it is mechanically enforced.

## Getting started

```bash
cp .env.example .env          # fill in the blanks; no secrets are committed
docker compose up             # app + postgres
curl -s localhost:8000/readyz # honest readiness: 200 ready / 503 not ready
```

Without Docker:

```bash
uv venv --python 3.12
uv pip install -e ".[dev]"
uv run uvicorn mawjood.main:app --reload
```

## Working on it

`make` on its own lists every target.

| Purpose | Command |
|---|---|
| Everything CI runs | `make check` |
| Test (no network, ever) | `make test` |
| Test against a live PostgreSQL | `make test-integration` |
| Lint + format check | `make lint` |
| Typecheck (strict on `mawjood/core/`) | `make typecheck` |
| Secret scan and the other hooks | `make secrets-scan` |
| Migrate | `make migrate` |
| Run locally | `make run` |
| Chat with Mawjood, no credentials needed | `make simulate` |
| Seed the tenant and routing config | `make seed` |
| Start/stop the local stack | `make up` / `make down` |

Install the git hooks once with `make hooks`.

## Documentation

| File | What's in it |
|---|---|
| [`CLAUDE.md`](CLAUDE.md) | The invariant, the adapter contract, settled decisions, the working agreement. **Read this first.** |
| [`PLAN.md`](PLAN.md) | Five phases, each with deliverables and a testable definition of done. |

### Operator pack

| File | What's in it |
|---|---|
| [`docs/ADD_AN_AGGREGATOR.md`](docs/ADD_AN_AGGREGATOR.md) | Building one new adapter, end to end. **The most useful page here** — every gated integration lands after launch. |
| [`docs/DEPLOY.md`](docs/DEPLOY.md) | Clean machine to running, plus sizing. |
| [`docs/RUNBOOK.md`](docs/RUNBOOK.md) | On-call basics, incident playbooks, backup and the restore drill. |
| [`docs/KEYS.md`](docs/KEYS.md) | Every credential — where it lives, who owns it, how to rotate it — and every declared processor. |
| [`docs/LICENCES.md`](docs/LICENCES.md) | Open-source licence inventory, runtime and development split. |

### Evidence and decisions

| File | What's in it |
|---|---|
| [`docs/ACCEPTANCE.md`](docs/ACCEPTANCE.md) | One row per capability: how it was tested, the result, the evidence. Real latency numbers, including the one that missed. |
| [`docs/PDPL.md`](docs/PDPL.md) | UAE PDPL compliance, written for a reviewer. Consent, erasure, retention, residency, known gaps. |
| [`docs/SCHEMA.md`](docs/SCHEMA.md) | The tables, with a diagram and the reasoning behind the non-obvious choices. |
| [`docs/INTEGRATION_NOTES.md`](docs/INTEGRATION_NOTES.md) | Per-provider API facts recorded from live docs, with confidence levels — and what is blocked. |
| [`docs/RETROSPECTIVE.md`](docs/RETROSPECTIVE.md) | What I would do differently with more time. |
| [`docs/evidence/`](docs/evidence/) | Raw output backing every number claimed above. |

## Status

**Phases 0–4 complete, except for anything needing a credential.** The cascade,
the conversation state machine, notifications, the read-only ops console, the
PDPL deletion and retention paths, backups with a performed restore drill,
monitoring and the security pass all ship and are tested. 981 tests, ruff and
mypy clean.

**No live integration exists.** Four vendor documentation hosts — Meta,
360dialog, Zenoti and Deliveroo — return HTTP 403 on every path, so per
`CLAUDE.md` section 10 no endpoints were invented and those adapters are
documented stubs. There is no WhatsApp account, no aggregator sandbox and no LLM
key. Everything is proven against fakes.

That is the honest position, and it is what `docs/ACCEPTANCE.md` says row by row.

```bash
make seed && make simulate      # book a haircut in Marina, no credentials needed
make seed-all-down && make simulate   # watch the invariant hold with everything down
```
