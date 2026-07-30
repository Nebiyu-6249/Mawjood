"""Wati — documented stub, deliberately not implemented.

Wati's webhook envelope differs from Meta's and its authentication scheme is
not the ``X-Hub-Signature-256`` HMAC that 360dialog inherits. Wati's primary
documentation was not reachable for automated fetching during Phase 1, and
CLAUDE.md section 10 is explicit: do not guess a provider's contract from
memory — if the docs cannot be reached, say so and stop.

So this file registers cleanly, declares itself unimplemented, and refuses to
parse rather than mis-parse. A parser invented from memory would silently drop
or mangle real consumer messages, which is far worse than an obvious refusal.

## To finish this adapter

Confirm from Wati's live documentation and record in docs/INTEGRATION_NOTES.md:

1. The inbound webhook JSON envelope, and the field names carrying the sender
   identifier, the provider message id, and the text body.
2. How inbound requests are authenticated — header name, algorithm, what the
   signature covers, and whether a shared token is used instead of an HMAC.
3. Whether delivery statuses arrive on the same endpoint as messages.
4. The outbound send endpoint, its auth header, and its response shape.

Then replace the two methods below. Nothing outside this file needs to change.
"""

from __future__ import annotations

from collections.abc import Sequence

from mawjood.core.conversation.types import InboundMessage, OutboundMessage
from mawjood.services.bsp.base import DeliveryReceipt, SignatureScheme


class WatiNotConfigured(NotImplementedError):
    """Raised rather than guessing at Wati's contract."""


class WatiBSP:
    """Placeholder for Wati. Registers, declares capability, does not pretend."""

    slug = "wati"
    # Almost certainly wrong for Wati; carried so the type checks. Nothing reads
    # it while the adapter refuses to parse.
    signature_scheme = SignatureScheme()
    implemented = False

    def parse_inbound(self, raw_body: bytes, *, tenant_slug: str) -> Sequence[InboundMessage]:
        raise WatiNotConfigured(
            "The Wati adapter is a documented stub. Its payload shape and auth "
            "scheme have not been confirmed against live documentation, and "
            "guessing them would silently mangle real consumer messages. "
            "See the module docstring for what to confirm."
        )

    def parse_receipts(self, raw_body: bytes) -> Sequence[DeliveryReceipt]:
        raise WatiNotConfigured(
            "The Wati adapter is a documented stub. Its delivery-status payload "
            "has not been confirmed against live documentation."
        )

    async def send(self, outbound: OutboundMessage) -> str | None:
        raise WatiNotConfigured(
            "The Wati adapter is a documented stub; its send endpoint has not "
            "been confirmed against live documentation."
        )


__all__ = ["WatiBSP", "WatiNotConfigured"]
