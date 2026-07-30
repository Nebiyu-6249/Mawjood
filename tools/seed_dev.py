#!/usr/bin/env python
"""Create the default tenant, and optionally a starter routing configuration.

Idempotent: safe to run repeatedly. docker-compose runs the equivalent at
startup via ``MAWJOOD_SEED_DEV_TENANT_ON_START``; this is the manual door.

    uv run python tools/seed_dev.py
    uv run python tools/seed_dev.py --routing
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from sqlalchemy import select

from mawjood.config import get_settings
from mawjood.core.enums import Category
from mawjood.db.engine import create_engine, create_session_factory
from mawjood.db.models import MerchantCredential
from mawjood.db.repositories.core import (
    RoutingConfigRepository,
    TenantRepository,
)

# Priority order per category. Slugs are free text on purpose: the adapter
# registry (Phase 2) is the authority on which exist, this table only orders
# them. Seeding them now means Phase 2 has something to route against.
STARTER_ROUTING: dict[Category, list[str]] = {
    # Zenoti sits first because it is the intended primary for salons. It is a
    # documented stub while its API docs are unreachable, so it returns
    # UNSUPPORTED and the cascade advances — which is exactly the behaviour the
    # invariant promises, visible in the demo.
    Category.SALON: ["zenoti", "fake_happy", "fake_flaky"],
    Category.SPA: ["zenoti", "fake_happy"],
    Category.RESTAURANT: ["fake_happy"],
    Category.FOOD_DELIVERY: ["fake_happy"],
}

# The all-down configuration used by the second demo transcript. Every candidate
# refuses in a different way, so the consumer reaches a human without ever being
# told a shelf is empty.
ALL_DOWN_ROUTING: dict[Category, list[str]] = {
    Category.SALON: ["fake_empty", "fake_auth", "fake_timeout", "zenoti"],
}

# Merchants per platform. Real deployments load these from the operator's
# onboarding; here they make the cascade concrete. secret_ref is None because the
# fakes need no credentials — a live platform would name a secret-store entry.
STARTER_MERCHANTS: list[tuple[str, str, str]] = [
    ("fake_happy", "Marina Beauty Lounge", "Dubai Marina"),
    ("fake_happy", "JLT Hair Studio", "JLT"),
    ("fake_flaky", "Downtown Cuts", "Downtown Dubai"),
    ("fake_empty", "Al Barsha Salon", "Al Barsha"),
    ("fake_auth", "Business Bay Spa", "Business Bay"),
    ("fake_timeout", "Deira Barbers", "Deira"),
    ("zenoti", "Zenoti Partner Salon", "Dubai Marina"),
]


async def seed(with_routing: bool, all_down: bool = False) -> int:
    settings = get_settings()
    engine = create_engine(settings)
    session_factory = create_session_factory(engine)

    try:
        async with session_factory() as session:
            tenant = await TenantRepository(session).ensure(
                settings.default_tenant_slug, settings.default_tenant_name
            )
            await session.commit()
            print(f"tenant: {tenant.slug} ({tenant.id})")

            if with_routing:
                merchants_made = 0
                for platform, name, area in STARTER_MERCHANTS:
                    exists = await session.execute(
                        select(MerchantCredential)
                        .where(MerchantCredential.tenant_id == tenant.id)
                        .where(MerchantCredential.platform_slug == platform)
                        .where(MerchantCredential.display_name == name)
                    )
                    if exists.scalar_one_or_none() is not None:
                        continue
                    session.add(
                        MerchantCredential(
                            tenant_id=tenant.id,
                            platform_slug=platform,
                            display_name=name,
                            area=area,
                            external_ids={"centre_id": f"c-{merchants_made:03d}"},
                            secret_ref=None,
                        )
                    )
                    merchants_made += 1
                await session.commit()
                print(f"merchants created: {merchants_made}")

                routing = RoutingConfigRepository(session, tenant.id)
                created = 0
                table = ALL_DOWN_ROUTING if all_down else STARTER_ROUTING
                for category, platforms in table.items():
                    existing = await routing.ordered_platforms(str(category))
                    if existing:
                        print(f"routing: {category} already configured -> {existing}")
                        continue
                    for position, slug in enumerate(platforms):
                        await routing.add(category=category, platform_slug=slug, position=position)
                        created += 1
                    print(f"routing: {category} -> {platforms}")
                await session.commit()
                print(f"routing rows created: {created}")
        return 0
    finally:
        await engine.dispose()


def main() -> int:
    parser = argparse.ArgumentParser(description="Seed local development data.")
    parser.add_argument(
        "--routing", action="store_true", help="also seed merchants and priority lists"
    )
    parser.add_argument(
        "--all-down",
        action="store_true",
        help="configure salon routing so every candidate fails, for the invariant demo",
    )
    args = parser.parse_args()
    return asyncio.run(seed(args.routing or args.all_down, args.all_down))


if __name__ == "__main__":
    raise SystemExit(main())
