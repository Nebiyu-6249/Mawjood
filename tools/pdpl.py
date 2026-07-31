#!/usr/bin/env python
"""PDPL operations: erase a consumer, and run the retention sweep.

Two duties from CLAUDE.md section 11, both of which have to produce evidence
rather than a shrug.

    # What would erasing this person remove? Nothing is deleted.
    uv run python tools/pdpl.py plan --wa-id 971501234567

    # Do it, and print a receipt.
    uv run python tools/pdpl.py erase --wa-id 971501234567 --confirm

    # What would the retention sweep remove today?
    uv run python tools/pdpl.py retention --dry-run

    # Run it. Intended for cron, daily.
    uv run python tools/pdpl.py retention

Erasure requires --confirm. It is irreversible and it deletes from the
append-only audit log, so it should not be one typo away from happening.

Both commands print JSON evidence to stdout with --json, which is what gets
filed against a request.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from mawjood.config import get_settings
from mawjood.core.compliance import (
    RetentionPolicy,
    erase_consumer,
    plan_erasure,
    sweep_retention,
)
from mawjood.db.engine import create_engine, create_session_factory
from mawjood.db.repositories.core import TenantRepository


async def _run(args: argparse.Namespace) -> int:
    settings = get_settings()
    engine = create_engine(settings)
    session_factory = create_session_factory(engine)

    try:
        async with session_factory() as session:
            tenant = await TenantRepository(session).get_by_slug(settings.default_tenant_slug)
            if tenant is None:
                print(f"no tenant with slug {settings.default_tenant_slug!r}", file=sys.stderr)
                return 1

            if args.command == "plan":
                plan = await plan_erasure(session, tenant_id=tenant.id, wa_id=args.wa_id)
                if plan is None:
                    print("no such consumer — nothing to erase")
                    return 0
                if args.json:
                    print(
                        json.dumps(
                            {"rows": plan.rows, "live_bookings": list(plan.live_bookings)}, indent=2
                        )
                    )
                    return 0
                print(f"erasing this consumer would remove {plan.total_rows} rows:")
                for table, count in sorted(plan.rows.items()):
                    if count:
                        print(f"  {table:26} {count}")
                if plan.needs_attention:
                    print("\n  ATTENTION — bookings still ahead of them:")
                    for booking in plan.live_bookings:
                        print(f"    {booking}")
                    print(
                        "\n  Erasing our record does NOT cancel these. The venue is still\n"
                        "  expecting this person. Cancel first if that is what they asked for."
                    )
                return 0

            if args.command == "erase":
                if not args.confirm:
                    print("erasure is irreversible. Re-run with --confirm.", file=sys.stderr)
                    return 2
                receipt = await erase_consumer(
                    session, tenant_id=tenant.id, wa_id=args.wa_id, reason=args.reason
                )
                if receipt is None:
                    print("no such consumer — nothing to erase")
                    return 0
                await session.commit()

                if args.json:
                    print(json.dumps(receipt.as_evidence(), indent=2))
                else:
                    print(f"erased {receipt.total_deleted} rows")
                    for table, count in sorted(receipt.deleted.items()):
                        if count:
                            print(f"  {table:26} {count}")
                    print(f"\nsubject hash : {receipt.subject_hash}")
                    print(f"complete     : {receipt.complete}")
                    if not receipt.complete:
                        left = {k: v for k, v in receipt.remaining.items() if v}
                        print(f"REMAINING    : {left}", file=sys.stderr)
                        return 1
                return 0

            # retention
            sweep = await sweep_retention(
                session,
                tenant_id=tenant.id,
                policy=RetentionPolicy.from_settings(settings),
                dry_run=args.dry_run,
            )
            if not args.dry_run:
                await session.commit()

            if args.json:
                print(json.dumps(sweep.as_evidence(), indent=2))
            else:
                label = "would remove" if args.dry_run else "removed"
                print(f"retention sweep {label} {sweep.total_deleted} rows")
                print(
                    f"  audit older than    {sweep.policy.audit_days}d ({sweep.audit_cutoff:%Y-%m-%d})"
                )
                print(
                    f"  consumers older than {sweep.policy.consumer_days}d ({sweep.consumer_cutoff:%Y-%m-%d})"
                )
                for table, count in sorted(sweep.deleted.items()):
                    if count:
                        print(f"  {table:26} {count}")
                if sweep.skipped_with_future_bookings:
                    print(
                        f"  kept {sweep.skipped_with_future_bookings} consumer(s) past the "
                        "cutoff who still have a booking ahead of them"
                    )
        return 0
    finally:
        await engine.dispose()


def main() -> int:
    parser = argparse.ArgumentParser(description="Mawjood PDPL operations.")
    parser.add_argument("--json", action="store_true", help="print evidence as JSON")
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("plan", help="what erasing a consumer would remove")
    p.add_argument("--wa-id", required=True)

    p = sub.add_parser("erase", help="remove a consumer everywhere")
    p.add_argument("--wa-id", required=True)
    p.add_argument("--confirm", action="store_true", help="required; erasure is irreversible")
    p.add_argument("--reason", default="consumer request")

    p = sub.add_parser("retention", help="delete everything past its retention date")
    p.add_argument("--dry-run", action="store_true", help="count without deleting")

    return asyncio.run(_run(parser.parse_args()))


if __name__ == "__main__":
    raise SystemExit(main())
