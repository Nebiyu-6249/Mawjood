"""The inbound pipeline.

**This is the one code path.** The WhatsApp webhook and ``tools/chat_sim.py`` both
call ``handle_inbound`` with an ``InboundMessage`` and both get a ``TurnResult``
back. Neither knows anything the other does not, and
``tests/test_transport_parity.py`` fails the build if they ever diverge.

One turn, in order:

1. Resolve the tenant.
2. Drop a redelivered provider message (webhooks are at-least-once).
3. Resolve or create the lead; capture source attribution on first contact.
4. Open or resume the conversation, restoring the state machine's position.
5. Persist the inbound message.
6. **Understand** the message (LLM or the deterministic understudy).
7. **Decide** the next state — an explicit transition, never the model's choice.
8. **Perform** the action: run the cascade, book, or hand to a human.
9. Record consent events, storing the exact wording that was sent.
10. Render, persist and return the outbound messages.

Every step writes to the audit trail on the same transaction, so the record and
the state can never disagree about what happened.
"""

from __future__ import annotations

import uuid
from collections.abc import Mapping
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from mawjood.core.aggregators.base import Deadline
from mawjood.core.aggregators.registry import AdapterRegistry, build_registry_for
from mawjood.core.attribution import (
    capture_attribution,
    parse_source_code,
    strip_source_code,
)
from mawjood.core.conversation.consent import ConsentSignal, ConsentState, classify
from mawjood.core.conversation.engine import ActionResult, TurnEngine
from mawjood.core.conversation.nlu import understand
from mawjood.core.conversation.phrasebank import (
    Phrasebank,
    PhraseKey,
    RenderedMessage,
    get_phrasebank,
)
from mawjood.core.conversation.states import (
    CollectedSlots,
    ConversationState,
    MachineInput,
    PhraseSpec,
    TurnAction,
    advance,
)
from mawjood.core.conversation.types import InboundMessage, OutboundMessage, TurnResult
from mawjood.core.enums import BookingStatus, Category, ConsentEvent, HandoffReason, PaymentStatus
from mawjood.core.notifications import Offsets, capture_satisfaction
from mawjood.core.routing.cascade import Cascade
from mawjood.core.routing.router import Router
from mawjood.db.repositories.core import (
    ConsentRepository,
    ConversationRepository,
    HandoffRepository,
    LeadRepository,
    MessageRepository,
    TenantRepository,
)
from mawjood.observability.audit import AuditTrail
from mawjood.observability.logging import get_logger
from mawjood.services.llm import LLMProvider, Turn, build_llm
from mawjood.services.secrets import CredentialResolver, StaticSecretStore, build_secret_store

log = get_logger(__name__)


class UnknownTenant(LookupError):
    """The inbound message names a tenant that does not exist.

    A configuration fault, not a consumer-visible condition. Raised so it is loud
    in logs and Sentry; the consumer never learns of it.
    """


async def handle_inbound(
    session: AsyncSession,
    inbound: InboundMessage,
    *,
    correlation_id: str,
    phrasebank: Phrasebank | None = None,
    llm: LLMProvider | None = None,
    registry: AdapterRegistry | None = None,
    resolver: CredentialResolver | None = None,
    settings: Any | None = None,
    commit: bool = True,
) -> TurnResult:
    """Handle one inbound consumer message end to end."""
    phrasebank = phrasebank or get_phrasebank()
    llm = llm or build_llm(settings) if settings is not None else (llm or build_llm(object()))
    registry = registry or build_registry_for(settings if settings is not None else object())
    resolver = resolver or CredentialResolver(
        build_secret_store(settings) if settings is not None else StaticSecretStore()
    )
    deadline_ms = int(getattr(settings, "turn_deadline_ms", 2500)) if settings else 2500
    timezone = str(getattr(settings, "timezone", "Asia/Dubai")) if settings else "Asia/Dubai"

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

    # --- 3. Lead and attribution -------------------------------------------
    lead, lead_created = await leads.get_or_create(inbound.wa_id, locale=tenant.default_locale)
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

    hours_idle = _hours_since(conversation.last_activity_at)

    source_code = parse_source_code(inbound.text)
    if source_code:
        await capture_attribution(
            session=session,
            tenant_id=tenant.id,
            lead_id=lead.id,
            conversation_id=conversation.id,
            source_code=source_code,
            raw=inbound.text,
            audit=audit,
        )

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

    # --- 5a. Bot mute (decision 7) -----------------------------------------
    # A human owns this thread. Mawjood records what the consumer says so the
    # operator sees it, and says nothing itself until released. Answering over
    # the top of a colleague is worse than silence.
    if conversation.bot_muted:
        await audit.record(
            "conversation.bot_muted_skip",
            details={"reason": "handed off to a human"},
        )
        conversation.last_activity_at = datetime.now(UTC)
        if commit:
            await session.commit()
        return TurnResult(
            turn_id=audit.turn_id,
            conversation_id=conversation.id,
            lead_id=lead.id,
            state=conversation.state,
        )

    # --- 6. Understand -----------------------------------------------------
    history = [
        Turn(role="user" if m.direction == "inbound" else "assistant", content=m.body)
        for m in await messages.history(conversation.id, limit=8)
    ]
    # A QR prefill is bookkeeping, not a request. Strip it so the engine reads
    # intent rather than trying to book a campaign code.
    understanding = await understand(
        llm, message=strip_source_code(inbound.text), history=history[:-1]
    )

    consent_state = await consents.state_for_lead(lead.id)
    consent_signal = classify(inbound.text)

    # The gate must see consent as of *this* message, not as of the last one. A
    # consumer who replies "yes" to a notice-plus-offer has consented on this
    # turn; making them say it twice is friction with no compliance benefit. The
    # event itself is still recorded below, from the pre-message state.
    effective_consent = (
        ConsentState.GRANTED
        if consent_signal is ConsentSignal.AFFIRMATIVE
        and consent_state is ConsentState.NOTICE_SHOWN
        else consent_state
    )

    # --- 6b. Satisfaction ---------------------------------------------------
    # A consumer answering the satisfaction ask sends "4" or "4, staff were
    # lovely". That is feedback, not a booking request, and it is captured here
    # before the state machine sees it — the machine has no state for "rating a
    # past visit" and would read a bare number as a slot choice.
    #
    # Only when we actually asked. Otherwise "book me 5 people" scores a 5.
    if await _awaiting_satisfaction(session, tenant.id, conversation.id):
        captured = await capture_satisfaction(
            session,
            tenant_id=tenant.id,
            conversation_id=conversation.id,
            lead_id=lead.id,
            text=inbound.text,
            audit=audit,
        )
        if captured is not None:
            thanks = phrasebank.render(PhraseKey.NOTIFY_SATISFACTION_THANKS, lead.locale)
            outbound = await _emit(
                messages=messages,
                audit=audit,
                rendered=[thanks],
                conversation_id=conversation.id,
                lead_id=lead.id,
                channel=inbound.channel,
                turn_id=audit.turn_id,
            )
            conversation.last_activity_at = datetime.now(UTC)
            if commit:
                await session.commit()
            return TurnResult(
                turn_id=audit.turn_id,
                outbound=tuple(outbound),
                conversation_id=conversation.id,
                lead_id=lead.id,
                state=conversation.state,
            )

    # --- 7. Decide ---------------------------------------------------------
    transition = advance(
        MachineInput(
            state=_state_of(conversation.state),
            collected=CollectedSlots.from_dict(conversation.collected_slots),
            understanding=understanding,
            consent_state=effective_consent,
            consent_signal=consent_signal,
            is_new_lead=lead_created,
            hours_since_last_activity=hours_idle,
            pending_offer=conversation.pending_offer or None,
            has_booking=_state_of(conversation.state)
            in (ConversationState.CONFIRMED, ConversationState.POST_BOOKING),
        )
    )

    # --- 8. Perform --------------------------------------------------------
    cascade = Cascade(
        audit=audit,
        deadline=Deadline.in_ms(deadline_ms),
        tenant_id=tenant.id,
        conversation_id=conversation.id,
        correlation_id=correlation_id,
    )
    router = Router(registry=registry, resolver=resolver, session=session, tenant_id=tenant.id)

    async def candidates_for(category: Category, area: str | None) -> list[Any]:
        return await router.candidates_for(category, area=area)

    engine = TurnEngine(cascade=cascade, candidates_for=candidates_for, timezone=timezone)

    if transition.action is TurnAction.BOOK:
        offer = dict(conversation.pending_offer or {})
        candidates = await router.candidates_for(
            Category(offer.get("category", Category.OTHER)), area=offer.get("area")
        )
        outcome = await engine.book_pending(
            pending_offer=offer,
            candidates=candidates,
            consumer_name=lead.display_name,
            consumer_phone=lead.phone_e164,
        )
    else:
        outcome = await engine.perform(transition)

    # --- Merge the transition and the action -------------------------------
    phrases: list[PhraseSpec] = [*transition.phrases, *outcome.phrases]
    next_state = outcome.state or transition.state
    conversation.state = str(next_state)
    conversation.collected_slots = transition.collected.as_dict()

    if outcome.pending_offer is not None:
        conversation.pending_offer = outcome.pending_offer
    elif outcome.clear_pending_offer or transition.clear_pending_offer:
        conversation.pending_offer = {}

    if outcome.booking is not None:
        await _record_booking(
            session=session,
            tenant_id=tenant.id,
            conversation_id=conversation.id,
            lead_id=lead.id,
            payload=outcome.booking,
            offsets=Offsets.from_settings(settings) if settings is not None else None,
        )

    handoff_reason = outcome.handoff_reason or transition.handoff_reason
    if handoff_reason:
        await _enqueue_handoff(
            session=session,
            tenant_id=tenant.id,
            conversation_id=conversation.id,
            lead_id=lead.id,
            reason=handoff_reason,
            audit=audit,
            context={
                "state": str(next_state),
                "collected": transition.collected.as_dict(),
            },
        )
        conversation.bot_muted = True

    # --- 9. Consent --------------------------------------------------------
    rendered = [phrasebank.render(spec.key, lead.locale, **spec.variables) for spec in phrases]
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

    # --- 10. Outbound ------------------------------------------------------
    outbound = await _emit(
        messages=messages,
        audit=audit,
        rendered=rendered,
        conversation_id=conversation.id,
        lead_id=lead.id,
        channel=inbound.channel,
        turn_id=audit.turn_id,
    )

    conversation.last_activity_at = datetime.now(UTC)
    if commit:
        await session.commit()

    return TurnResult(
        turn_id=audit.turn_id,
        outbound=tuple(outbound),
        conversation_id=conversation.id,
        lead_id=lead.id,
        deferred=outcome.deferred,
        state=str(next_state),
    )


async def deliver_deferred(
    session: AsyncSession,
    *,
    tenant_slug: str,
    conversation_id: uuid.UUID,
    lead_id: uuid.UUID,
    channel: Any,
    deferred: Any,
    correlation_id: str,
    phrasebank: Phrasebank | None = None,
) -> TurnResult:
    """Finish a cascade that outran the turn budget and deliver the follow-up.

    The consumer already has the holding pivot. This is the same mechanism as the
    graceful pivot, used for latency cover — one code path, both jobs.
    """
    phrasebank = phrasebank or get_phrasebank()
    tenant = await TenantRepository(session).get_by_slug(tenant_slug)
    if tenant is None:
        raise UnknownTenant(tenant_slug)

    audit = AuditTrail(session=session, tenant_id=tenant.id, correlation_id=correlation_id)
    audit.bind(conversation_id=conversation_id, lead_id=lead_id)

    result: ActionResult = await deferred()

    conversations = ConversationRepository(session, tenant.id)
    conversation = await conversations.get(conversation_id)
    if conversation is not None:
        if result.state is not None:
            conversation.state = str(result.state)
        if result.pending_offer is not None:
            conversation.pending_offer = result.pending_offer
        elif result.clear_pending_offer:
            conversation.pending_offer = {}

    if result.handoff_reason:
        await _enqueue_handoff(
            session=session,
            tenant_id=tenant.id,
            conversation_id=conversation_id,
            lead_id=lead_id,
            reason=result.handoff_reason,
            audit=audit,
            context={"deferred": True},
        )

    rendered = [phrasebank.render(spec.key, "en", **spec.variables) for spec in result.phrases]
    outbound = await _emit(
        messages=MessageRepository(session, tenant.id),
        audit=audit,
        rendered=rendered,
        conversation_id=conversation_id,
        lead_id=lead_id,
        channel=channel,
        turn_id=audit.turn_id,
    )
    await session.commit()
    return TurnResult(
        turn_id=audit.turn_id,
        outbound=tuple(outbound),
        conversation_id=conversation_id,
        lead_id=lead_id,
    )


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


async def _emit(
    *,
    messages: MessageRepository,
    audit: AuditTrail,
    rendered: list[RenderedMessage],
    conversation_id: uuid.UUID,
    lead_id: uuid.UUID,
    channel: Any,
    turn_id: uuid.UUID,
) -> list[OutboundMessage]:
    out: list[OutboundMessage] = []
    for message in rendered:
        await messages.record_outbound(
            conversation_id=conversation_id,
            lead_id=lead_id,
            turn_id=turn_id,
            channel=channel,
            body=message.text,
            locale=message.locale,
            phrasebank_key=message.key,
        )
        await audit.message_sent(
            phrasebank_key=message.key,
            locale=message.locale,
            text=message.text,
            channel=str(channel),
        )
        out.append(
            OutboundMessage(
                message=message,
                conversation_id=conversation_id,
                lead_id=lead_id,
                channel=channel,
            )
        )
    return out


async def _record_booking(
    *,
    session: AsyncSession,
    tenant_id: uuid.UUID,
    conversation_id: uuid.UUID,
    lead_id: uuid.UUID,
    payload: Mapping[str, Any],
    offsets: Any = None,
) -> None:
    from mawjood.core.notifications import schedule_for_booking
    from mawjood.db.models import Booking

    booking = Booking(
        tenant_id=tenant_id,
        conversation_id=conversation_id,
        lead_id=lead_id,
        category=Category(payload.get("category", Category.OTHER)),
        platform_slug=str(payload["platform_slug"]),
        external_booking_ref=str(payload["external_id"]),
        idempotency_key=f"{conversation_id}:{payload['external_id']}",
        status=BookingStatus.CONFIRMED,
        slot_start=datetime.fromisoformat(str(payload["slot_start"])),
        slot_end=datetime.fromisoformat(str(payload["slot_end"])),
        venue_name=payload.get("venue_name"),
        price_amount=payload.get("price_amount"),
        price_currency=payload.get("price_currency"),
        # v1 moves no money (decision 3).
        payment_status=PaymentStatus.PAY_AT_VENUE,
        confirmed_at=datetime.now(UTC),
    )
    session.add(booking)
    await session.flush()

    # Reminders become rows here, not timers. Written in the same transaction as
    # the booking, so a booking that exists always has its schedule and a crash
    # cannot leave a consumer booked but never reminded.
    await schedule_for_booking(session, tenant_id=tenant_id, booking=booking, offsets=offsets)


async def _awaiting_satisfaction(
    session: AsyncSession, tenant_id: uuid.UUID, conversation_id: uuid.UUID
) -> bool:
    """Whether the satisfaction ask actually went out on this conversation.

    Guards against scoring a message nobody asked for. Without it "book me 5
    people" parses as a five-star review, which is both wrong and a lost booking.
    """
    from mawjood.core.enums import Direction, NotificationKind
    from mawjood.core.notifications import PHRASE_FOR
    from mawjood.db.models import Message as MessageModel

    asked = await session.execute(
        select(MessageModel.id)
        .where(MessageModel.tenant_id == tenant_id)
        .where(MessageModel.conversation_id == conversation_id)
        .where(MessageModel.direction == Direction.OUTBOUND)
        .where(MessageModel.phrasebank_key == PHRASE_FOR[NotificationKind.SATISFACTION])
        .limit(1)
    )
    return asked.scalar_one_or_none() is not None


async def _enqueue_handoff(
    *,
    session: AsyncSession,
    tenant_id: uuid.UUID,
    conversation_id: uuid.UUID,
    lead_id: uuid.UUID,
    reason: str,
    audit: AuditTrail,
    context: dict[str, Any],
) -> None:
    try:
        parsed = HandoffReason(reason)
    except ValueError:
        parsed = HandoffReason.SYSTEM_ERROR
    await HandoffRepository(session, tenant_id).add(
        conversation_id=conversation_id,
        lead_id=lead_id,
        reason=parsed,
        context=context,
    )
    await audit.handoff_enqueued(reason=str(parsed), context=context)


def _state_of(raw: str | None) -> ConversationState:
    try:
        return ConversationState(raw or ConversationState.GREETING)
    except ValueError:
        return ConversationState.GREETING


def _hours_since(when: datetime | None) -> float:
    if when is None:
        return 0.0
    if when.tzinfo is None:
        when = when.replace(tzinfo=UTC)
    return max(0.0, (datetime.now(UTC) - when).total_seconds() / 3600)


async def _record_consent(
    *,
    consents: ConsentRepository,
    audit: AuditTrail,
    phrasebank: Phrasebank,
    rendered: list[RenderedMessage],
    lead_id: uuid.UUID,
    conversation_id: uuid.UUID,
    channel: Any,
    locale: str,
    prior_state: ConsentState,
    signal: ConsentSignal,
    evidence_message_id: uuid.UUID,
    inbound_text: str,
) -> None:
    """Write consent events for this turn.

    The notice is recorded **only when it is actually sent**, storing the text that
    was rendered rather than a lookup of what it should have been. PDPL asks what
    the consumer was shown; the only reliable answer is the string that went out.
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


__all__ = ["UnknownTenant", "deliver_deferred", "handle_inbound"]
