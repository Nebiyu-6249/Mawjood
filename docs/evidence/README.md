# Evidence

Raw machine-readable output backing the claims in `ACCEPTANCE.md`, `PDPL.md` and
`LICENCES.md`. Every file here was produced by a command in this repository, and
every command is named below so a reviewer can re-run it rather than take the
file on trust.

**None of it involves live WhatsApp traffic or a live aggregator.** No credentials
exist yet (`KEYS.md` §5). Everything is against fakes, the local stack, or the
real dependency tree.

| File | What it is | Regenerate with |
|---|---|---|
| `load-happy-0ms.json` | 200 conversations / 25 concurrent, instant upstreams. p50 0.345 s, p95 0.491 s. | `uv run python tools/loadtest.py --scenario happy --conversations 200 --concurrency 25 --upstream-ms 0` |
| `load-happy-400ms.json` | 100 / 20 with 400 ms upstreams. p50 0.375 s, p95 0.695 s. | `… --scenario happy --conversations 100 --concurrency 20 --upstream-ms 400` |
| `load-degraded-400ms.json` | Cascade exhausted every turn, 400 ms upstreams. p95 1.153 s. | `… --scenario degraded --conversations 60 --concurrency 15 --upstream-ms 400` |
| `load-degraded-900ms.json` | Cascade exhausted, 900 ms upstreams — the worst realistic case. p95 2.172 s. | `… --scenario degraded --conversations 60 --concurrency 15 --upstream-ms 900` |
| `dependency-audit.json` | `pip-audit` over the full runtime and development closure. | `uv run pip-audit --format json` |
| `gitleaks-history.json` | Secret scan over **all commits**, not just the working tree. Empty array = clean. | `gitleaks detect --source . --log-opts="--all" --report-format json` |
| `licences.json` | Open-source licence inventory, runtime and development split. | `uv run python tools/licences.py --json` |
| `erasure-receipt.json` | One PDPL erasure, with per-table before/after counts and a salted hash of the subject. | `uv run python tools/pdpl.py erase --wa-id … --confirm --json` |
| `retention-sweep.json` | One retention sweep: cutoffs, rows removed per table, consumers kept for future bookings. | `uv run python tools/pdpl.py retention --json` |

## Reading the latency files

`percentiles` are **nearest-rank, not interpolated** — every published figure is a
turn that actually happened, not an average of two that did. `turns` counts
complete consumer turns end to end through the pipeline, not HTTP requests.

The numbers exclude a WhatsApp round trip, a real LLM call and a real aggregator.
`ACCEPTANCE.md` states what that means; the short version is that the LLM is the
most likely route to missing the 3 s budget in production and none of these
figures include it.

## Reading the erasure receipt

The subject is recorded as a tenant-salted SHA-256 hash. A deletion record
containing the phone number it deleted is not a deletion, which is why the raw
identifier does not appear.

`before` and `after` are row counts per table. Every `after` is zero. `audit_log`
is included — erasure reaches the append-only log, which is the part most systems
quietly skip.
