"""The Mawjood schema.

Every core table carries ``tenant_id`` (CLAUDE.md section 6). Relationships are
deliberately not declared: under async SQLAlchemy a lazy load raises at await
time, usually in production rather than in a test, so repositories issue explicit
queries instead.

Enum-backed columns use ``native_enum=False``, which produces a VARCHAR plus a
CHECK constraint. Values are validated, and widening the set is an ordinary
migration rather than a PostgreSQL type alteration. ``audit_log`` is the
deliberate exception — see the note on that model.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Identity,
    Index,
    Integer,
    Numeric,
    String,
    Text,
    UniqueConstraint,
    func,
    text,
)
from sqlalchemy import (
    Enum as SAEnum,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.dialects.postgresql import UUID as PgUUID
from sqlalchemy.orm import Mapped, mapped_column

from mawjood.core.enums import (
    AttributionMedium,
    BookingStatus,
    Category,
    Channel,
    ConsentEvent,
    ConversationStatus,
    Direction,
    HandoffReason,
    HandoffStatus,
    MessageStatus,
    PaymentStatus,
)
from mawjood.db.base import Base, TenantMixin, TimestampMixin, uuid_pk


def _enum(enum_cls: type, name: str) -> SAEnum:
    """VARCHAR + CHECK constraint holding the enum's *values*, not its names."""
    return SAEnum(
        enum_cls,
        name=name,
        native_enum=False,
        length=48,
        values_callable=lambda e: [member.value for member in e],
    )


class Tenant(Base, TimestampMixin):
    """An operator running Mawjood.

    v1 runs a single tenant. This table exists so that stops being an assumption
    baked into every other table. Tenant management, billing and onboarding are
    explicitly out of scope (CLAUDE.md section 14).
    """

    __tablename__ = "tenants"

    id: Mapped[uuid.UUID] = uuid_pk()
    slug: Mapped[str] = mapped_column(String(64), nullable=False, unique=True)
    name: Mapped[str] = mapped_column(String(200), nullable=False)
    default_locale: Mapped[str] = mapped_column(String(8), nullable=False, server_default="en")
    timezone: Mapped[str] = mapped_column(String(64), nullable=False, server_default="Asia/Dubai")
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default="true")


class Lead(Base, TenantMixin, TimestampMixin):
    """A consumer.

    ``wa_id`` is the messaging provider's identifier for the person and is the
    natural key within a tenant. Personal data here is the minimum needed to hold
    a conversation and send a confirmation; erasure removes the row and every
    row referencing it (PDPL, Phase 4).
    """

    __tablename__ = "leads"

    id: Mapped[uuid.UUID] = uuid_pk()
    wa_id: Mapped[str] = mapped_column(String(64), nullable=False)
    phone_e164: Mapped[str | None] = mapped_column(String(32), nullable=True)
    display_name: Mapped[str | None] = mapped_column(String(200), nullable=True)
    locale: Mapped[str] = mapped_column(String(8), nullable=False, server_default="en")
    first_seen_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    last_seen_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )

    __table_args__ = (
        UniqueConstraint("tenant_id", "wa_id", name="uq_leads_tenant_id_wa_id"),
        Index("ix_leads_tenant_id_last_seen_at", "tenant_id", "last_seen_at"),
    )


class Conversation(Base, TenantMixin, TimestampMixin):
    """One continuous exchange with a consumer.

    ``bot_muted`` implements the handoff mute (decision 7): while a human owns the
    thread, the assistant stays silent on it until explicitly released.
    """

    __tablename__ = "conversations"

    id: Mapped[uuid.UUID] = uuid_pk()
    lead_id: Mapped[uuid.UUID] = mapped_column(
        PgUUID(as_uuid=True), ForeignKey("leads.id", ondelete="CASCADE"), nullable=False, index=True
    )
    channel: Mapped[Channel] = mapped_column(_enum(Channel, "channel"), nullable=False)
    status: Mapped[ConversationStatus] = mapped_column(
        _enum(ConversationStatus, "conversation_status"),
        nullable=False,
        server_default=ConversationStatus.ACTIVE.value,
    )
    locale: Mapped[str] = mapped_column(String(8), nullable=False, server_default="en")
    bot_muted: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default="false")

    # Where the state machine is, and what it has gathered. Persisted so a
    # conversation survives a restart and a consumer can pick it up tomorrow.
    #
    # Free text rather than a CHECK: the state vocabulary will grow, and a
    # constraint here would turn adding a state into a migration on a hot table.
    state: Mapped[str] = mapped_column(String(32), nullable=False, server_default="greeting")
    collected_slots: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False, server_default=text("'{}'::jsonb")
    )
    # The concrete slot awaiting an explicit yes. Nothing is booked while this is
    # empty — see core/conversation/states.py.
    pending_offer: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False, server_default=text("'{}'::jsonb")
    )
    started_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    last_activity_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now(), index=True
    )
    closed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    __table_args__ = (
        Index("ix_conversations_tenant_id_lead_id_status", "tenant_id", "lead_id", "status"),
    )


class Message(Base, TenantMixin, TimestampMixin):
    """Every message in and out.

    Outbound rows record ``phrasebank_key``: consumer-facing copy is rendered from
    the phrasebank, never composed inline, so the blocklist scan over the
    phrasebank is exhaustive rather than best-effort (CLAUDE.md section 2).

    ``turn_id`` ties a message to the audit rows produced while handling it.
    """

    __tablename__ = "messages"

    id: Mapped[uuid.UUID] = uuid_pk()
    # Stable total ordering, assigned by the database.
    #
    # created_at cannot do this job: its server default is now(), which in
    # PostgreSQL is *transaction start time*, so every message written in one
    # turn shares a timestamp and the tiebreak falls to a random UUID. A
    # conversation transcript then reads back in arbitrary order — including the
    # inbound message sorting between the two replies to it.
    seq: Mapped[int] = mapped_column(BigInteger, Identity(always=True), nullable=False, unique=True)
    conversation_id: Mapped[uuid.UUID] = mapped_column(
        PgUUID(as_uuid=True),
        ForeignKey("conversations.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    lead_id: Mapped[uuid.UUID] = mapped_column(
        PgUUID(as_uuid=True), ForeignKey("leads.id", ondelete="CASCADE"), nullable=False
    )
    turn_id: Mapped[uuid.UUID] = mapped_column(PgUUID(as_uuid=True), nullable=False, index=True)
    direction: Mapped[Direction] = mapped_column(_enum(Direction, "direction"), nullable=False)
    channel: Mapped[Channel] = mapped_column(_enum(Channel, "channel"), nullable=False)
    status: Mapped[MessageStatus] = mapped_column(
        _enum(MessageStatus, "message_status"), nullable=False
    )
    body: Mapped[str] = mapped_column(Text, nullable=False)
    locale: Mapped[str] = mapped_column(String(8), nullable=False, server_default="en")
    phrasebank_key: Mapped[str | None] = mapped_column(String(128), nullable=True)
    provider_message_id: Mapped[str | None] = mapped_column(String(200), nullable=True)
    sent_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    __table_args__ = (
        # Webhook delivery is at-least-once. This is what makes redelivery of the
        # same provider message a no-op instead of a duplicate conversation turn.
        Index(
            "uq_messages_tenant_id_channel_provider_message_id",
            "tenant_id",
            "channel",
            "provider_message_id",
            unique=True,
            postgresql_where=text("provider_message_id IS NOT NULL"),
        ),
        Index("ix_messages_conversation_id_seq", "conversation_id", "seq"),
        CheckConstraint(
            "(direction <> 'outbound') OR (phrasebank_key IS NOT NULL)",
            name="outbound_messages_come_from_the_phrasebank",
        ),
    )


class Consent(Base, TenantMixin, TimestampMixin):
    """PDPL consent, recorded as an append-only event stream.

    Current state is derived from the events rather than overwritten, so what a
    consumer was shown, and when, survives intact. ``wording`` stores the exact
    text displayed, not a reference to it: the copy will change, and an audit
    needs the version the consumer actually read.
    """

    __tablename__ = "consents"

    id: Mapped[uuid.UUID] = uuid_pk()
    lead_id: Mapped[uuid.UUID] = mapped_column(
        PgUUID(as_uuid=True), ForeignKey("leads.id", ondelete="CASCADE"), nullable=False, index=True
    )
    conversation_id: Mapped[uuid.UUID | None] = mapped_column(
        PgUUID(as_uuid=True), ForeignKey("conversations.id", ondelete="SET NULL"), nullable=True
    )
    event: Mapped[ConsentEvent] = mapped_column(
        _enum(ConsentEvent, "consent_event"), nullable=False
    )
    policy_version: Mapped[str] = mapped_column(String(32), nullable=False)
    wording: Mapped[str] = mapped_column(Text, nullable=False)
    locale: Mapped[str] = mapped_column(String(8), nullable=False)
    channel: Mapped[Channel] = mapped_column(_enum(Channel, "channel"), nullable=False)
    # The inbound message that constituted the grant, for evidential purposes.
    evidence_message_id: Mapped[uuid.UUID | None] = mapped_column(
        PgUUID(as_uuid=True), ForeignKey("messages.id", ondelete="SET NULL"), nullable=True
    )
    occurred_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now(), index=True
    )

    __table_args__ = (
        Index("ix_consents_tenant_id_lead_id_occurred_at", "tenant_id", "lead_id", "occurred_at"),
    )


class Booking(Base, TenantMixin, TimestampMixin):
    """A booking placed through an aggregator.

    ``idempotency_key`` is derived from (conversation, category, slot start,
    aggregator) and is unique per tenant. It is the anchor for the double-booking
    defence: on a create timeout the cascade reconciles against this key rather
    than blindly retrying elsewhere (CLAUDE.md section 5.1).

    Price and payment columns exist but no payment flow does (decision 3).
    """

    __tablename__ = "bookings"

    id: Mapped[uuid.UUID] = uuid_pk()
    conversation_id: Mapped[uuid.UUID] = mapped_column(
        PgUUID(as_uuid=True),
        ForeignKey("conversations.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    lead_id: Mapped[uuid.UUID] = mapped_column(
        PgUUID(as_uuid=True), ForeignKey("leads.id", ondelete="CASCADE"), nullable=False, index=True
    )
    category: Mapped[Category] = mapped_column(_enum(Category, "category"), nullable=False)
    platform_slug: Mapped[str] = mapped_column(String(64), nullable=False)
    merchant_ref: Mapped[str | None] = mapped_column(String(200), nullable=True)
    external_booking_ref: Mapped[str | None] = mapped_column(String(200), nullable=True)
    idempotency_key: Mapped[str] = mapped_column(String(200), nullable=False)
    status: Mapped[BookingStatus] = mapped_column(
        _enum(BookingStatus, "booking_status"), nullable=False
    )
    slot_start: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    slot_end: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    venue_name: Mapped[str | None] = mapped_column(String(200), nullable=True)
    price_amount: Mapped[float | None] = mapped_column(Numeric(12, 2), nullable=True)
    price_currency: Mapped[str | None] = mapped_column(String(3), nullable=True)
    payment_status: Mapped[PaymentStatus] = mapped_column(
        _enum(PaymentStatus, "payment_status"),
        nullable=False,
        server_default=PaymentStatus.NOT_APPLICABLE.value,
    )
    confirmed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    cancelled_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    __table_args__ = (
        UniqueConstraint(
            "tenant_id", "idempotency_key", name="uq_bookings_tenant_id_idempotency_key"
        ),
        Index("ix_bookings_tenant_id_status_slot_start", "tenant_id", "status", "slot_start"),
    )


class Feedback(Base, TenantMixin, TimestampMixin):
    """Post-booking satisfaction, 1 to 5."""

    __tablename__ = "feedback"

    id: Mapped[uuid.UUID] = uuid_pk()
    conversation_id: Mapped[uuid.UUID] = mapped_column(
        PgUUID(as_uuid=True), ForeignKey("conversations.id", ondelete="CASCADE"), nullable=False
    )
    booking_id: Mapped[uuid.UUID | None] = mapped_column(
        PgUUID(as_uuid=True), ForeignKey("bookings.id", ondelete="SET NULL"), nullable=True
    )
    lead_id: Mapped[uuid.UUID] = mapped_column(
        PgUUID(as_uuid=True), ForeignKey("leads.id", ondelete="CASCADE"), nullable=False, index=True
    )
    rating: Mapped[int | None] = mapped_column(Integer, nullable=True)
    comment: Mapped[str | None] = mapped_column(Text, nullable=True)
    requested_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    responded_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    __table_args__ = (
        CheckConstraint("rating IS NULL OR (rating BETWEEN 1 AND 5)", name="rating_is_one_to_five"),
    )


class Attribution(Base, TenantMixin, TimestampMixin):
    """Where a consumer came from: a wa.me link code or a scanned QR."""

    __tablename__ = "attribution"

    id: Mapped[uuid.UUID] = uuid_pk()
    lead_id: Mapped[uuid.UUID] = mapped_column(
        PgUUID(as_uuid=True), ForeignKey("leads.id", ondelete="CASCADE"), nullable=False, index=True
    )
    conversation_id: Mapped[uuid.UUID | None] = mapped_column(
        PgUUID(as_uuid=True), ForeignKey("conversations.id", ondelete="SET NULL"), nullable=True
    )
    source_code: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    medium: Mapped[AttributionMedium] = mapped_column(
        _enum(AttributionMedium, "attribution_medium"),
        nullable=False,
        server_default=AttributionMedium.UNKNOWN.value,
    )
    # True for the source that first brought this consumer to Mawjood.
    is_first_touch: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default="false")
    raw_payload: Mapped[str | None] = mapped_column(Text, nullable=True)

    __table_args__ = (Index("ix_attribution_tenant_id_source_code", "tenant_id", "source_code"),)


class RoutingConfig(Base, TenantMixin, TimestampMixin):
    """Category to ordered aggregator list.

    ``platform_slug`` is intentionally free text with no foreign key: the adapter
    registry (Phase 2) is the authority on which slugs exist, and this table only
    decides their order. A slug with no registered adapter is a configuration
    error the router reports; it must never become a dead end for a consumer.
    """

    __tablename__ = "routing_config"

    id: Mapped[uuid.UUID] = uuid_pk()
    category: Mapped[Category] = mapped_column(_enum(Category, "category"), nullable=False)
    platform_slug: Mapped[str] = mapped_column(String(64), nullable=False)
    position: Mapped[int] = mapped_column(Integer, nullable=False)
    is_enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default="true")
    config: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False, server_default=text("'{}'::jsonb")
    )

    __table_args__ = (
        UniqueConstraint(
            "tenant_id", "category", "platform_slug", name="uq_routing_config_tenant_category_slug"
        ),
        UniqueConstraint(
            "tenant_id", "category", "position", name="uq_routing_config_tenant_category_position"
        ),
        CheckConstraint("position >= 0", name="position_is_non_negative"),
    )


class HandoffQueue(Base, TenantMixin, TimestampMixin):
    """Conversations waiting for a human.

    This queue is the invariant's floor. When the cascade is exhausted or an
    action is unsupported, the consumer is handed to a person — never told the
    shelves are empty.
    """

    __tablename__ = "handoff_queue"

    id: Mapped[uuid.UUID] = uuid_pk()
    conversation_id: Mapped[uuid.UUID] = mapped_column(
        PgUUID(as_uuid=True),
        ForeignKey("conversations.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    lead_id: Mapped[uuid.UUID] = mapped_column(
        PgUUID(as_uuid=True), ForeignKey("leads.id", ondelete="CASCADE"), nullable=False
    )
    reason: Mapped[HandoffReason] = mapped_column(
        _enum(HandoffReason, "handoff_reason"), nullable=False
    )
    status: Mapped[HandoffStatus] = mapped_column(
        _enum(HandoffStatus, "handoff_status"),
        nullable=False,
        server_default=HandoffStatus.OPEN.value,
    )
    priority: Mapped[int] = mapped_column(Integer, nullable=False, server_default="0")
    claimed_by: Mapped[str | None] = mapped_column(String(200), nullable=True)
    claimed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    resolved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    notes: Mapped[str | None] = mapped_column(Text, nullable=True)
    # Enough context for a human to pick up cold: what was tried, what is known.
    context: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False, server_default=text("'{}'::jsonb")
    )

    __table_args__ = (
        Index("ix_handoff_queue_tenant_id_status_priority", "tenant_id", "status", "priority"),
    )


class MerchantCredential(Base, TenantMixin, TimestampMixin):
    """A merchant on a platform, and where its credentials live.

    Decision 1: aggregator APIs are merchant-side. Each salon has its own centre
    ids and key, so the cascade iterates platform → merchants within it rather
    than platform → platform.

    ``secret_ref`` is a *reference*, never a credential. Values live in the secret
    store and are resolved just-in-time by the router, which hands them to the
    adapter in CallContext. Nothing secret is ever in this table, so a database
    dump is not a credential leak.
    """

    __tablename__ = "merchant_credentials"

    id: Mapped[uuid.UUID] = uuid_pk()
    platform_slug: Mapped[str] = mapped_column(String(64), nullable=False)
    display_name: Mapped[str] = mapped_column(String(200), nullable=False)
    area: Mapped[str | None] = mapped_column(String(120), nullable=True)
    # Platform-side identifiers: centre_id, org_id, location_id, and so on.
    external_ids: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False, server_default=text("'{}'::jsonb")
    )
    secret_ref: Mapped[str | None] = mapped_column(String(200), nullable=True)
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default="true")
    # Order within a platform. The cascade tries lower numbers first.
    priority: Mapped[int] = mapped_column(Integer, nullable=False, server_default="0")
    # Set when a credential is rejected upstream, so ops can see it without
    # reading logs. AUTH_ERROR is our problem, never the consumer's.
    credential_degraded_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )

    __table_args__ = (
        UniqueConstraint(
            "tenant_id",
            "platform_slug",
            "display_name",
            name="uq_merchant_credentials_tenant_platform_name",
        ),
        Index(
            "ix_merchant_credentials_tenant_platform_priority",
            "tenant_id",
            "platform_slug",
            "priority",
        ),
    )


class AuditLog(Base, TenantMixin):
    """Append-only record of everything the system decided and showed.

    The requirement is that a routing decision is reconstructable months later:
    the inputs, every aggregator attempted, its outcome and latency, why the
    cascade advanced, and what the consumer was actually shown.

    Design choices that serve that:

    * **Typed columns for what you query on**, JSONB only for what varies. A
      pure-JSONB audit table is unqueryable by the time you need it.
    * **``turn_id``** groups every row produced while handling one inbound
      message. **``decision_id``** groups the attempts of one cascade run. One
      turn may contain several decisions.
    * **``seq``** is a database-assigned identity giving stable total ordering.
      Timestamps collide at millisecond resolution and cannot be sorted on alone.
    * **``event_type`` and ``outcome`` are unconstrained text.** Every other table
      validates its enums; this one must not. An audit write that fails because
      of an unrecognised value loses the very record explaining what happened.
    * **No ``updated_at``, and updates are blocked by a trigger.** Audit rows are
      never edited. Deletion is blocked too, except under the explicit session
      flag the PDPL erasure path sets, so the right to erasure still works while
      casual deletion does not.
    """

    __tablename__ = "audit_log"

    id: Mapped[uuid.UUID] = uuid_pk()
    seq: Mapped[int] = mapped_column(BigInteger, Identity(always=True), nullable=False, unique=True)
    occurred_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now(), index=True
    )

    # --- Correlation ------------------------------------------------------
    turn_id: Mapped[uuid.UUID | None] = mapped_column(
        PgUUID(as_uuid=True), nullable=True, index=True
    )
    decision_id: Mapped[uuid.UUID | None] = mapped_column(
        PgUUID(as_uuid=True), nullable=True, index=True
    )
    conversation_id: Mapped[uuid.UUID | None] = mapped_column(
        PgUUID(as_uuid=True), ForeignKey("conversations.id", ondelete="CASCADE"), nullable=True
    )
    lead_id: Mapped[uuid.UUID | None] = mapped_column(
        PgUUID(as_uuid=True), ForeignKey("leads.id", ondelete="CASCADE"), nullable=True
    )
    # Ties an audit row to the HTTP request and to the JSON log lines for it.
    correlation_id: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)

    # --- What happened ----------------------------------------------------
    event_type: Mapped[str] = mapped_column(String(64), nullable=False)
    actor: Mapped[str] = mapped_column(String(32), nullable=False)

    # --- Routing specifics (null for non-routing events) ------------------
    category: Mapped[str | None] = mapped_column(String(32), nullable=True)
    platform_slug: Mapped[str | None] = mapped_column(String(64), nullable=True)
    merchant_ref: Mapped[str | None] = mapped_column(String(200), nullable=True)
    attempt_number: Mapped[int | None] = mapped_column(Integer, nullable=True)
    outcome: Mapped[str | None] = mapped_column(String(48), nullable=True)
    latency_ms: Mapped[int | None] = mapped_column(Integer, nullable=True)
    # Why the cascade moved on. The single most useful column in an incident.
    advance_reason: Mapped[str | None] = mapped_column(String(200), nullable=True)

    # --- What the consumer saw --------------------------------------------
    consumer_visible: Mapped[bool] = mapped_column(
        Boolean, nullable=False, server_default="false", index=True
    )
    phrasebank_key: Mapped[str | None] = mapped_column(String(128), nullable=True)
    locale: Mapped[str | None] = mapped_column(String(8), nullable=True)
    consumer_text: Mapped[str | None] = mapped_column(Text, nullable=True)

    # --- Free-form --------------------------------------------------------
    # The request that produced the decision, kept separately from the result so
    # a replay can distinguish "what we asked" from "what came back".
    inputs: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False, server_default=text("'{}'::jsonb")
    )
    details: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False, server_default=text("'{}'::jsonb")
    )
    # Lets a future reader know which shape they are looking at.
    schema_version: Mapped[int] = mapped_column(Integer, nullable=False, server_default="1")

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )

    __table_args__ = (
        Index("ix_audit_log_tenant_id_occurred_at", "tenant_id", "occurred_at"),
        Index("ix_audit_log_tenant_id_conversation_id_seq", "tenant_id", "conversation_id", "seq"),
        Index(
            "ix_audit_log_tenant_id_event_type_occurred_at",
            "tenant_id",
            "event_type",
            "occurred_at",
        ),
        Index(
            "ix_audit_log_tenant_id_platform_slug_outcome", "tenant_id", "platform_slug", "outcome"
        ),
        Index("ix_audit_log_details", "details", postgresql_using="gin"),
    )


__all__ = [
    "Attribution",
    "AuditLog",
    "Booking",
    "Consent",
    "Conversation",
    "Feedback",
    "HandoffQueue",
    "Lead",
    "MerchantCredential",
    "Message",
    "RoutingConfig",
    "Tenant",
]
