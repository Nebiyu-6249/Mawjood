"""The inbound webhook endpoint."""

from __future__ import annotations

import json
import uuid

import pytest
from fastapi import FastAPI
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from starlette.testclient import TestClient

from mawjood.config import Settings
from mawjood.db.models import Lead, Message
from mawjood.services.bsp.base import SignatureScheme, sign_payload
from mawjood.services.bsp.whatsapp_cloud import WhatsAppCloudBSP

from .conftest import needs_database
from .test_transport_parity import signed_headers, whatsapp_payload

pytestmark = [pytest.mark.integration, needs_database]


class TestSignatureEnforcement:
    def test_a_correctly_signed_payload_is_accepted(
        self, db_app: FastAPI, db_settings: Settings, tenant_id: uuid.UUID
    ) -> None:
        body = whatsapp_payload("971520000001", "hi", "wamid.sig1")
        with TestClient(db_app) as client:
            response = client.post(
                "/webhooks/whatsapp", content=body, headers=signed_headers(db_settings, body)
            )
        assert response.status_code == 200
        assert response.json()["handled"] == 1

    def test_an_unsigned_payload_is_refused(
        self, db_app: FastAPI, db_settings: Settings, tenant_id: uuid.UUID
    ) -> None:
        body = whatsapp_payload("971520000002", "hi", "wamid.sig2")
        with TestClient(db_app) as client:
            response = client.post(
                "/webhooks/whatsapp", content=body, headers={"content-type": "application/json"}
            )
        assert response.status_code == 403
        assert response.json()["reason"] == "missing_header"

    def test_a_forged_signature_is_refused(
        self, db_app: FastAPI, db_settings: Settings, tenant_id: uuid.UUID
    ) -> None:
        body = whatsapp_payload("971520000003", "hi", "wamid.sig3")
        forged = sign_payload(scheme=SignatureScheme(), secret="wrong-secret", raw_body=body)
        with TestClient(db_app) as client:
            response = client.post(
                "/webhooks/whatsapp",
                content=body,
                headers={"content-type": "application/json", "x-hub-signature-256": forged},
            )
        assert response.status_code == 403
        assert response.json()["reason"] == "mismatch"

    async def test_a_refused_payload_persists_nothing(
        self,
        db_app: FastAPI,
        db_settings: Settings,
        session_factory: async_sessionmaker[AsyncSession],
        tenant_id: uuid.UUID,
    ) -> None:
        """A rejected webhook must not create a lead or a message.

        Rejecting after persisting would let an attacker seed the database with
        conversations Mawjood then acts on.
        """
        body = whatsapp_payload("971520000004", "hi", "wamid.sig4")
        with TestClient(db_app) as client:
            response = client.post(
                "/webhooks/whatsapp",
                content=body,
                headers={"content-type": "application/json"},
            )
        assert response.status_code == 403

        async with session_factory() as session:
            assert await _count(session, Lead) == 0
            assert await _count(session, Message) == 0

    def test_tampering_with_a_signed_body_is_refused(
        self, db_app: FastAPI, db_settings: Settings, tenant_id: uuid.UUID
    ) -> None:
        body = whatsapp_payload("971520000005", "hi", "wamid.sig5")
        headers = signed_headers(db_settings, body)
        tampered = whatsapp_payload("971520000005", "book me the penthouse", "wamid.sig5")
        with TestClient(db_app) as client:
            response = client.post("/webhooks/whatsapp", content=tampered, headers=headers)
        assert response.status_code == 403


class TestSubscriptionHandshake:
    def test_the_challenge_is_echoed_for_a_matching_token(
        self, db_app: FastAPI, db_settings: Settings, tenant_id: uuid.UUID
    ) -> None:
        assert db_settings.bsp_verify_token is not None
        with TestClient(db_app) as client:
            response = client.get(
                "/webhooks/whatsapp",
                params={
                    "hub.mode": "subscribe",
                    "hub.verify_token": db_settings.bsp_verify_token.get_secret_value(),
                    "hub.challenge": "challenge-12345",
                },
            )
        assert response.status_code == 200
        assert response.text == "challenge-12345"

    def test_a_wrong_token_is_refused(
        self, db_app: FastAPI, db_settings: Settings, tenant_id: uuid.UUID
    ) -> None:
        with TestClient(db_app) as client:
            response = client.get(
                "/webhooks/whatsapp",
                params={
                    "hub.mode": "subscribe",
                    "hub.verify_token": "not-the-token",
                    "hub.challenge": "challenge-12345",
                },
            )
        assert response.status_code == 403


class TestPayloadHandling:
    def test_a_status_only_payload_is_accepted_and_does_nothing(
        self, db_app: FastAPI, db_settings: Settings, tenant_id: uuid.UUID
    ) -> None:
        """Delivery receipts are most of a webhook's traffic.

        Returning an error would make the provider retry them forever.
        """
        body = json.dumps(
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
                                    "statuses": [{"id": "wamid.x", "status": "delivered"}],
                                },
                            }
                        ],
                    }
                ],
            }
        ).encode()
        with TestClient(db_app) as client:
            response = client.post(
                "/webhooks/whatsapp", content=body, headers=signed_headers(db_settings, body)
            )
        assert response.status_code == 200
        assert response.json()["handled"] == 0

    def test_a_redelivered_message_is_reported_as_duplicate(
        self, db_app: FastAPI, db_settings: Settings, tenant_id: uuid.UUID
    ) -> None:
        body = whatsapp_payload("971520000006", "hi", "wamid.repeat")
        headers = signed_headers(db_settings, body)
        with TestClient(db_app) as client:
            first = client.post("/webhooks/whatsapp", content=body, headers=headers)
            second = client.post("/webhooks/whatsapp", content=body, headers=headers)

        assert first.json()["turns"][0]["duplicate"] is False
        assert second.json()["turns"][0]["duplicate"] is True


class TestParser:
    """Parser behaviour, no database needed."""

    def test_unparseable_json_yields_no_messages(self) -> None:
        assert WhatsAppCloudBSP().parse_inbound(b"not json", tenant_slug="t") == ()

    def test_missing_keys_do_not_raise(self) -> None:
        """Webhook payloads from the wild are not reliably shaped, and a parser
        that raises turns a consumer's message into a 500."""
        for body in (b"{}", b'{"entry": null}', b'{"entry": [{"changes": null}]}'):
            assert WhatsAppCloudBSP().parse_inbound(body, tenant_slug="t") == ()

    def test_non_text_messages_are_skipped(self) -> None:
        body = json.dumps(
            {
                "entry": [
                    {
                        "changes": [
                            {"value": {"messages": [{"id": "1", "from": "9715", "type": "image"}]}}
                        ]
                    }
                ]
            }
        ).encode()
        assert WhatsAppCloudBSP().parse_inbound(body, tenant_slug="t") == ()

    def test_a_text_message_is_parsed_with_its_profile_name(self) -> None:
        body = whatsapp_payload("971521111111", "need a haircut", "wamid.p1")
        parsed = WhatsAppCloudBSP().parse_inbound(body, tenant_slug="acme")
        assert len(parsed) == 1
        assert parsed[0].wa_id == "971521111111"
        assert parsed[0].text == "need a haircut"
        assert parsed[0].provider_message_id == "wamid.p1"
        assert parsed[0].display_name == "Test Consumer"
        assert parsed[0].tenant_slug == "acme"


async def _count(session: AsyncSession, model: type) -> int:
    result = await session.execute(select(func.count()).select_from(model))
    return int(result.scalar_one())
