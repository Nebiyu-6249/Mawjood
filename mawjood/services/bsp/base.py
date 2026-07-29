"""The BSP abstraction.

A Business Solution Provider is whoever carries WhatsApp traffic for us —
360dialog, Wati, or (for local work) the terminal. The rest of Mawjood must not
know which one is in play, so everything provider-shaped lives behind this
interface: signature verification, payload parsing, sending.

Adding a provider is one file implementing ``BSPAdapter``.

> **Verification status.** The signature scheme implemented here is Meta's
> ``X-Hub-Signature-256``: HMAC-SHA256 over the *raw* request body, keyed with the
> app secret, hex-encoded behind a ``sha256=`` prefix. 360dialog forwards Meta's
> payloads. The primary vendor documentation for both Meta and 360dialog returned
> HTTP 403 to automated fetches, so this scheme is corroborated but **not
> confirmed against the vendor's own docs** — see docs/INTEGRATION_NOTES.md. The
> header name, prefix and digest are therefore configuration, not constants, so
> correcting them is a one-line change rather than a rewrite.
"""

from __future__ import annotations

import hashlib
import hmac
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from enum import StrEnum, auto
from typing import Protocol, runtime_checkable

from mawjood.core.conversation.types import InboundMessage, OutboundMessage


class SignatureResult(StrEnum):
    """Why a webhook was accepted or refused.

    Distinguished rather than collapsed into a boolean so logs can tell a
    misconfiguration apart from an attack.
    """

    VALID = auto()
    MISSING_HEADER = auto()
    MALFORMED_HEADER = auto()
    MISMATCH = auto()
    NOT_CONFIGURED = auto()
    # Verification deliberately switched off. Refused outright in production by
    # a Settings validator.
    SKIPPED = auto()

    @property
    def ok(self) -> bool:
        return self in (SignatureResult.VALID, SignatureResult.SKIPPED)


@dataclass(frozen=True, slots=True)
class SignatureScheme:
    """How a provider signs its webhooks."""

    header: str = "x-hub-signature-256"
    prefix: str = "sha256="
    digest: str = "sha256"


def verify_signature(
    *,
    scheme: SignatureScheme,
    secret: str | None,
    raw_body: bytes,
    headers: Mapping[str, str],
) -> SignatureResult:
    """Verify an inbound webhook signature.

    Three things this gets right on purpose:

    * It hashes the **raw body**, not a re-serialised parse. JSON round-tripping
      changes whitespace and key order and would break every signature.
    * It compares with :func:`hmac.compare_digest`, so a wrong signature takes
      the same time as a right one and cannot be guessed byte by byte.
    * A missing secret is ``NOT_CONFIGURED``, never a pass. Failing open on a
      webhook means accepting anything anyone posts at the URL.
    """
    if secret is None or not secret:
        return SignatureResult.NOT_CONFIGURED

    # Header lookup is case-insensitive; Starlette already lowercases, but a
    # direct caller might not.
    provided: str | None = None
    for name, value in headers.items():
        if name.lower() == scheme.header.lower():
            provided = value
            break

    if provided is None:
        return SignatureResult.MISSING_HEADER

    provided = provided.strip()
    if scheme.prefix and not provided.startswith(scheme.prefix):
        return SignatureResult.MALFORMED_HEADER

    supplied_hex = provided[len(scheme.prefix) :] if scheme.prefix else provided
    if not supplied_hex:
        return SignatureResult.MALFORMED_HEADER

    expected_hex = hmac.new(
        secret.encode("utf-8"), raw_body, getattr(hashlib, scheme.digest)
    ).hexdigest()

    if hmac.compare_digest(expected_hex, supplied_hex.lower()):
        return SignatureResult.VALID
    return SignatureResult.MISMATCH


def sign_payload(*, scheme: SignatureScheme, secret: str, raw_body: bytes) -> str:
    """Produce a signature header value. Used by tests and by chat_sim's
    ``--via-webhook`` mode, never in the request path."""
    digest = hmac.new(secret.encode("utf-8"), raw_body, getattr(hashlib, scheme.digest)).hexdigest()
    return f"{scheme.prefix}{digest}"


@runtime_checkable
class BSPAdapter(Protocol):
    """One messaging provider."""

    slug: str
    signature_scheme: SignatureScheme

    def parse_inbound(self, raw_body: bytes, *, tenant_slug: str) -> Sequence[InboundMessage]:
        """Normalise a provider payload into transport-neutral messages.

        Returns an empty sequence for payloads that are valid but carry nothing
        actionable — delivery receipts, status updates, read markers. Those are
        the majority of webhook traffic and must not be errors.
        """
        ...

    async def send(self, outbound: OutboundMessage) -> str | None:
        """Deliver a message. Returns the provider's message id, if any.

        Accepts an ``OutboundMessage``, which wraps a ``RenderedMessage``, which
        only the phrasebank can construct. There is no way to hand this method a
        raw string.
        """
        ...


__all__ = [
    "BSPAdapter",
    "SignatureResult",
    "SignatureScheme",
    "sign_payload",
    "verify_signature",
]
