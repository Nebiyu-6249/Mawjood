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
| Start/stop the local stack | `make up` / `make down` |

Install the git hooks once with `make hooks`. The full intended command set,
including `simulate` (Phase 2) and `seed` (Phase 3), is in
[`CLAUDE.md`](CLAUDE.md) section 12 — those targets arrive with the tools they wrap.

## Documentation

| File | What's in it |
|---|---|
| [`CLAUDE.md`](CLAUDE.md) | The invariant, the adapter contract, settled decisions, the working agreement. **Read this first.** |
| [`PLAN.md`](PLAN.md) | Five phases, each with deliverables and a testable definition of done. |
| `docs/RUNBOOK.md` | Operations, backup and the restore drill. |
| `docs/DEPLOY.md` | Deployment and region selection. |
| `docs/ACCEPTANCE.md` | Launch acceptance checklist. |
| `docs/KEYS.md` | Every credential and declared data processor. |
| `docs/INTEGRATION_NOTES.md` | Per-aggregator API facts, recorded from live docs. |

## Status

**Phase 0 (scaffold) complete.** The skeleton boots, `/readyz` tells the truth about
the database, logs are JSON with credentials redacted, and CI enforces lint, types,
tests and a secret scan. No aggregator, conversation or routing code exists yet —
that starts in Phase 1. See [`PLAN.md`](PLAN.md).
