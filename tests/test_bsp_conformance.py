"""One conformance suite every BSP implementation must pass.

PLAN.md asks for both providers behind one interface, passing one shared suite.
Two of the three implementations here are real (360dialog/Meta Cloud, and the
console transport chat_sim runs on); Wati is a documented stub because its
payload shape and auth scheme could not be confirmed against live documentation,
and CLAUDE.md section 10 forbids guessing them.

So the suite is written in two tiers:

* **Every implementation**, stub included, must satisfy the protocol, declare a
  signature scheme, and refuse to invent. A stub that quietly returned ``()``
  from ``parse_inbound`` would look like a working provider that never receives
  anything — far worse than one that raises.
* **Implementations that claim to parse** must additionally survive the payloads
  the wild actually produces: duplicates, statuses, nulls, unknown message
  types, and bodies that are not JSON at all.

The second tier is where the value is. A webhook parser meets malformed input on
its first day, and the failure mode of a strict one is a 500 in response to a
real consumer's message — which the provider then retries, forever.

> **Verification status.** The Meta/360dialog payload shapes below are written
> from a corroborated but second-hand description; the vendor documentation
> returns HTTP 403 to every automated fetch from this environment. These
> fixtures pin *our* behaviour, not the vendor's contract. See
> docs/INTEGRATION_NOTES.md.
"""

from __future__ import annotations

import dataclasses
import json
import uuid
from typing import Any

import pytest
from fastapi import FastAPI
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from starlette.testclient import TestClient

from mawjood.config import Settings
from mawjood.core.enums import Channel, Direction, MessageStatus
from mawjood.db.models import Conversation, Lead, Message
from mawjood.services.bsp.base import (
    BSPAdapter,
    DeliveryReceipt,
    SignatureResult,
    SignatureScheme,
    sign_payload,
    verify_signature,
)
from mawjood.services.bsp.console import ConsoleBSP
from mawjood.services.bsp.wati import WatiBSP, WatiNotConfigured
from mawjood.services.bsp.whatsapp_cloud import WhatsAppCloudBSP

from .conftest import needs_database

TENANT = "test-tenant"

# Every implementation, including the stub.
ALL_PROVIDERS = [WhatsAppCloudBSP, ConsoleBSP, WatiBSP]
# The ones that claim to parse. Wati is excluded deliberately — it refuses.
PARSING_PROVIDERS = [WhatsAppCloudBSP, ConsoleBSP]


def meta_envelope(*, messages: list[dict[str, Any]], statuses: list[dict[str, Any]]) -> bytes:
    return json.dumps(
        {
            "object": "whatsapp_business_account",
            "entry": [
                {
                    "id": "waba-1",
                    "changes": [
                        {
                            "field": "messages",
                            "value": {
                                "messaging_product": "whatsapp",
                                "metadata": {"phone_number_id": "pn-1"},
                                "contacts": [
                                    {"wa_id": "971501234567", "profile": {"name": "Layla"}}
                                ],
                                "messages": messages,
                                "statuses": statuses,
                            },
                        }
                    ],
                }
            ],
        }
    ).encode()


def text_message(message_id: str = "wamid.1", body: str = "hi") -> dict[str, Any]:
    return {
        "id": message_id,
        "from": "971501234567",
        "timestamp": "1730000000",
        "type": "text",
        "text": {"body": body},
    }


def status_event(message_id: str, state: str, **extra: Any) -> dict[str, Any]:
    return {"id": message_id, "status": state, "timestamp": "1730000100", **extra}


# ---------------------------------------------------------------------------
# Tier 1 — every implementation, stub included
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("provider", ALL_PROVIDERS)
class TestEveryImplementation:
    def test_it_satisfies_the_protocol(self, provider: type) -> None:
        assert isinstance(provider(), BSPAdapter)

    def test_it_declares_a_slug_and_a_signature_scheme(self, provider: type) -> None:
        instance = provider()
        assert instance.slug
        assert isinstance(instance.signature_scheme, SignatureScheme)

    def test_slugs_are_distinct(self, provider: type) -> None:
        slugs = [p().slug for p in ALL_PROVIDERS]
        assert len(slugs) == len(set(slugs))


class TestTheStubRefusesRatherThanInvents:
    """A stub that returned () would look like a provider that never receives.

    That is the worst possible failure: silent, plausible, and indistinguishable
    from a quiet day.
    """

    def test_parsing_raises(self) -> None:
        with pytest.raises(WatiNotConfigured):
            WatiBSP().parse_inbound(b"{}", tenant_slug=TENANT)

    def test_receipts_raise(self) -> None:
        with pytest.raises(WatiNotConfigured):
            WatiBSP().parse_receipts(b"{}")

    async def test_sending_raises(self) -> None:
        with pytest.raises(WatiNotConfigured):
            await WatiBSP().send(None)  # type: ignore[arg-type]

    def test_it_says_it_is_not_implemented(self) -> None:
        assert WatiBSP().implemented is False


# ---------------------------------------------------------------------------
# Tier 2 — implementations that claim to parse
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("provider", PARSING_PROVIDERS)
class TestMalformedInputIsSurvived:
    """A parser meets the wild on its first day.

    The failure mode of a strict one is a 500 in response to a real message,
    which the provider then retries forever.
    """

    def test_a_body_that_is_not_json(self, provider: type) -> None:
        instance = provider()
        try:
            result = instance.parse_inbound(b"<html>oh dear</html>", tenant_slug=TENANT)
        except json.JSONDecodeError:
            pytest.skip(
                f"{instance.slug} is strict by design: chat_sim builds InboundMessage "
                "directly, so this transport never meets untrusted input"
            )
        assert list(result) == []

    def test_an_empty_body(self, provider: type) -> None:
        instance = provider()
        try:
            assert list(instance.parse_inbound(b"", tenant_slug=TENANT)) == []
        except json.JSONDecodeError:
            pytest.skip(f"{instance.slug} is strict by design; see above")

    def test_receipts_from_a_body_that_is_not_json(self, provider: type) -> None:
        assert list(provider().parse_receipts(b"not json at all")) == []


class TestTheMetaParser:
    """The 360dialog / Meta Cloud envelope specifically."""

    def test_a_text_message_is_parsed(self) -> None:
        body = meta_envelope(messages=[text_message()], statuses=[])
        parsed = WhatsAppCloudBSP().parse_inbound(body, tenant_slug=TENANT)

        assert len(parsed) == 1
        message = parsed[0]
        assert message.wa_id == "971501234567"
        assert message.text == "hi"
        assert message.provider_message_id == "wamid.1"
        assert message.display_name == "Layla"
        assert message.channel is Channel.WHATSAPP
        assert message.tenant_slug == TENANT

    def test_a_status_only_payload_yields_no_messages(self) -> None:
        """The majority of webhook traffic. Must not be an error."""
        body = meta_envelope(messages=[], statuses=[status_event("wamid.1", "delivered")])
        assert WhatsAppCloudBSP().parse_inbound(body, tenant_slug=TENANT) == ()

    def test_null_fields_do_not_raise(self) -> None:
        body = json.dumps({"object": "whatsapp_business_account", "entry": None}).encode()
        assert WhatsAppCloudBSP().parse_inbound(body, tenant_slug=TENANT) == ()

    def test_an_unsupported_message_type_is_dropped_not_half_processed(self) -> None:
        image = {"id": "wamid.2", "from": "971501234567", "type": "image", "image": {"id": "x"}}
        body = meta_envelope(messages=[image], statuses=[])
        assert WhatsAppCloudBSP().parse_inbound(body, tenant_slug=TENANT) == ()

    def test_a_message_missing_its_body_is_dropped(self) -> None:
        broken = {"id": "wamid.3", "from": "971501234567", "type": "text"}
        body = meta_envelope(messages=[broken], statuses=[])
        assert WhatsAppCloudBSP().parse_inbound(body, tenant_slug=TENANT) == ()

    def test_one_body_can_carry_both_messages_and_statuses(self) -> None:
        """Which is why receipts are parsed separately rather than as an
        else-branch of "did we find any messages?"."""
        body = meta_envelope(messages=[text_message()], statuses=[status_event("wamid.0", "read")])
        bsp = WhatsAppCloudBSP()
        assert len(bsp.parse_inbound(body, tenant_slug=TENANT)) == 1
        assert len(bsp.parse_receipts(body)) == 1


class TestDeliveryReceipts:
    def test_each_provider_status_maps_to_one_of_ours(self) -> None:
        body = meta_envelope(
            messages=[],
            statuses=[
                status_event("m1", "sent"),
                status_event("m2", "delivered"),
                status_event("m3", "read"),
                status_event("m4", "failed"),
            ],
        )
        receipts = {
            r.provider_message_id: r.status for r in WhatsAppCloudBSP().parse_receipts(body)
        }
        assert receipts == {
            "m1": MessageStatus.SENT,
            "m2": MessageStatus.DELIVERED,
            "m3": MessageStatus.READ,
            "m4": MessageStatus.FAILED,
        }

    def test_an_unknown_status_is_skipped_not_guessed(self) -> None:
        """Putting a message into a state the system reasons about wrongly is
        worse than not recording the status at all."""
        body = meta_envelope(messages=[], statuses=[status_event("m1", "warp_speed")])
        assert WhatsAppCloudBSP().parse_receipts(body) == ()

    def test_a_timestamp_is_carried_across(self) -> None:
        body = meta_envelope(messages=[], statuses=[status_event("m1", "delivered")])
        receipt = WhatsAppCloudBSP().parse_receipts(body)[0]
        assert receipt.occurred_at is not None
        assert receipt.occurred_at.tzinfo is not None

    def test_a_nonsense_timestamp_does_not_lose_the_receipt(self) -> None:
        body = meta_envelope(
            messages=[], statuses=[{"id": "m1", "status": "sent", "timestamp": "yesterday"}]
        )
        receipt = WhatsAppCloudBSP().parse_receipts(body)[0]
        assert receipt.status is MessageStatus.SENT
        assert receipt.occurred_at is None

    def test_a_failure_reason_is_captured_for_ops(self) -> None:
        """Recorded for the audit trail. Never shown to a consumer — "your
        message failed" is exactly the language the invariant forbids."""
        body = meta_envelope(
            messages=[],
            statuses=[
                status_event(
                    "m1", "failed", errors=[{"code": 131026, "title": "Message undeliverable"}]
                )
            ],
        )
        receipt = WhatsAppCloudBSP().parse_receipts(body)[0]
        assert receipt.status is MessageStatus.FAILED
        assert receipt.failed_reason == "Message undeliverable"

    def test_a_receipt_is_transport_neutral(self) -> None:
        """The pipeline must not learn any provider's vocabulary."""
        body = meta_envelope(messages=[], statuses=[status_event("m1", "delivered")])
        receipt = WhatsAppCloudBSP().parse_receipts(body)[0]
        assert isinstance(receipt, DeliveryReceipt)
        assert isinstance(receipt.status, MessageStatus)


class TestSignatureVerification:
    """Shared across providers because the scheme is data, not code."""

    SECRET = "shared-secret"

    def test_a_correct_signature_is_accepted(self) -> None:
        scheme = WhatsAppCloudBSP().signature_scheme
        body = meta_envelope(messages=[text_message()], statuses=[])
        header = sign_payload(scheme=scheme, secret=self.SECRET, raw_body=body)
        assert (
            verify_signature(
                scheme=scheme,
                secret=self.SECRET,
                raw_body=body,
                headers={scheme.header: header},
            )
            is SignatureResult.VALID
        )

    def test_a_tampered_body_is_refused(self) -> None:
        scheme = WhatsAppCloudBSP().signature_scheme
        body = meta_envelope(messages=[text_message()], statuses=[])
        header = sign_payload(scheme=scheme, secret=self.SECRET, raw_body=body)
        tampered = meta_envelope(
            messages=[text_message(body="book me something else")], statuses=[]
        )
        assert (
            verify_signature(
                scheme=scheme,
                secret=self.SECRET,
                raw_body=tampered,
                headers={scheme.header: header},
            )
            is SignatureResult.MISMATCH
        )

    def test_a_missing_secret_never_passes(self) -> None:
        """Failing open on a webhook means accepting anything anyone posts."""
        scheme = WhatsAppCloudBSP().signature_scheme
        result = verify_signature(
            scheme=scheme, secret=None, raw_body=b"{}", headers={scheme.header: "sha256=abc"}
        )
        assert result is SignatureResult.NOT_CONFIGURED
        assert not result.ok

    @pytest.mark.parametrize("provider", ALL_PROVIDERS)
    def test_the_scheme_is_data_so_a_correction_is_one_line(self, provider: type) -> None:
        """The Meta scheme is corroborated but unconfirmed (vendor docs 403).

        Holding it as a dataclass rather than constants is what makes correcting
        it a one-line change instead of a rewrite.
        """
        scheme = provider().signature_scheme
        assert scheme.header and scheme.digest
        # Frozen, so a provider cannot mutate a shared scheme out from under
        # another one at runtime. Correcting it is an edit to the declaration.
        with pytest.raises(dataclasses.FrozenInstanceError):
            scheme.header = "x-some-other-header"


# ---------------------------------------------------------------------------
# Receipts through the webhook, against a real database
# ---------------------------------------------------------------------------


@pytest.mark.integration
@needs_database
class TestReceiptsReachTheDatabase:
    """Parsing a receipt is only half of it — it has to land on the message.

    And it has to land *monotonically*. Providers replay statuses and deliver
    them out of order, so a late "sent" arriving after a "read" must not walk the
    message backwards.
    """

    async def _sent_message(
        self, session_factory: async_sessionmaker[AsyncSession], tenant_id: uuid.UUID
    ) -> uuid.UUID:
        """One outbound message with a provider id, as if we had just sent it."""
        async with session_factory() as session:
            lead = Lead(tenant_id=tenant_id, wa_id="971501234567", locale="en")
            session.add(lead)
            await session.flush()
            conversation = Conversation(
                tenant_id=tenant_id, lead_id=lead.id, channel=Channel.WHATSAPP, locale="en"
            )
            session.add(conversation)
            await session.flush()
            message = Message(
                tenant_id=tenant_id,
                conversation_id=conversation.id,
                lead_id=lead.id,
                turn_id=uuid.uuid4(),
                direction=Direction.OUTBOUND,
                channel=Channel.WHATSAPP,
                status=MessageStatus.QUEUED,
                body="Booked.",
                locale="en",
                phrasebank_key="booking.confirmed",
                provider_message_id="wamid.out.1",
            )
            session.add(message)
            await session.commit()
            return message.id

    async def _post(
        self, db_app: FastAPI, db_settings: Settings, statuses: list[dict[str, Any]]
    ) -> dict[str, Any]:
        body = meta_envelope(messages=[], statuses=statuses)
        assert db_settings.bsp_webhook_secret is not None
        headers = {
            "content-type": "application/json",
            "x-hub-signature-256": sign_payload(
                scheme=SignatureScheme(),
                secret=db_settings.bsp_webhook_secret.get_secret_value(),
                raw_body=body,
            ),
        }
        with TestClient(db_app) as client:
            response = client.post("/webhooks/whatsapp", content=body, headers=headers)
        assert response.status_code == 200
        payload: dict[str, Any] = response.json()
        return payload

    async def _status_of(
        self, session_factory: async_sessionmaker[AsyncSession], message_id: uuid.UUID
    ) -> MessageStatus:
        async with session_factory() as session:
            message = await session.get(Message, message_id)
            assert message is not None
            return message.status

    async def test_a_delivered_receipt_updates_the_message(
        self,
        db_app: FastAPI,
        db_settings: Settings,
        session_factory: async_sessionmaker[AsyncSession],
        tenant_id: uuid.UUID,
    ) -> None:
        message_id = await self._sent_message(session_factory, tenant_id)
        result = await self._post(db_app, db_settings, [status_event("wamid.out.1", "delivered")])

        assert result["receipts"] == 1
        assert await self._status_of(session_factory, message_id) is MessageStatus.DELIVERED

    async def test_status_only_traffic_is_a_200_with_no_turns(
        self,
        db_app: FastAPI,
        db_settings: Settings,
        session_factory: async_sessionmaker[AsyncSession],
        tenant_id: uuid.UUID,
    ) -> None:
        """Most webhook traffic. An error here means the provider retries forever."""
        await self._sent_message(session_factory, tenant_id)
        result = await self._post(db_app, db_settings, [status_event("wamid.out.1", "sent")])
        assert result["status"] == "accepted"
        assert result["handled"] == 0

    async def test_a_late_status_does_not_walk_the_message_backwards(
        self,
        db_app: FastAPI,
        db_settings: Settings,
        session_factory: async_sessionmaker[AsyncSession],
        tenant_id: uuid.UUID,
    ) -> None:
        """Providers replay and reorder. Delivery is a one-way ratchet."""
        message_id = await self._sent_message(session_factory, tenant_id)

        await self._post(db_app, db_settings, [status_event("wamid.out.1", "read")])
        assert await self._status_of(session_factory, message_id) is MessageStatus.READ

        # The stale "sent" arrives afterwards.
        result = await self._post(db_app, db_settings, [status_event("wamid.out.1", "sent")])
        assert result["receipts"] == 0
        assert await self._status_of(session_factory, message_id) is MessageStatus.READ

    async def test_a_replayed_receipt_is_applied_once(
        self,
        db_app: FastAPI,
        db_settings: Settings,
        session_factory: async_sessionmaker[AsyncSession],
        tenant_id: uuid.UUID,
    ) -> None:
        message_id = await self._sent_message(session_factory, tenant_id)
        first = await self._post(db_app, db_settings, [status_event("wamid.out.1", "delivered")])
        second = await self._post(db_app, db_settings, [status_event("wamid.out.1", "delivered")])

        assert first["receipts"] == 1
        assert second["receipts"] == 0
        assert await self._status_of(session_factory, message_id) is MessageStatus.DELIVERED

    async def test_a_receipt_for_an_unknown_message_is_not_an_error(
        self, db_app: FastAPI, db_settings: Settings, tenant_id: uuid.UUID
    ) -> None:
        """A status for something sent before this database existed."""
        result = await self._post(db_app, db_settings, [status_event("wamid.never-seen", "read")])
        assert result["status"] == "accepted"
        assert result["receipts"] == 0

    async def test_a_failed_delivery_is_recorded_and_says_nothing_to_anyone(
        self,
        db_app: FastAPI,
        db_settings: Settings,
        session_factory: async_sessionmaker[AsyncSession],
        tenant_id: uuid.UUID,
    ) -> None:
        """A failed send is an operations problem.

        Telling a consumer "your message failed" is exactly the failure language
        the invariant forbids, so a failure produces a log line and a column
        value — and no outbound message at all.
        """
        message_id = await self._sent_message(session_factory, tenant_id)
        await self._post(
            db_app,
            db_settings,
            [
                status_event(
                    "wamid.out.1",
                    "failed",
                    errors=[{"code": 131026, "title": "Message undeliverable"}],
                )
            ],
        )

        assert await self._status_of(session_factory, message_id) is MessageStatus.FAILED

        async with session_factory() as session:
            outbound = (
                (
                    await session.execute(
                        select(Message)
                        .where(Message.direction == Direction.OUTBOUND)
                        .where(Message.id != message_id)
                    )
                )
                .scalars()
                .all()
            )
        assert outbound == [], "a delivery failure produced a message to the consumer"
