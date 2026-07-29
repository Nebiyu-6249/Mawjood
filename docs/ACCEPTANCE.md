# ACCEPTANCE

The checklist that must be fully green before Mawjood takes real consumer traffic.

> **Status: stub, with Phase 0 items already checked.** Completed and executed
> against a deployed environment in Phase 4.

## The invariant — no empty shelves

- [ ] Every phrasebank entry, across every locale, passes the failure-language
      blocklist scan. *(Phase 1)*
- [ ] The blocklist scan provably fails when a bad phrase is planted. A scan that
      cannot fail is not a scan. *(Phase 1)*
- [ ] Consumer-facing copy is unforgeable: the send path accepts only a
      `RenderedMessage` from the phrasebank. *(Phase 1)*
- [ ] Every upstream down → human handoff with a graceful pivot, zero blocklist
      words. *(Phase 2)*
- [ ] `UNSUPPORTED` routes to handoff and never synthesises a success. *(Phase 3)*

## Correctness under failure

- [ ] Timeout on `create_booking` → reconciliation attempted → exactly one
      successful create across all adapters. *(Phase 2)*
- [ ] Inconclusive reconciliation → human handoff, never a silent retry
      elsewhere. *(Phase 2)*
- [ ] Deadline expiry → holding pivot inside budget, cascade continues in the
      background, follow-up delivered. *(Phase 2)*
- [ ] Failover that materially changes the offer → re-confirmed before booking.
      *(Phase 2)*
- [ ] No adapter raises into the router under fault injection. *(Phase 1)*

## Performance

- [ ] Median (p50) consumer response under 3 seconds, measured under load.
      *(Phase 4)*
- [ ] p95 recorded and accepted. *(Phase 4)*

## Compliance

- [ ] PDPL consent captured on first inbound message, with the exact wording
      shown and a timestamp. *(Phase 2)*
- [ ] Deletion path purges a consumer across every table including logs, with a
      residue scan proving it. *(Phase 4)*
- [ ] Configurable retention, and the retention job runs. *(Phase 4)*
- [x] Data residency is `gcc`, `eu` or `india`; US-only is not representable and a
      US region code is refused at startup. *(Phase 0)*
- [x] Zero hardcoded secrets; `.env.example` ships; pre-commit secret scan
      installed. *(Phase 0)*
- [ ] Encryption in transit and at rest, documented. *(Phase 4)*
- [ ] Least-privilege database user, separate from the migration user. *(Phase 4)*
- [x] Logs are structured JSON; credentials are redacted and PII is masked before
      reaching a handler. *(Phase 0)*
- [ ] 90-day log retention configured in the deployed environment. *(Phase 4)*
- [ ] `audit_log` row for every routing decision: what was tried, what came back,
      why it advanced, what the consumer saw. *(Phase 2)*
- [ ] Daily backup with 30-day retention, and a restore drill actually performed
      and timed. *(Phase 4)*
- [ ] Consumer data is not used to train models and does not leave the declared
      processors — confirmed in writing per processor. *(Phase 4)*

## Operability

- [x] `/healthz` is dependency-free; `/readyz` returns 503 when PostgreSQL is
      unreachable. *(Phase 0)*
- [x] CI enforces lint, typecheck, tests and the secret scan. *(Phase 0)*
- [x] No real network calls in tests, enforced mechanically. *(Phase 0)*
- [ ] Ops console shows conversations, bookings, the routing audit trail and the
      handoff queue. *(Phase 3)*
- [ ] Handoff round trip: queue → console reply → bot muted → released.
      *(Phase 3)*
- [ ] Sentry receiving errors. *(Phase 4)*
- [ ] Adding a new platform is one file plus one config row, proven by test.
      *(Phase 3)*
