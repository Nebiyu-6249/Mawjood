"""chat_sim and the WhatsApp webhook must never become two code paths.

The terminal harness exists so Mawjood can be demonstrated and iterated on before
a number is provisioned. That is only worth having if what you see in the
terminal is what a real consumer would get. The moment the two drift, every demo
becomes a lie and every fix has to be made twice.

These tests hold the line two ways:

* **Structurally** — both transports are asserted to call the same
  ``handle_inbound``, so a future fork is caught at import.
* **Behaviourally** — the same message through both produces identical replies,
  identical phrasebank keys, and identical audit event sequences.
"""

from __future__ import annotations

import json
import uuid

import pytest
from fastapi import FastAPI
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from starlette.testclient import TestClient

from mawjood.config import Settings
from mawjood.core.conversation.pipeline import handle_inbound
from mawjood.core.conversation.types import InboundMessage
from mawjood.core.enums import Channel
from mawjood.db.models import AuditLog, Message
from mawjood.services.bsp.base import SignatureScheme, sign_payload

from .conftest import needs_database

pytestmark = [pytest.mark.integration, needs_database]


def whatsapp_payload(wa_id: str, text: str, message_id: str) -> bytes:
    """A Meta/360dialog-shaped inbound text message."""
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
                                    {"wa_id": wa_id, "profile": {"name": "Test Consumer"}}
                                ],
                                "messages": [
                                    {
                                        "id": message_id,
                                        "from": wa_id,
                                        "timestamp": "1730000000",
                                        "type": "text",
                                        "text": {"body": text},
                                    }
                                ],
                            },
                        }
                    ],
                }
            ],
        }
    ).encode()


def signed_headers(settings: Settings, body: bytes) -> dict[str, str]:
    assert settings.bsp_webhook_secret is not None
    return {
        "content-type": "application/json",
        "x-hub-signature-256": sign_payload(
            scheme=SignatureScheme(),
            secret=settings.bsp_webhook_secret.get_secret_value(),
            raw_body=body,
        ),
    }


class TestStructuralParity:
    def test_the_webhook_uses_the_shared_pipeline(self) -> None:
        from mawjood.api.webhooks import whatsapp as webhook_module

        # Deliberate namespace introspection: the point is which object the
        # webhook module actually bound, not what it re-exports.
        assert vars(webhook_module)["handle_inbound"] is handle_inbound

    def test_chat_sim_uses_the_shared_pipeline(self) -> None:
        import importlib.util
        from pathlib import Path

        path = Path(__file__).resolve().parent.parent / "tools" / "chat_sim.py"
        spec = importlib.util.spec_from_file_location("chat_sim_under_test", path)
        assert spec and spec.loader
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)

        assert vars(module)["handle_inbound"] is handle_inbound, (
            "chat_sim must drive the same pipeline as the webhook, not its own"
        )

    def test_the_console_transport_implements_the_bsp_protocol(self) -> None:
        from mawjood.services.bsp.base import BSPAdapter
        from mawjood.services.bsp.console import ConsoleBSP

        assert isinstance(ConsoleBSP(), BSPAdapter)

    def test_both_transports_pass_settings_into_the_pipeline(self) -> None:
        """Calling the same function is not the same as calling it the same way.

        ``handle_inbound(settings=...)`` is optional, and everything it
        configures fails *quietly* when it is omitted: the adapter registry comes
        up empty, the LLM provider reverts to the understudy, the turn budget and
        timezone revert to defaults. Replies still come back, so a
        behavioural-parity check comparing greetings sees nothing wrong.

        This is not hypothetical. chat_sim passed settings and the webhook did
        not, so a booking that worked in the terminal would have found no
        aggregators on WhatsApp — the exact divergence this file exists to stop,
        one layer below where it was looking.
        """
        import ast
        from pathlib import Path

        root = Path(__file__).resolve().parent.parent
        sources = {
            "webhook": root / "mawjood" / "api" / "webhooks" / "whatsapp.py",
            "chat_sim": root / "tools" / "chat_sim.py",
        }

        for name, path in sources.items():
            tree = ast.parse(path.read_text())
            calls = [
                node
                for node in ast.walk(tree)
                if isinstance(node, ast.Call)
                and isinstance(node.func, ast.Name)
                and node.func.id == "handle_inbound"
            ]
            assert calls, f"{name} does not call handle_inbound at all"
            for call in calls:
                keywords = {kw.arg for kw in call.keywords}
                assert "settings" in keywords, (
                    f"{name} calls handle_inbound without settings= at line "
                    f"{call.lineno}. Every configured value would be silently "
                    "ignored on that transport."
                )


class TestBehaviouralParity:
    async def test_identical_input_produces_identical_replies(
        self,
        db_app: FastAPI,
        db_settings: Settings,
        session_factory: async_sessionmaker[AsyncSession],
        tenant_id: uuid.UUID,
    ) -> None:
        text = "need a haircut in Marina tomorrow evening"

        # --- through the webhook -------------------------------------------
        wa_consumer = "971500000001"
        body = whatsapp_payload(wa_consumer, text, "wamid.parity1")
        with TestClient(db_app) as client:
            response = client.post(
                "/webhooks/whatsapp", content=body, headers=signed_headers(db_settings, body)
            )
        assert response.status_code == 200

        # --- through the console path --------------------------------------
        console_consumer = "971500000002"
        async with session_factory() as session:
            console_turn = await handle_inbound(
                session,
                InboundMessage(
                    tenant_slug=db_settings.default_tenant_slug,
                    channel=Channel.CONSOLE,
                    wa_id=console_consumer,
                    text=text,
                ),
                correlation_id="parity-console",
            )

        async with session_factory() as session:
            webhook_replies = await _outbound_for(session, wa_consumer)
            console_replies = [message.text for message in console_turn.outbound]
            webhook_keys = await _outbound_keys(session, wa_consumer)
            console_keys = [message.phrasebank_key for message in console_turn.outbound]

        assert webhook_replies == console_replies
        assert webhook_keys == console_keys

    async def test_identical_input_produces_identical_audit_events(
        self,
        db_app: FastAPI,
        db_settings: Settings,
        session_factory: async_sessionmaker[AsyncSession],
        tenant_id: uuid.UUID,
    ) -> None:
        text = "hello there"

        wa_consumer = "971500000003"
        body = whatsapp_payload(wa_consumer, text, "wamid.parity2")
        with TestClient(db_app) as client:
            client.post(
                "/webhooks/whatsapp", content=body, headers=signed_headers(db_settings, body)
            )

        console_consumer = "971500000004"
        async with session_factory() as session:
            await handle_inbound(
                session,
                InboundMessage(
                    tenant_slug=db_settings.default_tenant_slug,
                    channel=Channel.CONSOLE,
                    wa_id=console_consumer,
                    text=text,
                ),
                correlation_id="parity-console-2",
            )

        async with session_factory() as session:
            webhook_events = await _audit_events(session, wa_consumer)
            console_events = await _audit_events(session, console_consumer)

        assert webhook_events == console_events, (
            "the two transports produced different audit trails for the same "
            "message, which means they are no longer one pipeline"
        )
        assert "consent.notice_shown" in webhook_events


async def _outbound_for(session: AsyncSession, wa_id: str) -> list[str]:
    from sqlalchemy import select

    from mawjood.db.models import Lead

    result = await session.execute(
        select(Message.body)
        .join(Lead, Lead.id == Message.lead_id)
        .where(Lead.wa_id == wa_id)
        .where(Message.direction == "outbound")
        .order_by(Message.seq)
    )
    return list(result.scalars().all())


async def _outbound_keys(session: AsyncSession, wa_id: str) -> list[str | None]:
    from sqlalchemy import select

    from mawjood.db.models import Lead

    result = await session.execute(
        select(Message.phrasebank_key)
        .join(Lead, Lead.id == Message.lead_id)
        .where(Lead.wa_id == wa_id)
        .where(Message.direction == "outbound")
        .order_by(Message.seq)
    )
    return list(result.scalars().all())


async def _audit_events(session: AsyncSession, wa_id: str) -> list[str]:
    from sqlalchemy import select

    from mawjood.db.models import Lead

    result = await session.execute(
        select(AuditLog.event_type)
        .join(Lead, Lead.id == AuditLog.lead_id)
        .where(Lead.wa_id == wa_id)
        .order_by(AuditLog.seq)
    )
    return list(result.scalars().all())
