# The no-empty-shelves suite

This directory is the invariant's test suite. It gates the build.

| Test | Asserts | Phase |
|---|---|---|
| Copy blocklist scan | No phrasebank entry, in any locale, contains failure language | 1 |
| Test-of-the-test | Planting a blocklisted phrase makes the scan fail | 1 |
| Every upstream down | Handoff plus a graceful pivot, zero blocklist words | 2 |
| Timeout on create | Reconciliation runs; exactly one successful create across all adapters | 2 |
| Inconclusive reconciliation | Human handoff, never a silent retry elsewhere | 2 |
| Deadline expiry | Holding pivot inside budget, cascade continues in background | 2 |
| Material change on failover | Re-confirmed before booking | 2 |
| UNSUPPORTED | Routes to handoff, never a synthesised success | 3 |

Tests here are written **before** the code they cover. See `CLAUDE.md` section 13.
