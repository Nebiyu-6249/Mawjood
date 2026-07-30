"""Source attribution.

Where a consumer came from. Two carriers, one format:

* **Click-to-chat** — ``wa.me/9715XXXXXXXX?text=SRC12`` prefills the first message,
  so the code arrives as part of what they send.
* **QR codes** — the same link encoded, printed on a flyer, a table card, a
  shopfront. ``tools/make_qr.py`` generates them.

The code is parsed from the first inbound message and persisted against the lead
and the conversation, so attribution survives past the message that carried it.

The prefill is stripped from the text the state machine sees: a consumer who taps
a QR should not have to explain what "SRC12" meant, and the NLU should not try to
book one.
"""

from __future__ import annotations

import re
import uuid
from typing import TYPE_CHECKING

from sqlalchemy import select

from mawjood.core.enums import AttributionMedium
from mawjood.observability.logging import get_logger

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

    from mawjood.observability.audit import AuditTrail

log = get_logger(__name__)

# SRC followed by 2-16 alphanumerics. Word-anchored so it is not found inside an
# ordinary word, and case-insensitive because a printed flyer and a keyboard
# rarely agree.
SOURCE_PATTERN = re.compile(r"\bSRC([A-Z0-9]{2,16})\b", re.IGNORECASE)


def parse_source_code(text: str) -> str | None:
    """Extract a source code from an inbound message."""
    match = SOURCE_PATTERN.search(text or "")
    if match is None:
        return None
    return f"SRC{match.group(1).upper()}"


def strip_source_code(text: str) -> str:
    """Remove the prefill so the conversation engine sees only intent."""
    return SOURCE_PATTERN.sub("", text or "").strip()


def medium_for(text: str) -> AttributionMedium:
    """Infer the carrier.

    A bare code is a QR scan — nobody types "SRC12" unprompted. A code inside a
    longer message came from a click-to-chat prefill the consumer added to.
    """
    return AttributionMedium.QR if not strip_source_code(text) else AttributionMedium.WA_LINK


def build_wa_link(phone_e164: str, source_code: str) -> str:
    """The click-to-chat URL to print or encode."""
    digits = re.sub(r"\D", "", phone_e164)
    return f"https://wa.me/{digits}?text={source_code}"


async def capture_attribution(
    *,
    session: AsyncSession,
    tenant_id: uuid.UUID,
    lead_id: uuid.UUID,
    conversation_id: uuid.UUID,
    source_code: str,
    raw: str,
    audit: AuditTrail | None = None,
) -> None:
    """Record where this consumer came from.

    First touch is marked once and never overwritten: the campaign that first
    brought someone in is a different question from the one they most recently
    tapped, and conflating them makes both unanswerable.
    """
    from mawjood.db.models import Attribution

    existing = await session.execute(
        select(Attribution)
        .where(Attribution.tenant_id == tenant_id)
        .where(Attribution.lead_id == lead_id)
        .limit(1)
    )
    is_first_touch = existing.scalar_one_or_none() is None

    session.add(
        Attribution(
            tenant_id=tenant_id,
            lead_id=lead_id,
            conversation_id=conversation_id,
            source_code=source_code,
            medium=medium_for(raw),
            is_first_touch=is_first_touch,
            raw_payload=raw[:500],
        )
    )
    await session.flush()

    if audit is not None:
        await audit.record(
            "attribution.captured",
            details={
                "source_code": source_code,
                "medium": str(medium_for(raw)),
                "first_touch": is_first_touch,
            },
        )
    log.info("attribution.captured", source_code=source_code, first_touch=is_first_touch)


__all__ = [
    "SOURCE_PATTERN",
    "build_wa_link",
    "capture_attribution",
    "medium_for",
    "parse_source_code",
    "strip_source_code",
]
