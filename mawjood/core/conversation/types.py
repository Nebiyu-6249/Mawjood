"""Transport-neutral conversation types.

These are the boundary between "how a message arrived" and "what Mawjood does
about it". The WhatsApp webhook and the terminal harness both construct an
``InboundMessage`` and both receive a ``TurnResult`` — that is what keeps
``tools/chat_sim.py`` a transport rather than a second implementation.

Nothing here knows about HTTP, WhatsApp, or a BSP.
"""

from __future__ import annotations

import uuid
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from mawjood.core.conversation.phrasebank import RenderedMessage
from mawjood.core.enums import Channel


@dataclass(frozen=True, slots=True)
class InboundMessage:
    """One message from a consumer, normalised away from its transport."""

    tenant_slug: str
    channel: Channel
    wa_id: str
    text: str
    # None for transports without provider ids (the console). Present for
    # WhatsApp, where it is the idempotency key for at-least-once delivery.
    provider_message_id: str | None = None
    received_at: datetime = field(default_factory=lambda: datetime.now(UTC))
    display_name: str | None = None
    raw: Mapping[str, Any] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class OutboundMessage:
    """A reply, carrying proof of phrasebank origin.

    ``message`` is a RenderedMessage, which only the phrasebank can build. The
    send interface accepts nothing else.
    """

    message: RenderedMessage
    conversation_id: uuid.UUID
    lead_id: uuid.UUID
    channel: Channel

    @property
    def text(self) -> str:
        return self.message.text

    @property
    def phrasebank_key(self) -> str:
        return self.message.key


@dataclass(frozen=True, slots=True)
class TurnResult:
    """Everything one inbound message produced."""

    turn_id: uuid.UUID
    outbound: tuple[OutboundMessage, ...] = ()
    conversation_id: uuid.UUID | None = None
    lead_id: uuid.UUID | None = None
    # True when the provider redelivered a message already processed. The turn
    # is a no-op and nothing is sent again.
    duplicate: bool = False
    # Set when the turn budget expired mid-cascade. The holding pivot has already
    # gone out; awaiting this finishes the cascade and delivers the follow-up.
    # Same mechanism as the graceful pivot — one code path, both jobs.
    deferred: Any = None
    state: str | None = None

    @property
    def texts(self) -> tuple[str, ...]:
        return tuple(message.text for message in self.outbound)


__all__ = ["InboundMessage", "OutboundMessage", "TurnResult"]
