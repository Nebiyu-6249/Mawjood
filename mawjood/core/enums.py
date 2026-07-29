"""Domain enumerations.

These are the vocabulary of the system: they appear in the schema, in the audit
trail, and (for Outcome and Category) in the adapter contract that Phase 2 builds
on. Defining them once here means the contract imports them rather than declaring
a parallel set that can drift.

All are StrEnum so they serialise to readable strings in JSONB, in logs, and in
the audit trail. An audit row read months from now should be legible without a
lookup table.
"""

from __future__ import annotations

from enum import StrEnum


class Channel(StrEnum):
    """How a consumer is talking to Mawjood."""

    WHATSAPP = "whatsapp"
    # The terminal harness (tools/chat_sim.py). A transport, not a second
    # pipeline — it drives exactly the same handlers as the webhook.
    CONSOLE = "console"


class Direction(StrEnum):
    INBOUND = "inbound"
    OUTBOUND = "outbound"


class MessageStatus(StrEnum):
    RECEIVED = "received"
    QUEUED = "queued"
    SENT = "sent"
    DELIVERED = "delivered"
    READ = "read"
    FAILED = "failed"


class ConversationStatus(StrEnum):
    ACTIVE = "active"
    AWAITING_HANDOFF = "awaiting_handoff"
    HANDED_OFF = "handed_off"
    CLOSED = "closed"


class ConsentEvent(StrEnum):
    """PDPL consent is recorded as an append-only event stream.

    Current state is derived from the events, never overwritten, so the history
    of what a consumer was shown and when survives intact.
    """

    NOTICE_SHOWN = "notice_shown"
    GRANTED = "granted"
    WITHDRAWN = "withdrawn"


class Category(StrEnum):
    """What a consumer wants. Drives routing_config lookup."""

    SALON = "salon"
    SPA = "spa"
    RESTAURANT = "restaurant"
    FOOD_DELIVERY = "food_delivery"
    RIDE = "ride"
    OTHER = "other"


class Outcome(StrEnum):
    """The adapter contract's typed outcome. See CLAUDE.md section 4.

    Adapters never raise into the router; every call reports one of these. The
    router decides what an outcome means, the adapter only reports it.
    """

    OK = "ok"
    NO_AVAILABILITY = "no_availability"
    LOW_CONFIDENCE = "low_confidence"
    TIMEOUT = "timeout"
    AUTH_ERROR = "auth_error"
    RATE_LIMITED = "rate_limited"
    UPSTREAM_ERROR = "upstream_error"
    UNSUPPORTED = "unsupported"


class BookingStatus(StrEnum):
    PENDING = "pending"
    CONFIRMED = "confirmed"
    # create_booking timed out and reconciliation could not determine whether the
    # booking landed. Goes to human handoff; never silently retried elsewhere.
    UNCERTAIN = "uncertain"
    CANCELLED = "cancelled"
    FAILED = "failed"


class PaymentStatus(StrEnum):
    """v1 moves no money (decision 3). Present so deposits later are a feature
    rather than a migration of every booking row."""

    NOT_APPLICABLE = "not_applicable"
    PAY_AT_VENUE = "pay_at_venue"
    PENDING = "pending"
    PAID = "paid"
    REFUNDED = "refunded"


class HandoffReason(StrEnum):
    CASCADE_EXHAUSTED = "cascade_exhausted"
    UNSUPPORTED_ACTION = "unsupported_action"
    RECONCILIATION_INCONCLUSIVE = "reconciliation_inconclusive"
    CONSUMER_REQUEST = "consumer_request"
    CONSENT_WITHDRAWN = "consent_withdrawn"
    SYSTEM_ERROR = "system_error"


class HandoffStatus(StrEnum):
    OPEN = "open"
    CLAIMED = "claimed"
    RESOLVED = "resolved"
    ABANDONED = "abandoned"


class AttributionMedium(StrEnum):
    WA_LINK = "wa_link"
    QR = "qr"
    DIRECT = "direct"
    UNKNOWN = "unknown"


class Actor(StrEnum):
    """Who caused an audit event."""

    SYSTEM = "system"
    CONSUMER = "consumer"
    OPERATOR = "operator"
    AGGREGATOR = "aggregator"


class AuditEvent(StrEnum):
    """Audit event vocabulary.

    The audit_log column itself is unconstrained text on purpose (see
    db/models.py): an audit write must never fail because of an unknown value.
    This enum is the set we deliberately emit.
    """

    # Message lifecycle
    MESSAGE_RECEIVED = "message.received"
    MESSAGE_DUPLICATE_IGNORED = "message.duplicate_ignored"
    MESSAGE_SENT = "message.sent"

    # Lead and conversation lifecycle
    LEAD_CREATED = "lead.created"
    CONVERSATION_STARTED = "conversation.started"
    CONVERSATION_RESUMED = "conversation.resumed"

    # PDPL consent
    CONSENT_NOTICE_SHOWN = "consent.notice_shown"
    CONSENT_GRANTED = "consent.granted"
    CONSENT_WITHDRAWN = "consent.withdrawn"

    # Attribution
    ATTRIBUTION_CAPTURED = "attribution.captured"

    # Routing and the cascade (emitted from Phase 2)
    ROUTING_STARTED = "routing.started"
    ROUTING_ATTEMPT = "routing.attempt"
    ROUTING_ADVANCED = "routing.advanced"
    ROUTING_SUCCEEDED = "routing.succeeded"
    ROUTING_EXHAUSTED = "routing.exhausted"
    ROUTING_DEADLINE_EXCEEDED = "routing.deadline_exceeded"

    # Booking safety (Phase 2)
    BOOKING_RECONCILIATION_STARTED = "booking.reconciliation_started"
    BOOKING_RECONCILIATION_RESULT = "booking.reconciliation_result"

    # Handoff
    HANDOFF_ENQUEUED = "handoff.enqueued"
    HANDOFF_CLAIMED = "handoff.claimed"
    HANDOFF_RESOLVED = "handoff.resolved"

    # Compliance
    DATA_ERASURE_REQUESTED = "compliance.erasure_requested"
    DATA_ERASURE_COMPLETED = "compliance.erasure_completed"


__all__ = [
    "Actor",
    "AttributionMedium",
    "AuditEvent",
    "BookingStatus",
    "Category",
    "Channel",
    "ConsentEvent",
    "ConversationStatus",
    "Direction",
    "HandoffReason",
    "HandoffStatus",
    "MessageStatus",
    "Outcome",
    "PaymentStatus",
]
