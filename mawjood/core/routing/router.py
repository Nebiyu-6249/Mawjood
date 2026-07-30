"""Turning configuration into candidates.

The priority list is data. ``routing_config`` holds (category → ordered platform
slugs) and ``merchant_credentials`` holds the merchants under each platform. Ops
reorder a category by updating rows — **no code change, no redeploy**. There is a
test for that.

The cascade order is platform-major, merchant-minor (decision 1):

    salon → [zenoti, fresha, booksy]
    zenoti → [Marina centre, JLT centre]      ← tried before moving to fresha

A configured slug with no registered adapter is a configuration fault. It is
logged and skipped, never surfaced: a typo in a config table must not become a
dead end for a consumer.
"""

from __future__ import annotations

import uuid
from collections.abc import Sequence

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from mawjood.core.aggregators.base import MerchantRef
from mawjood.core.aggregators.registry import AdapterRegistry
from mawjood.core.enums import Category
from mawjood.core.routing.cascade import Candidate
from mawjood.db.models import MerchantCredential, RoutingConfig
from mawjood.observability.logging import get_logger
from mawjood.services.secrets import CredentialResolver

log = get_logger(__name__)


class Router:
    """Resolves a category into an ordered candidate list."""

    def __init__(
        self,
        *,
        registry: AdapterRegistry,
        resolver: CredentialResolver,
        session: AsyncSession,
        tenant_id: uuid.UUID,
    ) -> None:
        self._registry = registry
        self._resolver = resolver
        self._session = session
        self._tenant_id = tenant_id

    async def platforms_for(self, category: Category | str) -> list[str]:
        """The configured priority order, straight from the table."""
        result = await self._session.execute(
            select(RoutingConfig.platform_slug)
            .where(RoutingConfig.tenant_id == self._tenant_id)
            .where(RoutingConfig.category == str(category))
            .where(RoutingConfig.is_enabled.is_(True))
            .order_by(RoutingConfig.position)
        )
        return list(result.scalars().all())

    async def merchants_for(self, platform_slug: str) -> list[MerchantCredential]:
        result = await self._session.execute(
            select(MerchantCredential)
            .where(MerchantCredential.tenant_id == self._tenant_id)
            .where(MerchantCredential.platform_slug == platform_slug)
            .where(MerchantCredential.is_active.is_(True))
            .order_by(MerchantCredential.priority, MerchantCredential.display_name)
        )
        return list(result.scalars().all())

    async def candidates_for(
        self, category: Category | str, *, area: str | None = None
    ) -> list[Candidate]:
        """Build the candidate list for one cascade run.

        Credentials are resolved here, just before the call, and handed to the
        adapter in ``CallContext``. Adapters never reach for a secret store.
        """
        candidates: list[Candidate] = []

        for slug in await self.platforms_for(category):
            adapter = self._registry.get(slug)
            if adapter is None:
                # A typo or a platform that has not shipped yet. Ops problem.
                log.error(
                    "routing.configured_platform_has_no_adapter",
                    platform=slug,
                    category=str(category),
                    registered=self._registry.slugs,
                )
                continue

            merchants = await self.merchants_for(slug)
            if not merchants:
                log.warning("routing.platform_has_no_merchants", platform=slug)
                continue

            for merchant in _prefer_area(merchants, area):
                candidates.append(
                    Candidate(
                        adapter=adapter,
                        merchant=MerchantRef(
                            platform_slug=slug,
                            merchant_id=merchant.id,
                            display_name=merchant.display_name,
                            external_ids={
                                str(k): str(v) for k, v in (merchant.external_ids or {}).items()
                            },
                            area=merchant.area,
                        ),
                        credentials=self._resolver.resolve(merchant.secret_ref),
                    )
                )

        return candidates


def _prefer_area(
    merchants: Sequence[MerchantCredential], area: str | None
) -> list[MerchantCredential]:
    """Merchants in the requested area first, then the rest.

    Not a filter: a salon two neighbourhoods over beats no salon at all, and the
    invariant means we would rather offer something slightly further than nothing.
    Ordering, never exclusion.
    """
    if not area:
        return list(merchants)
    wanted = area.strip().lower()
    near = [m for m in merchants if (m.area or "").strip().lower() == wanted]
    rest = [m for m in merchants if (m.area or "").strip().lower() != wanted]
    return near + rest


__all__ = ["Router"]
