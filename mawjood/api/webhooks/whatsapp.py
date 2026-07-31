"""Inbound WhatsApp webhook.

Provider-agnostic: this module verifies a signature, asks the configured
``BSPAdapter`` to normalise the payload, and hands each message to
``handle_inbound``. It contains no vendor-specific parsing and no conversation
logic — the former lives in ``services/bsp/``, the latter in
``core/conversation/pipeline.py``, which the terminal harness drives too.

Three deliberate response choices:

* **A bad signature is 403 and nothing else happens.** Not a 200-and-ignore:
  silently swallowing forged traffic hides an attack in progress.
* **A payload that parses to no actionable message is 200.** Delivery receipts
  and read markers are most of a webhook's traffic. Returning an error would make
  the provider retry them forever.
* **A handler failure is still 200 once the message is persisted.** Providers
  retry non-2xx, and a retry storm on a poison message is its own outage.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from typing import Any

from fastapi import APIRouter, Request, Response, status
from fastapi.responses import JSONResponse, PlainTextResponse
from sqlalchemy import select

from mawjood.api.ratelimit import Limit, RateLimiter, client_address
from mawjood.config import Settings
from mawjood.core.conversation.pipeline import UnknownTenant, handle_inbound
from mawjood.core.enums import MessageStatus
from mawjood.db.models import Message
from mawjood.observability.logging import get_logger
from mawjood.services.bsp.base import SignatureResult, verify_signature

router = APIRouter(prefix="/webhooks", tags=["webhooks"])
log = get_logger(__name__)


@router.get("/whatsapp", response_class=PlainTextResponse)
async def verify_subscription(request: Request) -> Response:
    """The provider's subscription handshake.

    The provider calls this once with a challenge and the shared verify token.
    Echo the challenge only if the token matches.
    """
    settings: Settings = request.app.state.settings
    params = request.query_params

    mode = params.get("hub.mode")
    token = params.get("hub.verify_token")
    challenge = params.get("hub.challenge")

    if settings.bsp_verify_token is None:
        log.error("webhook.verify_token_not_configured")
        return PlainTextResponse("verification not configured", status_code=503)

    expected = settings.bsp_verify_token.get_secret_value()
    if mode == "subscribe" and token == expected and challenge is not None:
        log.info("webhook.subscription_verified")
        return PlainTextResponse(challenge, status_code=200)

    log.warning("webhook.subscription_rejected", mode=mode)
    return PlainTextResponse("verification failed", status_code=403)


@router.post("/whatsapp")
async def receive(request: Request) -> Response:
    """Receive a batch of inbound messages."""
    settings: Settings = request.app.state.settings
    bsp = request.app.state.bsp
    limiter: RateLimiter = request.app.state.rate_limiter
    correlation_id = request.headers.get("x-request-id") or uuid.uuid4().hex
    headers = dict(request.headers)

    # --- Rate limit, before anything expensive ------------------------------
    # Applied ahead of the signature check because rejecting a forged body still
    # costs an HMAC, and the point is to bound the cost of traffic that will be
    # rejected anyway.
    source = client_address(headers, request.client.host if request.client else None)
    if not limiter.allow(
        "webhook.source", source, Limit(per_minute=settings.webhook_rate_per_minute)
    ):
        log.warning("webhook.rate_limited", scope="source")
        return JSONResponse(
            {"status": "rate_limited"},
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            headers={"Retry-After": "60"},
        )

    # --- Body size ----------------------------------------------------------
    # Checked from the header first so an oversized body is refused before it is
    # read into memory. Content-Length is a claim, not a fact, so the actual
    # bytes are checked again below.
    declared = headers.get("content-length")
    max_bytes = settings.webhook_max_body_bytes
    if declared and declared.isdigit() and int(declared) > max_bytes:
        log.warning("webhook.body_too_large", declared=int(declared), limit=max_bytes)
        return JSONResponse(
            {"status": "rejected", "reason": "payload too large"},
            status_code=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
        )

    # The signature covers the bytes as sent. Parsing first and re-serialising
    # would change whitespace and key order and invalidate every signature.
    raw_body = await request.body()
    if len(raw_body) > max_bytes:
        log.warning("webhook.body_too_large", actual=len(raw_body), limit=max_bytes)
        return JSONResponse(
            {"status": "rejected", "reason": "payload too large"},
            status_code=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
        )

    signature = _check_signature(settings, bsp, raw_body, headers)
    if not signature.ok:
        log.warning(
            "webhook.signature_rejected",
            reason=str(signature),
            provider=getattr(bsp, "slug", "unknown"),
        )
        return JSONResponse(
            {"status": "rejected", "reason": str(signature)},
            status_code=status.HTTP_403_FORBIDDEN,
        )

    try:
        inbound_messages = bsp.parse_inbound(raw_body, tenant_slug=settings.default_tenant_slug)
    except Exception:
        # A payload we cannot parse is not worth an infinite provider retry.
        log.exception("webhook.parse_failed", provider=getattr(bsp, "slug", "unknown"))
        return JSONResponse({"status": "accepted", "handled": 0}, status_code=200)

    session_factory = request.app.state.session_factory

    # Delivery statuses ride the same endpoint and one body can carry both, so
    # they are handled before the early return rather than after it.
    receipts_applied = await _apply_receipts(bsp, raw_body, session_factory, correlation_id)

    if not inbound_messages:
        # Statuses, read markers, and payloads carrying nothing actionable.
        return JSONResponse(
            {"status": "accepted", "handled": 0, "receipts": receipts_applied}, status_code=200
        )

    results: list[dict[str, Any]] = []

    for inbound in inbound_messages:
        # Per-consumer limit. One stuck client resending, or a script, must not
        # be able to drive unbounded database work. A throttled message is
        # dropped rather than answered: the consumer sees nothing, which is the
        # invariant, and the provider will redeliver.
        if not limiter.allow(
            "webhook.consumer",
            inbound.wa_id,
            Limit(per_minute=settings.consumer_rate_per_minute),
        ):
            log.warning("webhook.rate_limited", scope="consumer")
            continue

        async with session_factory() as session:
            try:
                # settings must be passed. Without it the pipeline falls back to
                # its own defaults and every configured value is silently
                # ignored on the live path — the adapter registry comes up
                # empty, the LLM provider reverts, the turn budget and timezone
                # revert. chat_sim passed settings and this did not, which is
                # precisely the "two code paths" failure the parity suite exists
                # to prevent.
                turn = await handle_inbound(
                    session, inbound, correlation_id=correlation_id, settings=settings
                )
            except UnknownTenant:
                log.exception("webhook.unknown_tenant", tenant=inbound.tenant_slug)
                await session.rollback()
                continue
            except Exception:
                log.exception("webhook.turn_failed")
                await session.rollback()
                continue

        results.append(
            {
                "turn_id": str(turn.turn_id),
                "duplicate": turn.duplicate,
                "replies": len(turn.outbound),
            }
        )

    return JSONResponse(
        {
            "status": "accepted",
            "handled": len(results),
            "turns": results,
            "receipts": receipts_applied,
        },
        status_code=200,
    )


async def _apply_receipts(
    bsp: Any, raw_body: bytes, session_factory: Any, correlation_id: str
) -> int:
    """Record what became of messages we sent. Returns how many were applied.

    Delivery status never reaches a consumer. A failed send is an operations
    problem, and telling someone "your message failed" is precisely the failure
    language the invariant exists to prevent — so this writes to ``messages`` and
    the log, and says nothing.

    Statuses arrive out of order and get replayed, so a receipt only ever moves a
    message forward: a late "sent" must not undo a "read".
    """
    parse = getattr(bsp, "parse_receipts", None)
    if parse is None:
        return 0
    try:
        receipts = parse(raw_body)
    except Exception:
        log.exception("webhook.receipt_parse_failed", provider=getattr(bsp, "slug", "unknown"))
        return 0
    if not receipts:
        return 0

    applied = 0
    async with session_factory() as session:
        for receipt in receipts:
            message = (
                await session.execute(
                    select(Message).where(
                        Message.provider_message_id == receipt.provider_message_id
                    )
                )
            ).scalar_one_or_none()
            if message is None:
                # A status for something we did not send, or sent before this
                # database existed. Not an error.
                continue
            if _RANK[receipt.status] <= _RANK.get(message.status, -1):
                continue

            message.status = receipt.status
            if receipt.status is MessageStatus.SENT and message.sent_at is None:
                message.sent_at = receipt.occurred_at or datetime.now(UTC)
            applied += 1

            if receipt.status is MessageStatus.FAILED:
                log.error(
                    "webhook.delivery_failed",
                    provider_message_id=receipt.provider_message_id,
                    reason=receipt.failed_reason,
                    correlation_id=correlation_id,
                )
        await session.commit()
    return applied


# Delivery is a one-way ratchet. Providers replay statuses and deliver them out
# of order, so a late "sent" must never undo a "read".
_RANK: dict[MessageStatus, int] = {
    MessageStatus.RECEIVED: 0,
    MessageStatus.QUEUED: 1,
    MessageStatus.SENT: 2,
    MessageStatus.DELIVERED: 3,
    MessageStatus.READ: 4,
    # Terminal, and ranked above delivered so a genuine failure is not masked by
    # a stale optimistic status.
    MessageStatus.FAILED: 5,
}


def _check_signature(
    settings: Settings, bsp: Any, raw_body: bytes, headers: dict[str, str]
) -> SignatureResult:
    if not settings.bsp_require_signature:
        # A Settings validator refuses this combination in production.
        return SignatureResult.SKIPPED

    secret = (
        settings.bsp_webhook_secret.get_secret_value()
        if settings.bsp_webhook_secret is not None
        else None
    )
    return verify_signature(
        scheme=bsp.signature_scheme,
        secret=secret,
        raw_body=raw_body,
        headers=headers,
    )


__all__ = ["router"]
