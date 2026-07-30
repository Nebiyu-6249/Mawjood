"""The ops console: authentication, the queue, and the one write path.

Two things are being tested here, and only one of them is a web page.

The **write path** matters because decision 7 stretched "read-only ops console"
knowingly: an operator can send words to a consumer from this UI. That is the
only place in the system where consumer-facing text does not come from the
phrasebank, so it gets checked like a door rather than like a form.

The **audit view** matters because it is the reason the console exists at all.
When someone asks months later why a consumer ended up with a human, the answer
has to be on one screen — every platform tried, what came back, why the cascade
moved on. A console that shows the transcript but not the reasoning is a chat
log, and we already have those.

Authentication is asserted before anything else. A console with no credential
configured must refuse to serve rather than default to open: it exposes every
consumer's transcript, so a misconfiguration has to fail closed.

The tests are async and drive a sync ``TestClient`` inside, matching
test_transport_parity.py. The client runs the app on its own event loop in a
portal thread, so it holds its own database connections — anything the test sets
up beforehand must be committed, not merely flushed.
"""

from __future__ import annotations

import re
import uuid

import pytest
from fastapi import FastAPI
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from starlette.testclient import TestClient

from mawjood.config import Settings
from mawjood.core.enums import Category, HandoffStatus
from mawjood.db.models import Conversation, HandoffQueue, Lead, Message
from mawjood.main import create_app

from .conftest import CONSOLE_PASSWORD, CONSOLE_USERNAME, RoutingSeeder, needs_database
from .test_handoff import ALL_DOWN, drive_to_handoff

pytestmark = [pytest.mark.integration, needs_database]

EMPTY_QUEUE_COPY = "Everyone has been looked after"

_TAG = re.compile(r"<[^>]+>")
_NON_TEXT_ELEMENT = re.compile(r"<(style|script)\b.*?</\1>", re.IGNORECASE | re.DOTALL)


def visible_text(html: str) -> str:
    """The words a person actually reads, without the markup around them.

    Scanning raw HTML for language is a false-positive machine. The first run of
    this test flagged ``class="empty"``; the second flagged the ``.empty`` rule
    in the stylesheet. Neither is copy — nobody reads a class name or a CSS
    selector.

    So: stylesheets and scripts go entirely, then tags are stripped and their
    attributes with them. A blunt regex rather than a parser, because there is no
    HTML parser in the dependency list and one line of chrome does not justify
    adding one.
    """
    return _TAG.sub(" ", _NON_TEXT_ELEMENT.sub(" ", html))


def signed_in(app: FastAPI) -> TestClient:
    client = TestClient(app)
    client.auth = (CONSOLE_USERNAME, CONSOLE_PASSWORD)
    return client


async def conversation_id(session_factory: async_sessionmaker[AsyncSession]) -> uuid.UUID:
    async with session_factory() as session:
        return (await session.execute(select(Conversation))).scalar_one().id


class TestAuthentication:
    async def test_no_credential_configured_refuses_to_serve(
        self, db_settings: Settings, db_engine: object, tenant_id: uuid.UUID
    ) -> None:
        """A console nobody configured must be shut, not open.

        503 rather than 200: an operator who sees "unavailable" goes and sets the
        password. An operator who sees the dashboard never learns it was public.
        """
        settings = db_settings.model_copy(update={"console_password": None})
        with TestClient(create_app(settings)) as client:
            assert client.get("/console").status_code == 503

    async def test_anonymous_is_challenged(self, db_app: FastAPI) -> None:
        with TestClient(db_app) as client:
            response = client.get("/console")
        assert response.status_code == 401
        assert response.headers["www-authenticate"] == "Basic"

    async def test_a_wrong_password_is_refused(self, db_app: FastAPI) -> None:
        with TestClient(db_app) as client:
            client.auth = (CONSOLE_USERNAME, "not-the-password")
            assert client.get("/console").status_code == 401

    async def test_a_wrong_username_is_refused(self, db_app: FastAPI) -> None:
        with TestClient(db_app) as client:
            client.auth = ("someone-else", CONSOLE_PASSWORD)
            assert client.get("/console").status_code == 401

    async def test_the_right_credential_gets_in(self, db_app: FastAPI) -> None:
        with signed_in(db_app) as client:
            assert client.get("/console").status_code == 200

    async def test_every_console_route_is_behind_the_credential(self, db_app: FastAPI) -> None:
        """No unauthenticated corner.

        Enumerated from the app rather than listed by hand, so a route added
        later without the dependency fails here instead of shipping quietly.
        """
        spec = db_app.openapi()["paths"]
        paths = [p for p in spec if p.startswith("/console")]
        assert paths, "no console routes registered"

        with TestClient(db_app) as client:
            for path in paths:
                concrete = path.replace("{conversation_id}", str(uuid.uuid4())).replace(
                    "{handoff_id}", str(uuid.uuid4())
                )
                for method in spec[path]:
                    response = client.request(method, concrete)
                    assert response.status_code == 401, (
                        f"{method.upper()} {concrete} answered {response.status_code} "
                        "without a credential"
                    )


class TestDashboard:
    async def test_an_empty_queue_says_nothing_about_emptiness(self, db_app: FastAPI) -> None:
        """The invariant is consumer-facing, but the habit is worth keeping.

        More practically: the blocklist scan reads phrasebank entries, and
        console chrome is not a phrasebank entry. This is one of the few places a
        stray "no results" could grow without the scan noticing, so it is
        asserted here instead.
        """
        from tests.cascade.test_no_empty_shelves import scan

        with signed_in(db_app) as client:
            body = client.get("/console").text

        assert EMPTY_QUEUE_COPY in body
        hits = scan(visible_text(body))
        assert not hits, f"failure language in console chrome: {hits}"

    async def test_the_counts_reflect_the_database(
        self,
        db_app: FastAPI,
        session_factory: async_sessionmaker[AsyncSession],
        db_settings: Settings,
        tenant_id: uuid.UUID,
        routed: RoutingSeeder,
    ) -> None:
        await routed(Category.SALON, *ALL_DOWN)
        await drive_to_handoff(session_factory, db_settings)

        with signed_in(db_app) as client:
            body = client.get("/console").text

        assert EMPTY_QUEUE_COPY not in body


class TestTheHandoffJourney:
    """Claim → reply → release, through the HTTP surface an operator actually uses.

    The unit-level round trip is in test_handoff.py. This one exists because the
    console is where it happens in practice, and a form posting to the wrong
    place fails nowhere else.
    """

    @pytest.fixture
    async def waiting(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        db_settings: Settings,
        tenant_id: uuid.UUID,
        routed: RoutingSeeder,
    ) -> uuid.UUID:
        await routed(Category.SALON, *ALL_DOWN)
        return await drive_to_handoff(session_factory, db_settings)

    async def test_the_dashboard_lists_it(
        self,
        db_app: FastAPI,
        waiting: uuid.UUID,
        session_factory: async_sessionmaker[AsyncSession],
    ) -> None:
        cid = await conversation_id(session_factory)
        with signed_in(db_app) as client:
            body = client.get("/console").text

        # The row carries a link into the conversation, and names the consumer
        # so an operator can see who has been waiting without opening it.
        assert f'/console/conversations/{cid}"' in body
        assert "971509990001" in body
        assert EMPTY_QUEUE_COPY not in body

    async def test_the_conversation_page_shows_transcript_and_reasoning(
        self,
        db_app: FastAPI,
        waiting: uuid.UUID,
        session_factory: async_sessionmaker[AsyncSession],
    ) -> None:
        """One screen: what was said, and why it went the way it did."""
        cid = await conversation_id(session_factory)
        with signed_in(db_app) as client:
            body = client.get(f"/console/conversations/{cid}").text

        # The consumer's own words.
        assert "haircut" in body.lower()
        # Every platform the cascade tried, by name.
        for slug in ALL_DOWN:
            assert slug in body, f"the audit view does not mention {slug}"
        # And why it moved on.
        assert "why it advanced" in body.lower()

    async def test_claim_then_reply_reaches_the_consumer(
        self,
        db_app: FastAPI,
        waiting: uuid.UUID,
        session_factory: async_sessionmaker[AsyncSession],
    ) -> None:
        with signed_in(db_app) as client:
            claimed = client.post(f"/console/handoffs/{waiting}/claim", follow_redirects=False)
            assert claimed.status_code == 303
            assert claimed.headers["location"].startswith("/console/conversations/")

            sent = client.post(
                f"/console/handoffs/{waiting}/reply",
                data={"text": "Dana here — 5.30 at Marina Beauty Lounge, shall I take it?"},
                follow_redirects=False,
            )
            assert sent.status_code == 303

        async with session_factory() as session:
            bodies = [
                row.body
                for row in (
                    await session.execute(
                        select(Message).where(Message.phrasebank_key == "operator.reply")
                    )
                )
                .scalars()
                .all()
            ]
        assert len(bodies) == 1
        assert bodies[0].startswith("Dana here")

    async def test_replying_before_claiming_is_a_conflict(
        self, db_app: FastAPI, waiting: uuid.UUID
    ) -> None:
        with signed_in(db_app) as client:
            response = client.post(
                f"/console/handoffs/{waiting}/reply",
                data={"text": "hello"},
                follow_redirects=False,
            )
        assert response.status_code == 409

    async def test_an_empty_reply_is_rejected(self, db_app: FastAPI, waiting: uuid.UUID) -> None:
        with signed_in(db_app) as client:
            client.post(f"/console/handoffs/{waiting}/claim", follow_redirects=False)
            response = client.post(
                f"/console/handoffs/{waiting}/reply",
                data={"text": "   "},
                follow_redirects=False,
            )
        assert response.status_code == 400

    async def test_release_from_the_console_hands_it_back(
        self,
        db_app: FastAPI,
        waiting: uuid.UUID,
        session_factory: async_sessionmaker[AsyncSession],
    ) -> None:
        with signed_in(db_app) as client:
            client.post(f"/console/handoffs/{waiting}/claim", follow_redirects=False)
            released = client.post(
                f"/console/handoffs/{waiting}/release",
                data={"notes": "sorted by phone"},
                follow_redirects=False,
            )
            assert released.status_code == 303
            # And the queue is clear.
            assert EMPTY_QUEUE_COPY in client.get("/console").text

        async with session_factory() as session:
            handoff = (await session.execute(select(HandoffQueue))).scalar_one()
            conversation = (await session.execute(select(Conversation))).scalar_one()

        assert handoff.status is HandoffStatus.RESOLVED
        assert conversation.bot_muted is False

    async def test_an_unknown_handoff_is_a_404(self, db_app: FastAPI) -> None:
        with signed_in(db_app) as client:
            response = client.post(
                f"/console/handoffs/{uuid.uuid4()}/claim", follow_redirects=False
            )
        assert response.status_code == 404

    async def test_an_unknown_conversation_is_a_404(self, db_app: FastAPI) -> None:
        with signed_in(db_app) as client:
            response = client.get(f"/console/conversations/{uuid.uuid4()}")
        assert response.status_code == 404

    async def test_the_transcript_marks_who_spoke(
        self,
        db_app: FastAPI,
        waiting: uuid.UUID,
        session_factory: async_sessionmaker[AsyncSession],
    ) -> None:
        """An audit needs to tell a colleague's words from Mawjood's at a glance."""
        cid = await conversation_id(session_factory)
        with signed_in(db_app) as client:
            client.post(f"/console/handoffs/{waiting}/claim", follow_redirects=False)
            client.post(
                f"/console/handoffs/{waiting}/reply",
                data={"text": "Dana here."},
                follow_redirects=False,
            )
            body = client.get(f"/console/conversations/{cid}").text

        assert "<strong>operator</strong>" in body
        assert "mawjood" in body
        assert "consumer" in body


class TestTenantScope:
    async def test_another_tenants_conversation_is_not_visible(
        self,
        db_app: FastAPI,
        session_factory: async_sessionmaker[AsyncSession],
        other_tenant_id: uuid.UUID,
        tenant_id: uuid.UUID,
    ) -> None:
        """The console reads through the tenant scope like everything else.

        v1 runs one operator, so this cannot fail today. It is here because the
        day a second tenant exists is not the day anyone wants to discover the
        console had been querying unscoped.
        """
        async with session_factory() as session:
            lead = Lead(tenant_id=other_tenant_id, wa_id="971500000000", locale="en")
            session.add(lead)
            await session.flush()
            conversation = Conversation(
                tenant_id=other_tenant_id,
                lead_id=lead.id,
                channel="whatsapp",
                locale="en",
            )
            session.add(conversation)
            await session.commit()
            foreign_id = conversation.id

        with signed_in(db_app) as client:
            assert client.get(f"/console/conversations/{foreign_id}").status_code == 404
