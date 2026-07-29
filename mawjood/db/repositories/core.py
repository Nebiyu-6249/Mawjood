"""Concrete repositories.

One module rather than one file per table: these are thin, they share fixtures,
and splitting them would be five imports where one does.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from mawjood.core.conversation.consent import ConsentState
from mawjood.core.enums import (
    Channel,
    ConsentEvent,
    ConversationStatus,
    Direction,
    MessageStatus,
)
from mawjood.db.models import (
    Attribution,
    Consent,
    Conversation,
    HandoffQueue,
    Lead,
    Message,
    RoutingConfig,
    Tenant,
)
from mawjood.db.repositories.base import TenantScopedRepository


class TenantRepository:
    """Tenants are the root of the scoping hierarchy, so this one is unscoped."""

    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def get_by_slug(self, slug: str) -> Tenant | None:
        result = await self.session.execute(select(Tenant).where(Tenant.slug == slug))
        return result.scalar_one_or_none()

    async def ensure(self, slug: str, name: str) -> Tenant:
        """Idempotently create a tenant. Used by seeding and local development."""
        existing = await self.get_by_slug(slug)
        if existing is not None:
            return existing
        tenant = Tenant(slug=slug, name=name)
        self.session.add(tenant)
        await self.session.flush()
        return tenant


class LeadRepository(TenantScopedRepository[Lead]):
    model = Lead

    async def get_by_wa_id(self, wa_id: str) -> Lead | None:
        result = await self.session.execute(self.select().where(Lead.wa_id == wa_id))
        return result.scalar_one_or_none()

    async def get_or_create(self, wa_id: str, *, locale: str = "en") -> tuple[Lead, bool]:
        """Return the lead and whether it was created on this call."""
        existing = await self.get_by_wa_id(wa_id)
        if existing is not None:
            existing.last_seen_at = datetime.now(UTC)
            return existing, False
        lead = await self.add(wa_id=wa_id, locale=locale)
        return lead, True


class ConversationRepository(TenantScopedRepository[Conversation]):
    model = Conversation

    async def active_for_lead(self, lead_id: uuid.UUID) -> Conversation | None:
        result = await self.session.execute(
            self.select()
            .where(Conversation.lead_id == lead_id)
            .where(Conversation.status == ConversationStatus.ACTIVE)
            .order_by(Conversation.last_activity_at.desc())
            .limit(1)
        )
        return result.scalar_one_or_none()

    async def open_or_resume(
        self, lead_id: uuid.UUID, channel: Channel, *, locale: str = "en"
    ) -> tuple[Conversation, bool]:
        existing = await self.active_for_lead(lead_id)
        if existing is not None:
            existing.last_activity_at = datetime.now(UTC)
            return existing, False
        conversation = await self.add(lead_id=lead_id, channel=channel, locale=locale)
        return conversation, True


class MessageRepository(TenantScopedRepository[Message]):
    model = Message

    async def find_by_provider_id(
        self, channel: Channel, provider_message_id: str
    ) -> Message | None:
        """Inbound idempotency lookup.

        Webhook delivery is at-least-once. Without this, a redelivered message
        starts a second conversation turn and, later, a second booking attempt.
        """
        result = await self.session.execute(
            self.select()
            .where(Message.channel == channel)
            .where(Message.provider_message_id == provider_message_id)
        )
        return result.scalar_one_or_none()

    async def record_inbound(
        self,
        *,
        conversation_id: uuid.UUID,
        lead_id: uuid.UUID,
        turn_id: uuid.UUID,
        channel: Channel,
        body: str,
        locale: str,
        provider_message_id: str | None,
    ) -> Message:
        return await self.add(
            conversation_id=conversation_id,
            lead_id=lead_id,
            turn_id=turn_id,
            direction=Direction.INBOUND,
            channel=channel,
            status=MessageStatus.RECEIVED,
            body=body,
            locale=locale,
            provider_message_id=provider_message_id,
        )

    async def record_outbound(
        self,
        *,
        conversation_id: uuid.UUID,
        lead_id: uuid.UUID,
        turn_id: uuid.UUID,
        channel: Channel,
        body: str,
        locale: str,
        phrasebank_key: str,
    ) -> Message:
        """Outbound messages must name the phrasebank key they came from.

        A database CHECK enforces it too. Consumer-facing copy that cannot name
        its phrasebank entry has bypassed the blocklist scan.
        """
        return await self.add(
            conversation_id=conversation_id,
            lead_id=lead_id,
            turn_id=turn_id,
            direction=Direction.OUTBOUND,
            channel=channel,
            status=MessageStatus.QUEUED,
            body=body,
            locale=locale,
            phrasebank_key=phrasebank_key,
        )

    async def history(self, conversation_id: uuid.UUID, limit: int = 50) -> list[Message]:
        result = await self.session.execute(
            self.select()
            .where(Message.conversation_id == conversation_id)
            .order_by(Message.seq)
            .limit(limit)
        )
        return list(result.scalars().all())


class ConsentRepository(TenantScopedRepository[Consent]):
    model = Consent

    async def events_for_lead(self, lead_id: uuid.UUID) -> list[Consent]:
        result = await self.session.execute(
            self.select()
            .where(Consent.lead_id == lead_id)
            .order_by(Consent.occurred_at, Consent.id)
        )
        return list(result.scalars().all())

    async def latest_event(self, lead_id: uuid.UUID) -> Consent | None:
        result = await self.session.execute(
            self.select()
            .where(Consent.lead_id == lead_id)
            .order_by(Consent.occurred_at.desc(), Consent.id.desc())
            .limit(1)
        )
        return result.scalar_one_or_none()

    async def has_been_shown_notice(self, lead_id: uuid.UUID) -> bool:
        result = await self.session.execute(
            select(func.count())
            .select_from(Consent)
            .where(Consent.tenant_id == self.tenant_id)
            .where(Consent.lead_id == lead_id)
        )
        return int(result.scalar_one()) > 0

    async def state_for_lead(self, lead_id: uuid.UUID) -> ConsentState:
        """Derive current consent state from the event stream.

        Decisive events (grant, withdrawal) are checked first so a re-shown
        notice cannot read as a downgrade from an existing grant.
        """
        decisive = await self.session.execute(
            self.select()
            .where(Consent.lead_id == lead_id)
            .where(Consent.event.in_([ConsentEvent.GRANTED, ConsentEvent.WITHDRAWN]))
            .order_by(Consent.occurred_at.desc(), Consent.id.desc())
            .limit(1)
        )
        latest = decisive.scalar_one_or_none()
        if latest is not None:
            return (
                ConsentState.GRANTED
                if latest.event == ConsentEvent.GRANTED
                else ConsentState.WITHDRAWN
            )
        if await self.has_been_shown_notice(lead_id):
            return ConsentState.NOTICE_SHOWN
        return ConsentState.UNKNOWN

    async def is_granted(self, lead_id: uuid.UUID) -> bool:
        """Derived from the event stream, never stored as a mutable flag.

        Granted means the most recent decisive event is a grant. A withdrawal
        after a grant revokes it.
        """
        result = await self.session.execute(
            self.select()
            .where(Consent.lead_id == lead_id)
            .where(Consent.event.in_([ConsentEvent.GRANTED, ConsentEvent.WITHDRAWN]))
            .order_by(Consent.occurred_at.desc(), Consent.id.desc())
            .limit(1)
        )
        latest = result.scalar_one_or_none()
        return latest is not None and latest.event == ConsentEvent.GRANTED


class AttributionRepository(TenantScopedRepository[Attribution]):
    model = Attribution


class RoutingConfigRepository(TenantScopedRepository[RoutingConfig]):
    model = RoutingConfig

    async def ordered_platforms(self, category: str) -> list[str]:
        """The cascade order for a category. Position ascending, disabled dropped."""
        result = await self.session.execute(
            self.select()
            .where(RoutingConfig.category == category)
            .where(RoutingConfig.is_enabled.is_(True))
            .order_by(RoutingConfig.position)
        )
        return [row.platform_slug for row in result.scalars().all()]


class HandoffRepository(TenantScopedRepository[HandoffQueue]):
    model = HandoffQueue


__all__ = [
    "AttributionRepository",
    "ConsentRepository",
    "ConversationRepository",
    "HandoffRepository",
    "LeadRepository",
    "MessageRepository",
    "RoutingConfigRepository",
    "TenantRepository",
]
