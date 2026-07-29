"""Tenant isolation.

``tenant_id`` on every table is worth nothing if a repository can be asked for
another tenant's rows. These tests are the reason the scope lives in the
repository constructor rather than in each query.
"""

from __future__ import annotations

import uuid

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from mawjood.config import Settings
from mawjood.core.conversation.pipeline import handle_inbound
from mawjood.core.conversation.types import InboundMessage
from mawjood.core.enums import Channel
from mawjood.db.base import Base
from mawjood.db.models import Lead
from mawjood.db.repositories.core import (
    ConversationRepository,
    LeadRepository,
    MessageRepository,
)

from .conftest import needs_database

pytestmark = [pytest.mark.integration, needs_database]


class TestSchema:
    def test_every_core_table_carries_a_tenant_id(self) -> None:
        """Decision from CLAUDE.md section 6, asserted rather than trusted."""
        from mawjood.db import models

        exempt = {"tenants"}  # the root of the hierarchy
        checked = 0
        for name in dir(models):
            model = getattr(models, name)
            if not isinstance(model, type) or not issubclass(model, Base):
                continue
            # Mixins have no __table__; the issubclass(Base) filter already
            # excludes them, so only mapped tables reach here.
            table = getattr(model, "__table__", None)
            if table is None or table.name in exempt:
                continue
            checked += 1
            assert "tenant_id" in table.columns, f"{table.name} has no tenant_id"

        # Guard against the loop silently matching nothing and passing forever.
        assert checked >= 9, f"only inspected {checked} tables"


class TestRepositoryScoping:
    async def test_a_repository_cannot_read_another_tenants_rows(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        tenant_id: uuid.UUID,
        other_tenant_id: uuid.UUID,
    ) -> None:
        async with session_factory() as session:
            await LeadRepository(session, tenant_id).add(wa_id="971500000001")
            await LeadRepository(session, other_tenant_id).add(wa_id="971500000002")
            await session.commit()

        async with session_factory() as session:
            mine = LeadRepository(session, tenant_id)
            assert await mine.count() == 1
            assert await mine.get_by_wa_id("971500000002") is None
            assert [lead.wa_id for lead in await mine.list_all()] == ["971500000001"]

    async def test_get_by_id_refuses_a_foreign_row(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        tenant_id: uuid.UUID,
        other_tenant_id: uuid.UUID,
    ) -> None:
        async with session_factory() as session:
            foreign = await LeadRepository(session, other_tenant_id).add(wa_id="971500000003")
            await session.commit()
            foreign_id = foreign.id

        async with session_factory() as session:
            assert await LeadRepository(session, tenant_id).get(foreign_id) is None
            assert await LeadRepository(session, other_tenant_id).get(foreign_id) is not None

    async def test_rows_are_created_with_the_scoped_tenant(
        self, session_factory: async_sessionmaker[AsyncSession], tenant_id: uuid.UUID
    ) -> None:
        async with session_factory() as session:
            lead = await LeadRepository(session, tenant_id).add(wa_id="971500000004")
            await session.commit()
            assert lead.tenant_id == tenant_id

    async def test_building_a_row_for_another_tenant_is_refused(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        tenant_id: uuid.UUID,
        other_tenant_id: uuid.UUID,
    ) -> None:
        """Passing a foreign tenant_id to a scoped repository is a bug, not an
        override, and is rejected rather than honoured."""
        async with session_factory() as session:
            with pytest.raises(ValueError, match="refusing to build"):
                LeadRepository(session, tenant_id).build(
                    wa_id="971500000005", tenant_id=other_tenant_id
                )

    async def test_conversations_and_messages_are_scoped_too(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        tenant_id: uuid.UUID,
        other_tenant_id: uuid.UUID,
    ) -> None:
        async with session_factory() as session:
            lead = await LeadRepository(session, other_tenant_id).add(wa_id="971500000006")
            conversation, _ = await ConversationRepository(session, other_tenant_id).open_or_resume(
                lead.id, Channel.WHATSAPP
            )
            await MessageRepository(session, other_tenant_id).record_inbound(
                conversation_id=conversation.id,
                lead_id=lead.id,
                turn_id=uuid.uuid4(),
                channel=Channel.WHATSAPP,
                body="hello",
                locale="en",
                provider_message_id="wamid.other",
            )
            await session.commit()

        async with session_factory() as session:
            assert await ConversationRepository(session, tenant_id).count() == 0
            assert await MessageRepository(session, tenant_id).count() == 0
            assert (
                await MessageRepository(session, tenant_id).find_by_provider_id(
                    Channel.WHATSAPP, "wamid.other"
                )
                is None
            )


class TestPipelineIsolation:
    async def test_the_same_consumer_under_two_tenants_stays_separate(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        db_settings: Settings,
        tenant_id: uuid.UUID,
        other_tenant_id: uuid.UUID,
    ) -> None:
        """One phone number talking to two operators is two leads, not one."""
        from mawjood.db.models import Tenant

        async with session_factory() as session:
            other = (
                await session.execute(select(Tenant).where(Tenant.id == other_tenant_id))
            ).scalar_one()
            other_slug = other.slug

        wa_id = "971512345678"
        for slug in (db_settings.default_tenant_slug, other_slug):
            async with session_factory() as session:
                await handle_inbound(
                    session,
                    InboundMessage(
                        tenant_slug=slug,
                        channel=Channel.WHATSAPP,
                        wa_id=wa_id,
                        text="hi",
                    ),
                    correlation_id="c",
                )

        async with session_factory() as session:
            leads = (await session.execute(select(Lead).where(Lead.wa_id == wa_id))).scalars().all()

        assert len(leads) == 2
        assert {lead.tenant_id for lead in leads} == {tenant_id, other_tenant_id}
