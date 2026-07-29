"""The inbound pipeline.

**This is the one code path.** The WhatsApp webhook and ``tools/chat_sim.py``
both call ``handle_inbound`` with an ``InboundMessage`` and both get a
``TurnResult`` back. Neither knows anything the other does not. If a behaviour is
worth having, it belongs here, where both transports get it for free — and
``tests/test_transport_parity.py`` fails the build if the two ever diverge.

What one turn does, in order:

1. Resolve the tenant.
2. Drop a redelivered provider message (webhooks are at-least-once).
3. Resolve or create the lead.
4. Open or resume the conversation.
5. Persist the inbound message.
6. Decide the reply as phrasebank *keys* — never text.
7. Record consent events, storing the exact wording that was sent.
8. Render, persist and return the outbound messages.

Every step writes to the audit trail on the same transaction, so the record and
the state can never disagree about what happened.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

from sqlalchemy.ext.asyncio import AsyncSession

from mawjood.core.conversation.consent import ConsentSignal, ConsentState, classify
from mawjood.core.conversation.phrasebank import (
    Phrasebank,
    PhraseKey,
    RenderedMessage,
    get_phrasebank,
)
from mawjood.core.conversation.responder import Responder, ScriptedResponder, TurnContext
from mawjood.core.conversation.types import InboundMessage, OutboundMessage, TurnResult
from mawjood.core.enums import ConsentEvent
from mawjood.db.repositories.core import (
    ConsentRepository,
    ConversationRepository,
    LeadRepository,
    MessageRepository,
    TenantRepository,
)
from mawjood.observability.audit import AuditTrail
from mawjood.observability.logging import get_logger

log = get_logger(__name__)


class UnknownTenant(LookupError):
    """The inbound message names a tenant that does not exist.

    A configuration fault, not a consumer-visible condition. It is raised so it
    is loud in logs and Sentry; the consumer never learns of it.
    """


async def handle_inbound(
    session: AsyncSession,
    inbound: InboundMessage,
    *,
    correlation_id: str,
    phrasebank: Phrasebank | None = None,
    responder: Responder | None = None,
    commit: bool = True,
) -> TurnResult:
    """Handle one inbound consumer message end to end."""
    phrasebank = phrasebank or get_phrasebank()
    responder = responder or ScriptedResponder()

    tenants = TenantRepository(session)
    tenant = await tenants.get_by_slug(inbound.tenant_slug)
    if tenant is None:
        raise UnknownTenant(f"no tenant with slug {inbound.tenant_slug!r}")

    audit = AuditTrail(session=session, tenant_id=tenant.id, correlation_id=correlation_id)

    leads = LeadRepository(session, tenant.id)
    conversations = ConversationRepository(session, tenant.id)
    messages = MessageRepository(session, tenant.id)
    consents = ConsentRepository(session, tenant.id)

    # --- 2. Redelivery -----------------------------------------------------
    if inbound.provider_message_id:
        seen = await messages.find_by_provider_id(inbound.channel, inbound.provider_message_id)
        if seen is not None:
            audit.bind(conversation_id=seen.conversation_id, lead_id=seen.lead_id)
            await audit.duplicate_ignored(
                provider_message_id=inbound.provider_message_id,
                channel=str(inbound.channel),
            )
            if commit:
                await session.commit()
            return TurnResult(
                turn_id=audit.turn_id,
                conversation_id=seen.conversation_id,
                lead_id=seen.lead_id,
                duplicate=True,
            )

    # --- 3. Lead -----------------------------------------------------------
    locale = inbound_locale(tenant.default_locale)
    lead, lead_created = await leads.get_or_create(inbound.wa_id, locale=locale)
    if inbound.display_name and not lead.display_name:
        lead.display_name = inbound.display_name
    audit.bind(lead_id=lead.id)
    if lead_created:
        await audit.lead_created()

    # --- 4. Conversation ---------------------------------------------------
    conversation, conversation_started = await conversations.open_or_resume(
        lead.id, inbound.channel, locale=lead.locale
    )
    audit.bind(conversation_id=conversation.id)
    if conversation_started:
        await audit.conversation_started(channel=str(inbound.channel))
    else:
        await audit.conversation_resumed(channel=str(inbound.channel))

    # --- 5. Inbound message ------------------------------------------------
    inbound_row = await messages.record_inbound(
        conversation_id=conversation.id,
        lead_id=lead.id,
        turn_id=audit.turn_id,
        channel=inbound.channel,
        body=inbound.text,
        locale=lead.locale,
        provider_message_id=inbound.provider_message_id,
    )
    await audit.message_received(
        body=inbound.text,
        provider_message_id=inbound.provider_message_id,
        channel=str(inbound.channel),
    )

    # --- 6. Decide the reply ----------------------------------------------
    consent_state = await consents.state_for_lead(lead.id)
    consent_signal = classify(inbound.text)

    keys = await responder.respond(
        TurnContext(
            text=inbound.text,
            is_new_lead=lead_created,
            is_new_conversation=conversation_started,
            consent_state=consent_state,
            consent_signal=consent_signal,
        )
    )

    rendered = [phrasebank.render(key, lead.locale) for key in keys]

    # --- 7. Consent events -------------------------------------------------
    await _record_consent(
        consents=consents,
        audit=audit,
        phrasebank=phrasebank,
        rendered=rendered,
        lead_id=lead.id,
        conversation_id=conversation.id,
        channel=inbound.channel,
        locale=lead.locale,
        prior_state=consent_state,
        signal=consent_signal,
        evidence_message_id=inbound_row.id,
        inbound_text=inbound.text,
    )

    # --- 8. Outbound -------------------------------------------------------
    outbound: list[OutboundMessage] = []
    for message in rendered:
        await messages.record_outbound(
            conversation_id=conversation.id,
            lead_id=lead.id,
            turn_id=audit.turn_id,
            channel=inbound.channel,
            body=message.text,
            locale=message.locale,
            phrasebank_key=message.key,
        )
        await audit.message_sent(
            phrasebank_key=message.key,
            locale=message.locale,
            text=message.text,
            channel=str(inbound.channel),
        )
        outbound.append(
            OutboundMessage(
                message=message,
                conversation_id=conversation.id,
                lead_id=lead.id,
                channel=inbound.channel,
            )
        )

    conversation.last_activity_at = datetime.now(UTC)

    if commit:
        await session.commit()

    return TurnResult(
        turn_id=audit.turn_id,
        outbound=tuple(outbound),
        conversation_id=conversation.id,
        lead_id=lead.id,
    )


def inbound_locale(tenant_default: str) -> str:
    """v1 is English only. Kept as a function so Phase N adds detection here
    rather than threading a new parameter through every caller."""
    return tenant_default


async def _record_consent(
    *,
    consents: ConsentRepository,
    audit: AuditTrail,
    phrasebank: Phrasebank,
    rendered: list[RenderedMessage],
    lead_id: uuid.UUID,
    conversation_id: uuid.UUID,
    channel: object,
    locale: str,
    prior_state: ConsentState,
    signal: ConsentSignal,
    evidence_message_id: uuid.UUID,
    inbound_text: str,
) -> None:
    """Write consent events for this turn.

    The notice is recorded **only when it is actually sent**, storing the text
    that was rendered rather than a lookup of what it should have been. PDPL asks
    what the consumer was shown; the only reliable answer is the string that went
    out of the door.
    """
    policy_version = phrasebank.policy_version(locale)

    for message in rendered:
        if message.key == PhraseKey.CONSENT_NOTICE:
            await consents.add(
                lead_id=lead_id,
                conversation_id=conversation_id,
                event=ConsentEvent.NOTICE_SHOWN,
                policy_version=policy_version,
                wording=message.text,
                locale=message.locale,
                channel=channel,
            )
            await audit.consent_notice_shown(
                policy_version=policy_version,
                wording=message.text,
                locale=message.locale,
            )

    if signal is ConsentSignal.WITHDRAWAL:
        await consents.add(
            lead_id=lead_id,
            conversation_id=conversation_id,
            event=ConsentEvent.WITHDRAWN,
            policy_version=policy_version,
            wording=phrasebank.raw(PhraseKey.CONSENT_NOTICE, locale),
            locale=locale,
            channel=channel,
            evidence_message_id=evidence_message_id,
        )
        await audit.consent_withdrawn(policy_version=policy_version, evidence=inbound_text)
        return

    if signal is ConsentSignal.AFFIRMATIVE and prior_state is ConsentState.NOTICE_SHOWN:
        await consents.add(
            lead_id=lead_id,
            conversation_id=conversation_id,
            event=ConsentEvent.GRANTED,
            policy_version=policy_version,
            wording=phrasebank.raw(PhraseKey.CONSENT_NOTICE, locale),
            locale=locale,
            channel=channel,
            evidence_message_id=evidence_message_id,
        )
        await audit.consent_granted(policy_version=policy_version, evidence=inbound_text)


__all__ = ["UnknownTenant", "handle_inbound"]
