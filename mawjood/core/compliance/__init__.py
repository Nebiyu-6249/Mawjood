"""PDPL obligations that are code rather than policy: erasure and retention.

Two duties from CLAUDE.md §11, both of which have to be demonstrable rather than
asserted:

* **Erasure** — a working deletion path that purges a consumer across *every*
  table, including the audit log. :mod:`~mawjood.core.compliance.erasure`.
* **Retention** — logs kept 90 days, consumer data kept a configurable period,
  and a job that actually enforces both. :mod:`~mawjood.core.compliance.retention`.

## Why erasure is harder than `DELETE FROM leads`

Three things make it non-trivial, and all three are handled explicitly:

1. **The audit log is append-only**, enforced by a database trigger. It has to
   be, or the record of what was tried and why could be rewritten after the
   fact. Erasure therefore runs inside a session that sets a PostgreSQL
   session-local flag the trigger recognises — a deliberate, narrow, audited
   escape hatch rather than a hole.
2. **Cascades are not enough.** Most tables carry `ON DELETE CASCADE` from
   `leads`, but `audit_log` deliberately does not (it would let an ordinary
   delete quietly destroy the trail), and `bookings.lead_id` cascade would take
   a booking a venue still holds. Each table is handled on purpose.
3. **A booking that still exists in the world.** Erasing our record of a
   confirmed future booking does not cancel it at the venue. The consumer's
   right to erasure is real, and so is the venue expecting them. Erasure
   surfaces this rather than silently choosing: see
   :class:`~mawjood.core.compliance.erasure.ErasurePlan`.
"""

from __future__ import annotations

from mawjood.core.compliance.erasure import (
    ErasurePlan,
    ErasureReceipt,
    erase_consumer,
    plan_erasure,
)
from mawjood.core.compliance.retention import (
    RetentionPolicy,
    RetentionSweep,
    sweep_retention,
)

__all__ = [
    "ErasurePlan",
    "ErasureReceipt",
    "RetentionPolicy",
    "RetentionSweep",
    "erase_consumer",
    "plan_erasure",
    "sweep_retention",
]
