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

from mawjood.config import get_settings
from mawjood.core.enums import Category
from mawjood.db.engine import create_engine, create_session_factory
from mawjood.db.repositories.core import (
    RoutingConfigRepository,
    TenantRepository,
)

# Priority order per category. Slugs are free text on purpose: the adapter
# registry (Phase 2) is the authority on which exist, this table only orders
# them. Seeding them now means Phase 2 has something to route against.
STARTER_ROUTING: dict[Category, list[str]] = {
    Category.SALON: ["zenoti", "fresha", "booksy"],
    Category.SPA: ["zenoti", "fresha"],
    Category.FOOD_DELIVERY: ["deliveroo", "talabat"],
    Category.RESTAURANT: ["opentable", "foodics"],
}


async def seed(with_routing: bool) -> int:
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
                routing = RoutingConfigRepository(session, tenant.id)
                created = 0
                for category, platforms in STARTER_ROUTING.items():
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
        "--routing", action="store_true", help="also seed the category priority lists"
    )
    args = parser.parse_args()
    return asyncio.run(seed(args.routing))


if __name__ == "__main__":
    raise SystemExit(main())
