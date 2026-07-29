"""The terminal transport.

This is what ``tools/chat_sim.py`` runs on. It is a real ``BSPAdapter``, not a
mock and not a parallel implementation: the console harness builds an
``InboundMessage`` and hands it to the same ``handle_inbound`` the WhatsApp
webhook calls, and gets the same ``TurnResult`` back.

That is the whole design intent. Demonstrating Mawjood before a number is
provisioned must not mean maintaining a second conversation engine that drifts
from the real one. ``tests/test_transport_parity.py`` asserts the two produce
identical output for identical input.

There is no signature to verify — there is no network. Sending is capture, so a
caller can assert on what would have gone out.
"""

from __future__ import annotations

import json
from collections.abc import Sequence
from typing import Any

from mawjood.core.conversation.types import InboundMessage, OutboundMessage
from mawjood.core.enums import Channel
from mawjood.services.bsp.base import SignatureScheme


class ConsoleBSP:
    """A transport backed by the terminal."""

    slug = "console"
    signature_scheme = SignatureScheme()

    def __init__(self) -> None:
        self.sent: list[OutboundMessage] = []

    def parse_inbound(self, raw_body: bytes, *, tenant_slug: str) -> Sequence[InboundMessage]:
        """Accepts ``{"wa_id": ..., "text": ...}``.

        Present so the console satisfies the same interface as every other
        provider; chat_sim normally builds InboundMessage directly.
        """
        payload: dict[str, Any] = json.loads(raw_body)
        return (
            InboundMessage(
                tenant_slug=tenant_slug,
                channel=Channel.CONSOLE,
                wa_id=str(payload["wa_id"]),
                text=str(payload["text"]),
                provider_message_id=payload.get("id"),
                raw=payload,
            ),
        )

    async def send(self, outbound: OutboundMessage) -> str | None:
        self.sent.append(outbound)
        return None

    @property
    def sent_texts(self) -> list[str]:
        return [message.text for message in self.sent]


__all__ = ["ConsoleBSP"]
