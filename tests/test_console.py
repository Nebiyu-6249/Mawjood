"""The ops console: read-only as a property, session auth, and every view.

The headline claim is "read-only means read-only — no mutation routes exist at
all". A claim like that is worth nothing as a convention, because conventions
are what get broken at 6pm on a Friday by someone adding one small POST. So the
first class here proves it structurally: it walks the AST of the whole console
package and fails on any non-GET domain route, any ``session.add``, ``commit``,
``flush`` or ``delete``, and any imperative SQL construct.

The login POST is the single sanctioned exception. It sets a cookie and touches
no domain data, and it is allowed by name rather than by pattern — so a second
POST appearing anywhere fails this file rather than shipping.

Everything else here is ordinary: sessions expire and cannot be forged, every
view renders, the trace shows what it promises, and the tenant scope holds.
"""

from __future__ import annotations

import ast
import time
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from fastapi import FastAPI
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from starlette.testclient import TestClient

from mawjood.api.console import auth
from mawjood.config import Settings
from mawjood.core.enums import Category
from mawjood.main import create_app

from .conftest import CONSOLE_PASSWORD, CONSOLE_USERNAME, RoutingSeeder, needs_database
from .test_handoff import ALL_DOWN, drive_to_handoff

CONSOLE_PACKAGE = Path(__file__).resolve().parent.parent / "mawjood" / "api" / "console"

# The one sanctioned POST. Named explicitly: a pattern-based exemption would let
# a second write route in under the same rule.
ALLOWED_NON_GET = {("login", "post")}

# Calls that write. ``session.add`` is the obvious one; ``execute`` is not on the
# list because reads use it, so imperative SQL is caught by _SQL_WRITES instead.
_WRITE_METHODS = {"add", "add_all", "commit", "flush", "delete", "merge", "refresh", "expunge"}
_SQL_WRITES = {"insert", "update", "delete", "text"}


def console_sources() -> list[tuple[Path, ast.Module]]:
    return [(path, ast.parse(path.read_text())) for path in sorted(CONSOLE_PACKAGE.rglob("*.py"))]


class TestReadOnlyIsAProperty:
    """Not "we do not currently mutate" — "a mutation cannot be here"."""

    def test_no_route_other_than_login_is_a_write(self) -> None:
        offenders: list[str] = []
        for path, tree in console_sources():
            for node in ast.walk(tree):
                if not isinstance(node, ast.AsyncFunctionDef | ast.FunctionDef):
                    continue
                for decorator in node.decorator_list:
                    if not isinstance(decorator, ast.Call):
                        continue
                    func = decorator.func
                    if not isinstance(func, ast.Attribute):
                        continue
                    method = func.attr.lower()
                    if method == "get":
                        continue
                    if (
                        method in {"post", "put", "patch", "delete"}
                        and (
                            node.name,
                            method,
                        )
                        not in ALLOWED_NON_GET
                    ):
                        offenders.append(f"{path.name}:{node.lineno} {method.upper()} {node.name}")
        assert not offenders, (
            "the console must have no mutation routes. Found: "
            + ", ".join(offenders)
            + ". Domain writes belong in tools/handoff.py, not here."
        )

    def test_no_module_writes_to_the_session(self) -> None:
        offenders: list[str] = []
        for path, tree in console_sources():
            for node in ast.walk(tree):
                if (
                    isinstance(node, ast.Call)
                    and isinstance(node.func, ast.Attribute)
                    and node.func.attr in _WRITE_METHODS
                    and isinstance(node.func.value, ast.Name | ast.Attribute)
                ):
                    target = getattr(node.func.value, "id", None) or getattr(
                        node.func.value, "attr", ""
                    )
                    if "session" in str(target).lower():
                        offenders.append(f"{path.name}:{node.lineno} session.{node.func.attr}()")
        assert not offenders, "the console wrote to the database: " + ", ".join(offenders)

    def test_no_imperative_sql_is_constructed(self) -> None:
        """``insert()``, ``update()``, ``delete()`` and raw ``text()`` are absent.

        Raw ``text()`` is on the list not because it is always a write, but
        because it is the escape hatch through which one would arrive.
        """
        offenders: list[str] = []
        for path, tree in console_sources():
            for node in ast.walk(tree):
                if (
                    isinstance(node, ast.Call)
                    and isinstance(node.func, ast.Name)
                    and node.func.id in _SQL_WRITES
                ):
                    offenders.append(f"{path.name}:{node.lineno} {node.func.id}()")
        assert not offenders, "imperative SQL in the console: " + ", ".join(offenders)

    def test_the_scan_can_actually_fail(self) -> None:
        """A check that cannot fail is not a check.

        Same discipline as the blocklist scan's test-of-the-test: plant a
        violation and confirm the detector sees it.
        """
        planted = ast.parse(
            "@router.post('/x')\n"
            "async def wipe():\n"
            "    session.delete(thing)\n"
            "    await session.commit()\n"
        )
        writes = [
            node
            for node in ast.walk(planted)
            if isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr in _WRITE_METHODS
        ]
        posts = [
            node
            for node in ast.walk(planted)
            if isinstance(node, ast.AsyncFunctionDef)
            and any(
                isinstance(d, ast.Call)
                and isinstance(d.func, ast.Attribute)
                and d.func.attr == "post"
                for d in node.decorator_list
            )
        ]
        assert writes, "the session-write detector would not have caught a planted write"
        assert posts, "the route detector would not have caught a planted POST"

    def test_the_openapi_surface_is_get_only_apart_from_login(self) -> None:
        """Belt and braces, from the running app rather than the source.

        The AST test proves nothing was written; this proves nothing was
        *registered* — a route added by some other mechanism would still show up
        here.
        """
        settings = Settings(
            database_url="postgresql+asyncpg://u@127.0.0.1:5432/d", console_password="x"
        )
        spec = create_app(settings).openapi()["paths"]
        offenders = [
            f"{method.upper()} {path}"
            for path, methods in spec.items()
            if path.startswith("/console")
            for method in methods
            if method.lower() != "get" and path != "/console/login"
        ]
        assert not offenders, "non-GET console routes registered: " + ", ".join(offenders)


class TestSessionAuth:
    """A signed, expiring cookie. Forging one or extending one must not work."""

    SECRET = "session-signing-secret"

    def test_a_freshly_issued_cookie_verifies(self) -> None:
        cookie = auth.issue("dana", secret=self.SECRET)
        session = auth.verify(cookie, secret=self.SECRET)
        assert session is not None
        assert session.username == "dana"
        assert not session.expired

    def test_a_cookie_signed_with_another_key_is_refused(self) -> None:
        cookie = auth.issue("dana", secret="some-other-secret")
        assert auth.verify(cookie, secret=self.SECRET) is None

    def test_the_expiry_is_covered_by_the_signature(self) -> None:
        """Signing only the username would let anyone extend a session forever."""
        cookie = auth.issue("dana", secret=self.SECRET)
        username, expiry, signature = cookie.rsplit(":", 2)
        forged = f"{username}:{int(expiry) + 86400}:{signature}"
        assert auth.verify(forged, secret=self.SECRET) is None

    def test_an_expired_cookie_is_refused(self) -> None:
        cookie = auth.issue("dana", secret=self.SECRET, ttl_seconds=-1)
        assert auth.verify(cookie, secret=self.SECRET) is None

    @pytest.mark.parametrize("bad", ["", "nonsense", "a:b", "dana:notanumber:sig", None])
    def test_malformed_cookies_are_refused(self, bad: str | None) -> None:
        assert auth.verify(bad, secret=self.SECRET) is None

    def test_a_username_swap_is_refused(self) -> None:
        cookie = auth.issue("dana", secret=self.SECRET)
        _, expiry, signature = cookie.rsplit(":", 2)
        assert auth.verify(f"root:{expiry}:{signature}", secret=self.SECRET) is None

    def test_the_signing_key_changes_when_the_password_changes(self) -> None:
        """Rotating the password must invalidate outstanding sessions."""
        one = Settings(database_url="postgresql+asyncpg://u@127.0.0.1:5432/d", console_password="a")
        two = Settings(database_url="postgresql+asyncpg://u@127.0.0.1:5432/d", console_password="b")
        assert auth.session_secret(one) != auth.session_secret(two)

    def test_the_signing_key_is_not_the_password(self) -> None:
        settings = Settings(
            database_url="postgresql+asyncpg://u@127.0.0.1:5432/d", console_password="hunter2"
        )
        secret = auth.session_secret(settings)
        assert secret is not None
        assert "hunter2" not in secret

    def test_no_password_configured_means_no_sessions(self) -> None:
        settings = Settings(database_url="postgresql+asyncpg://u@127.0.0.1:5432/d")
        assert auth.session_secret(settings) is None

    def test_credentials_are_checked_against_config(self) -> None:
        settings = Settings(
            database_url="postgresql+asyncpg://u@127.0.0.1:5432/d",
            console_username="ops",
            console_password="secret",
        )
        assert auth.check_credentials("ops", "secret", settings=settings)
        assert not auth.check_credentials("ops", "wrong", settings=settings)
        assert not auth.check_credentials("root", "secret", settings=settings)

    def test_a_session_knows_when_it_has_expired(self) -> None:
        assert auth.Session("dana", int(time.time()) - 1).expired
        assert not auth.Session("dana", int(time.time()) + 60).expired


# ---------------------------------------------------------------------------
# The views, against a live database
# ---------------------------------------------------------------------------

pytestmark_integration = [pytest.mark.integration, needs_database]

EMPTY_QUEUE_COPY = "Everyone has been looked after"

VIEWS = [
    "/console",
    "/console/conversations",
    "/console/bookings",
    "/console/bookings?grain=week",
    "/console/bookings?grain=month",
    "/console/aggregators",
    "/console/failures",
    "/console/sources",
    "/console/scheduled",
    "/console/health",
]


def signed_in(app: FastAPI) -> TestClient:
    """A client holding a valid session cookie."""
    client = TestClient(app)
    secret = auth.session_secret(app.state.settings)
    assert secret is not None
    client.cookies.set(auth.COOKIE_NAME, auth.issue(CONSOLE_USERNAME, secret=secret))
    return client


@pytest.mark.integration
@needs_database
class TestAuthenticationGate:
    async def test_no_credential_configured_refuses_to_serve(
        self, db_settings: Settings, db_engine: object, tenant_id: uuid.UUID
    ) -> None:
        """A console nobody configured must be shut, not open."""
        settings = db_settings.model_copy(update={"console_password": None})
        with TestClient(create_app(settings)) as client:
            assert client.get("/console").status_code == 503

    async def test_anonymous_is_sent_to_the_login_page(self, db_app: FastAPI) -> None:
        with TestClient(db_app) as client:
            response = client.get("/console", follow_redirects=False)
        assert response.status_code == 303
        assert response.headers["location"] == "/console/login"

    async def test_every_view_is_behind_the_session(self, db_app: FastAPI) -> None:
        """Enumerated from the app, so a view added later without the dependency
        fails here instead of shipping open."""
        spec = db_app.openapi()["paths"]
        paths = [p for p in spec if p.startswith("/console") and p not in ("/console/login",)]
        assert paths
        with TestClient(db_app) as client:
            for path in paths:
                concrete = path.replace("{conversation_id}", str(uuid.uuid4()))
                response = client.get(concrete, follow_redirects=False)
                assert response.status_code == 303, f"{concrete} answered {response.status_code}"
                assert response.headers["location"] == "/console/login"

    async def test_the_login_page_is_reachable_without_a_session(self, db_app: FastAPI) -> None:
        with TestClient(db_app) as client:
            assert client.get("/console/login").status_code == 200

    async def test_signing_in_sets_a_cookie_and_lands_on_the_dashboard(
        self, db_app: FastAPI
    ) -> None:
        with TestClient(db_app) as client:
            response = client.post(
                "/console/login",
                data={"username": CONSOLE_USERNAME, "password": CONSOLE_PASSWORD},
                follow_redirects=False,
            )
            assert response.status_code == 303
            assert response.headers["location"] == "/console"
            assert auth.COOKIE_NAME in response.cookies
            assert client.get("/console").status_code == 200

    async def test_a_wrong_password_re_renders_the_form(self, db_app: FastAPI) -> None:
        with TestClient(db_app) as client:
            response = client.post(
                "/console/login",
                data={"username": CONSOLE_USERNAME, "password": "nope"},
                follow_redirects=False,
            )
        assert response.status_code == 200
        assert "did not match" in response.text
        assert auth.COOKIE_NAME not in response.cookies

    async def test_the_cookie_is_httponly(self, db_app: FastAPI) -> None:
        """A console cookie readable from JavaScript is a transcript leak away
        from any XSS anywhere on the origin."""
        with TestClient(db_app) as client:
            response = client.post(
                "/console/login",
                data={"username": CONSOLE_USERNAME, "password": CONSOLE_PASSWORD},
                follow_redirects=False,
            )
        assert "httponly" in response.headers["set-cookie"].lower()

    async def test_signing_out_clears_the_cookie(self, db_app: FastAPI) -> None:
        with signed_in(db_app) as client:
            response = client.get("/console/logout", follow_redirects=False)
        assert response.status_code == 303
        assert response.headers["location"] == "/console/login"


@pytest.mark.integration
@needs_database
class TestTheViewsRender:
    @pytest.fixture
    async def with_data(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        db_settings: Settings,
        tenant_id: uuid.UUID,
        routed: RoutingSeeder,
    ) -> uuid.UUID:
        """A conversation that exhausted the cascade — the interesting case."""
        await routed(Category.SALON, *ALL_DOWN)
        return await drive_to_handoff(session_factory, db_settings)

    @pytest.mark.parametrize("path", VIEWS)
    async def test_each_view_renders(self, db_app: FastAPI, path: str) -> None:
        with signed_in(db_app) as client:
            response = client.get(path)
        assert response.status_code == 200, f"{path} → {response.status_code}"
        assert "<html" in response.text

    @pytest.mark.parametrize("path", VIEWS)
    async def test_each_view_renders_with_data(
        self, db_app: FastAPI, with_data: uuid.UUID, path: str
    ) -> None:
        """Empty pages are easy. Populated ones are where a template breaks."""
        with signed_in(db_app) as client:
            response = client.get(path)
        assert response.status_code == 200, f"{path} → {response.status_code}"

    async def test_an_invalid_grain_is_refused(self, db_app: FastAPI) -> None:
        with signed_in(db_app) as client:
            assert client.get("/console/bookings?grain=fortnight").status_code in (400, 422)

    async def test_the_dashboard_says_nothing_about_emptiness(self, db_app: FastAPI) -> None:
        """The invariant is consumer-facing, but the habit is worth keeping —
        and console chrome is not phrasebank copy, so the main scan misses it."""
        import re

        from tests.cascade.test_no_empty_shelves import scan

        with signed_in(db_app) as client:
            body = client.get("/console").text

        visible = re.sub(
            r"<[^>]+>", " ", re.sub(r"<(style|script)\b.*?</\1>", " ", body, flags=re.S | re.I)
        )
        assert EMPTY_QUEUE_COPY in body
        assert not scan(visible), f"failure language in console chrome: {scan(visible)}"

    async def test_an_unknown_conversation_is_a_404(self, db_app: FastAPI) -> None:
        with signed_in(db_app) as client:
            assert client.get(f"/console/conversations/{uuid.uuid4()}").status_code == 404
            assert client.get(f"/console/conversations/{uuid.uuid4()}/trace").status_code == 404


@pytest.mark.integration
@needs_database
class TestTheRoutingTrace:
    """The view that wins the room. It has to actually show the decision tree."""

    @pytest.fixture
    async def traced(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        db_settings: Settings,
        tenant_id: uuid.UUID,
        routed: RoutingSeeder,
    ) -> uuid.UUID:
        from sqlalchemy import select

        from mawjood.db.models import Conversation

        await routed(Category.SALON, *ALL_DOWN)
        await drive_to_handoff(session_factory, db_settings)
        async with session_factory() as session:
            return (await session.execute(select(Conversation))).scalar_one().id

    async def test_it_names_every_platform_that_was_tried(
        self, db_app: FastAPI, traced: uuid.UUID
    ) -> None:
        with signed_in(db_app) as client:
            body = client.get(f"/console/conversations/{traced}/trace").text
        for slug in ALL_DOWN:
            assert slug in body, f"the trace does not mention {slug}"

    async def test_it_shows_each_outcome_and_why_the_engine_advanced(
        self, db_app: FastAPI, traced: uuid.UUID
    ) -> None:
        with signed_in(db_app) as client:
            body = client.get(f"/console/conversations/{traced}/trace").text
        assert "no_availability" in body
        assert "auth_error" in body
        assert "timeout" in body
        # The advance reasons the cascade records.
        assert "merchant credential rejected" in body
        assert "read timed out" in body

    async def test_it_shows_what_the_consumer_saw(self, db_app: FastAPI, traced: uuid.UUID) -> None:
        """The half that makes it a story rather than a log."""
        with signed_in(db_app) as client:
            body = client.get(f"/console/conversations/{traced}/trace").text
        assert "consumer saw" in body
        assert "bringing in one of our team" in body

    async def test_it_counts_the_attempts_and_the_platforms(
        self, db_app: FastAPI, traced: uuid.UUID
    ) -> None:
        with signed_in(db_app) as client:
            body = client.get(f"/console/conversations/{traced}/trace").text
        assert "aggregator attempts" in body
        assert "messages the consumer saw" in body

    async def test_latency_is_shown_per_attempt(self, db_app: FastAPI, traced: uuid.UUID) -> None:
        """The timeout fake burns the full budget; that number has to be visible
        or "why was this slow" is unanswerable."""
        with signed_in(db_app) as client:
            body = client.get(f"/console/conversations/{traced}/trace").text
        assert "2500" in body


@pytest.mark.integration
@needs_database
class TestTenantScope:
    async def test_another_tenants_conversation_is_not_visible(
        self,
        db_app: FastAPI,
        session_factory: async_sessionmaker[AsyncSession],
        other_tenant_id: uuid.UUID,
        tenant_id: uuid.UUID,
    ) -> None:
        from mawjood.core.enums import Channel
        from mawjood.db.models import Conversation, Lead

        async with session_factory() as session:
            lead = Lead(tenant_id=other_tenant_id, wa_id="971500000000", locale="en")
            session.add(lead)
            await session.flush()
            conversation = Conversation(
                tenant_id=other_tenant_id,
                lead_id=lead.id,
                channel=Channel.WHATSAPP,
                locale="en",
            )
            session.add(conversation)
            await session.commit()
            foreign_id = conversation.id

        with signed_in(db_app) as client:
            assert client.get(f"/console/conversations/{foreign_id}").status_code == 404
            assert client.get(f"/console/conversations/{foreign_id}/trace").status_code == 404


@pytest.mark.integration
@needs_database
class TestTheQueries:
    """The queries carry the logic, so they are tested without an HTTP client."""

    async def test_health_reports_the_registry_and_the_locales(
        self, db_session: AsyncSession, tenant_id: uuid.UUID
    ) -> None:
        from mawjood.api.console import queries

        report = await queries.health(db_session, tenant_id)
        assert report.ok
        assert "en" in report.phrasebank_locales
        assert "zenoti" in report.registered_adapters
        assert "deliveroo" in report.registered_adapters
        # Nothing is implemented against a live contract yet, and health should
        # say so rather than imply otherwise.
        assert report.implemented_adapters == []

    async def test_health_survives_a_missing_tenant(self, db_session: AsyncSession) -> None:
        from mawjood.api.console import queries

        report = await queries.health(db_session, None)
        assert report.tenant_ok is False
        assert report.database_ok is True

    async def test_aggregator_stats_come_from_the_audit_trail(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        db_settings: Settings,
        tenant_id: uuid.UUID,
        routed: RoutingSeeder,
    ) -> None:
        from mawjood.api.console import queries

        await routed(Category.SALON, *ALL_DOWN)
        await drive_to_handoff(session_factory, db_settings)

        async with session_factory() as session:
            stats = await queries.aggregator_stats(session, tenant_id)

        by_slug = {stat.platform_slug: stat for stat in stats}
        assert set(ALL_DOWN) <= set(by_slug)
        for slug in ALL_DOWN:
            assert by_slug[slug].attempts >= 1
            assert by_slug[slug].success_rate == 0.0
        # The timeout fake burns the whole budget, so its median must reflect that.
        assert by_slug["fake_timeout"].median_latency_ms == 2500

    async def test_failed_attempts_carry_a_reason(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        db_settings: Settings,
        tenant_id: uuid.UUID,
        routed: RoutingSeeder,
    ) -> None:
        from mawjood.api.console import queries

        await routed(Category.SALON, *ALL_DOWN)
        await drive_to_handoff(session_factory, db_settings)

        async with session_factory() as session:
            rows = await queries.failed_attempts(session, tenant_id)

        assert rows
        assert all(row.outcome != "ok" for row in rows)
        assert any(row.advance_reason for row in rows)

    async def test_bookings_group_by_each_grain(
        self, db_session: AsyncSession, tenant_id: uuid.UUID
    ) -> None:
        from mawjood.api.console import queries

        for grain in ("day", "week", "month"):
            assert await queries.bookings_by_grain(db_session, tenant_id, grain=grain) == []

    async def test_source_counts_pair_leads_with_bookings(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        db_settings: Settings,
        tenant_id: uuid.UUID,
    ) -> None:
        from mawjood.api.console import queries
        from mawjood.core.conversation.pipeline import handle_inbound
        from mawjood.core.conversation.types import InboundMessage
        from mawjood.core.enums import Channel

        async with session_factory() as session:
            await handle_inbound(
                session,
                InboundMessage(
                    tenant_slug=db_settings.default_tenant_slug,
                    channel=Channel.WHATSAPP,
                    wa_id="971500000123",
                    text="SRC77",
                ),
                correlation_id="src",
                settings=db_settings,
            )

        async with session_factory() as session:
            rows = await queries.source_counts(session, tenant_id)

        assert rows
        code, _medium, leads, booked = rows[0]
        assert code == "SRC77"
        assert leads == 1
        assert booked == 0

    async def test_the_trace_is_ordered_and_complete(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        db_settings: Settings,
        tenant_id: uuid.UUID,
        routed: RoutingSeeder,
    ) -> None:
        from sqlalchemy import select

        from mawjood.api.console import queries
        from mawjood.db.models import Conversation

        await routed(Category.SALON, *ALL_DOWN)
        await drive_to_handoff(session_factory, db_settings)

        async with session_factory() as session:
            conversation_id = (await session.execute(select(Conversation))).scalar_one().id
            steps = await queries.routing_trace(session, tenant_id, conversation_id)

        assert steps
        assert [s.seq for s in steps] == sorted(s.seq for s in steps)
        assert any(s.is_attempt for s in steps)
        assert any(s.consumer_visible for s in steps)
        # Both halves of the story, on one timeline.
        assert {s.platform_slug for s in steps if s.is_attempt} >= set(ALL_DOWN)

    async def test_stalled_conversations_are_counted(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        db_settings: Settings,
        tenant_id: uuid.UUID,
    ) -> None:
        """A rising count means turns are dying mid-flight, so it has to be real."""
        from sqlalchemy import select

        from mawjood.api.console import queries
        from mawjood.core.conversation.pipeline import handle_inbound
        from mawjood.core.conversation.types import InboundMessage
        from mawjood.core.enums import Channel
        from mawjood.db.models import Conversation

        async with session_factory() as session:
            await handle_inbound(
                session,
                InboundMessage(
                    tenant_slug=db_settings.default_tenant_slug,
                    channel=Channel.WHATSAPP,
                    wa_id="971500000999",
                    text="hi",
                ),
                correlation_id="stall",
                settings=db_settings,
            )

        async with session_factory() as session:
            report = await queries.health(session, tenant_id)
            assert report.stalled_conversations == 0

            conversation = (await session.execute(select(Conversation))).scalar_one()
            conversation.last_activity_at = datetime.now(UTC) - timedelta(hours=3)
            await session.commit()

        async with session_factory() as session:
            report = await queries.health(session, tenant_id)
        assert report.stalled_conversations == 1
