# Adapter conformance

Every registered adapter — fake, live, or a documented `UNSUPPORTED` stub — must
pass one shared suite. The contract is in `CLAUDE.md` section 4.

The load-bearing assertion: **adapters never raise into the router.** Under fault
injection at the transport layer, every method still returns a `Result` carrying
an `Outcome`. The router decides what an outcome means; the adapter only reports.

Empty until Phase 1, when the contract and the fakes land.
