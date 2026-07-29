"""360dialog / Meta Cloud API shaped webhooks.

360dialog forwards Meta's WhatsApp Cloud API payloads, so one parser serves both.
The envelope is::

    {"object": "whatsapp_business_account",
     "entry": [{"id": ...,
                "changes": [{"field": "messages",
                             "value": {"messaging_product": "whatsapp",
                                       "metadata": {...},
                                       "contacts": [{"wa_id": ..., "profile": {...}}],
                                       "messages": [{"id": ..., "from": ...,
                                                     "type": "text",
                                                     "text": {"body": ...}}]}}]}]}

Most webhook traffic is *statuses* (sent / delivered / read), not messages. Those
parse to nothing and must not be treated as errors.

> **Unverified.** Meta's and 360dialog's documentation both returned HTTP 403 to
> automated fetching, so this shape is written from a corroborated but
> second-hand description. It is exercised only against fixtures. Confirm against
> live docs and a real sandbox before production — see docs/INTEGRATION_NOTES.md.
"""

from __future__ import annotations

import json
from collections.abc import Sequence
from typing import Any

import httpx

from mawjood.core.conversation.types import InboundMessage, OutboundMessage
from mawjood.core.enums import Channel
from mawjood.observability.logging import get_logger
from mawjood.services.bsp.base import SignatureScheme

log = get_logger(__name__)

# Message types Mawjood can act on today. Everything else (image, audio,
# location, sticker...) is recognised but not yet handled, and is dropped here
# rather than half-processed downstream.
_TEXT_TYPES = frozenset({"text"})


class WhatsAppCloudBSP:
    """360dialog and Meta Cloud API."""

    slug = "360dialog"
    signature_scheme = SignatureScheme()

    def __init__(self, api_base: str | None = None, api_key: str | None = None) -> None:
        self._api_base = api_base
        self._api_key = api_key

    def parse_inbound(self, raw_body: bytes, *, tenant_slug: str) -> Sequence[InboundMessage]:
        try:
            payload: dict[str, Any] = json.loads(raw_body)
        except json.JSONDecodeError:
            log.warning("bsp.payload_not_json", provider=self.slug, size=len(raw_body))
            return ()

        messages: list[InboundMessage] = []
        for entry in _as_list(payload.get("entry")):
            for change in _as_list(entry.get("changes")):
                value = change.get("value") or {}
                # Map wa_id -> profile name so a message can be attributed.
                names = {
                    str(contact.get("wa_id")): (contact.get("profile") or {}).get("name")
                    for contact in _as_list(value.get("contacts"))
                    if contact.get("wa_id")
                }
                for message in _as_list(value.get("messages")):
                    parsed = self._parse_message(message, names, tenant_slug)
                    if parsed is not None:
                        messages.append(parsed)
        return tuple(messages)

    def _parse_message(
        self, message: dict[str, Any], names: dict[str, str | None], tenant_slug: str
    ) -> InboundMessage | None:
        message_type = str(message.get("type", ""))
        if message_type not in _TEXT_TYPES:
            log.info("bsp.unsupported_message_type", provider=self.slug, type=message_type)
            return None

        wa_id = message.get("from")
        message_id = message.get("id")
        body = (message.get("text") or {}).get("body")
        if not wa_id or not message_id or body is None:
            log.warning("bsp.message_missing_fields", provider=self.slug)
            return None

        return InboundMessage(
            tenant_slug=tenant_slug,
            channel=Channel.WHATSAPP,
            wa_id=str(wa_id),
            text=str(body),
            provider_message_id=str(message_id),
            display_name=names.get(str(wa_id)),
            raw=message,
        )

    async def send(self, outbound: OutboundMessage) -> str | None:
        """POST a text message back to the provider.

        Untested against a live API — no credentials exist yet (see docs/KEYS.md).
        Until they do, the pipeline persists outbound messages as ``queued`` and
        this method is not reached.
        """
        if not self._api_base or not self._api_key:
            raise RuntimeError(
                "360dialog send requires bsp_api_base and bsp_api_key; "
                "outbound messages remain queued until credentials are configured"
            )
        async with httpx.AsyncClient(base_url=self._api_base, timeout=10.0) as client:
            response = await client.post(
                "/messages",
                headers={"D360-API-KEY": self._api_key},
                json={
                    "messaging_product": "whatsapp",
                    "recipient_type": "individual",
                    "to": str(outbound.lead_id),
                    "type": "text",
                    "text": {"body": outbound.text},
                },
            )
            response.raise_for_status()
            data: dict[str, Any] = response.json()
        sent = _as_list(data.get("messages"))
        return str(sent[0]["id"]) if sent else None


def _as_list(value: Any) -> list[Any]:
    """Coerce a possibly-missing, possibly-scalar field into a list.

    Webhook payloads from the wild are not reliably shaped, and a parser that
    raises on an unexpected null turns a consumer's message into a 500.
    """
    if value is None:
        return []
    if isinstance(value, list):
        return value
    return [value]


__all__ = ["WhatsAppCloudBSP"]
