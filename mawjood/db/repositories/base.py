"""Tenant-scoped repository base.

Tenant isolation is enforced structurally rather than by remembering to add a
filter. A repository cannot be constructed without a tenant, every read it builds
carries the tenant predicate, and every row it creates has ``tenant_id`` set for
you. Forgetting the scope is not an available mistake.

``tests/test_tenant_isolation.py`` proves a cross-tenant read returns nothing.
"""

from __future__ import annotations

import uuid
from typing import Any, ClassVar

from sqlalchemy import Select, select
from sqlalchemy.ext.asyncio import AsyncSession

from mawjood.db.base import Base


class TenantScopedRepository[ModelT: Base]:
    """Base for every repository over a table carrying ``tenant_id``."""

    # Subclasses set this. Typed loosely because SQLAlchemy's declarative
    # attributes are not expressible as a Protocol without a lot of noise.
    model: ClassVar[Any]

    def __init__(self, session: AsyncSession, tenant_id: uuid.UUID) -> None:
        self.session = session
        self.tenant_id = tenant_id

    def select(self) -> Select[Any]:
        """A SELECT already narrowed to this tenant. Always start here."""
        return select(self.model).where(self.model.tenant_id == self.tenant_id)

    def build(self, **values: Any) -> ModelT:
        """Instantiate a row with the tenant already applied.

        Refuses an explicit mismatched tenant_id rather than silently honouring
        it: passing another tenant's id to a scoped repository is a bug, not an
        override.
        """
        supplied = values.pop("tenant_id", None)
        if supplied is not None and supplied != self.tenant_id:
            raise ValueError(
                f"repository is scoped to tenant {self.tenant_id}, "
                f"refusing to build a row for {supplied}"
            )
        instance: ModelT = self.model(tenant_id=self.tenant_id, **values)
        return instance

    async def add(self, **values: Any) -> ModelT:
        instance = self.build(**values)
        self.session.add(instance)
        # Populate server-side defaults (id, timestamps) without committing.
        await self.session.flush()
        return instance

    async def get(self, entity_id: uuid.UUID) -> ModelT | None:
        result = await self.session.execute(self.select().where(self.model.id == entity_id))
        row: ModelT | None = result.scalar_one_or_none()
        return row

    async def list_all(self, limit: int = 100) -> list[ModelT]:
        result = await self.session.execute(self.select().limit(limit))
        return list(result.scalars().all())

    async def count(self) -> int:
        from sqlalchemy import func

        result = await self.session.execute(
            select(func.count())
            .select_from(self.model)
            .where(self.model.tenant_id == self.tenant_id)
        )
        return int(result.scalar_one())


__all__ = ["TenantScopedRepository"]
