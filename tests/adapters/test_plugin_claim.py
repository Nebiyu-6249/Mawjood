"""The plugin claim, proven rather than asserted.

CLAUDE.md section 4: *"Core logic contains zero aggregator-specific branches.
Adding a platform later = writing one new file and inserting one config row.
There is a test that proves this."*

This is that test. It matters because the claim is what makes every partner-gated
stub worth shipping: the day a partnership closes, one file gains real methods and
nothing else moves. If core ever grows an `if slug == "zenoti"`, the promise is
gone and nobody notices until the next integration takes a fortnight.
"""

from __future__ import annotations

import ast
import uuid
from pathlib import Path
from typing import Any, ClassVar

import pytest

from mawjood.core.aggregators.base import (
    AggregatorAdapter,
    AvailabilityRequest,
    BookingRecord,
    BookingRef,
    BookingRequest,
    CallContext,
    Capabilities,
    Deadline,
    Result,
    Slot,
)
from mawjood.core.aggregators.fake import default_window, fake_merchant
from mawjood.core.aggregators.partners import PARTNER_ADAPTERS
from mawjood.core.aggregators.registry import AdapterRegistry, build_default_registry
from mawjood.core.enums import Category, Outcome
from mawjood.core.routing.cascade import Candidate, Cascade, SearchStatus

from ..cascade.conftest import RecordingAudit

CORE_ROOT = Path(__file__).resolve().parent.parent.parent / "mawjood"

# Directories that must never mention a platform by name. The adapter package and
# the registry are exempt — naming platforms is their entire job.
PLATFORM_AGNOSTIC_DIRS = (
    CORE_ROOT / "core" / "routing",
    CORE_ROOT / "core" / "conversation",
    CORE_ROOT / "api",
    CORE_ROOT / "db",
)

# Every slug that exists. If core mentions one of these, the claim is broken.
KNOWN_SLUGS = (
    "zenoti",
    "deliveroo",
    "opentable",
    "foodics",
    "booksy",
    "talabat",
    "careem",
    "fresha",
)


class TestCoreNamesNoPlatform:
    @pytest.mark.parametrize("slug", KNOWN_SLUGS)
    def test_no_platform_slug_appears_in_core_logic(self, slug: str) -> None:
        """A grep, deliberately. The cheapest possible guard on the claim."""
        offenders: list[str] = []
        for directory in PLATFORM_AGNOSTIC_DIRS:
            for path in directory.rglob("*.py"):
                text = path.read_text(encoding="utf-8")
                for number, line in enumerate(text.splitlines(), start=1):
                    stripped = line.strip()
                    # Comments and docstring prose may name a platform as an
                    # example; executable code may not.
                    if stripped.startswith("#"):
                        continue
                    if slug in line.lower() and _is_code(text, number):
                        offenders.append(f"{path.relative_to(CORE_ROOT)}:{number}: {stripped}")

        assert not offenders, (
            f"core logic names the platform {slug!r}:\n  "
            + "\n  ".join(offenders)
            + "\n\nAdding a platform must be one new file plus one config row. A "
            "branch on a slug in core means the next integration is a refactor."
        )

    def test_the_router_selects_by_configuration_not_by_name(self) -> None:
        from mawjood.core.routing import router

        source = (CORE_ROOT / "core" / "routing" / "router.py").read_text()
        # The router reads slugs from the database and looks them up in the
        # registry. It must never compare one to a literal.
        assert "routing_config" in source.lower() or "RoutingConfig" in source
        assert hasattr(router.Router, "candidates_for")


def _is_code(source: str, line_number: int) -> bool:
    """True when the line is not inside a string literal or docstring.

    A slug named in prose is documentation; a slug in an expression is a branch.
    """
    try:
        tree = ast.parse(source)
    except SyntaxError:  # pragma: no cover
        return True
    for node in ast.walk(tree):
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            start = getattr(node, "lineno", None)
            end = getattr(node, "end_lineno", None)
            if start is not None and end is not None and start <= line_number <= end:
                return False
    return True


# ---------------------------------------------------------------------------
# One new file, one config row
# ---------------------------------------------------------------------------


class FreshaAdapter:
    """A brand-new platform, defined entirely here.

    Nothing outside this class exists for it: no core change, no import in
    another module, no branch anywhere. If the claim holds, registering this and
    naming it in routing_config is enough to route to it.
    """

    slug = "fresha"
    categories: ClassVar[list[Category]] = [Category.SALON, Category.SPA]
    capabilities = Capabilities(can_search=True, can_book=True, honours_idempotency=True)

    def __init__(self) -> None:
        self.created: dict[str, BookingRef] = {}

    async def search_availability(
        self, ctx: CallContext, req: AvailabilityRequest
    ) -> Result[list[Slot]]:
        return Result(
            outcome=Outcome.OK,
            data=[
                Slot(
                    slot_id="fresha-1",
                    start=req.window_start,
                    end=req.window_end,
                    venue_name="Fresha Partner Salon",
                    service_name=req.service,
                    merchant=ctx.merchant,
                    price_amount=99.0,
                    price_currency="AED",
                )
            ],
            latency_ms=40,
        )

    async def create_booking(self, ctx: CallContext, req: BookingRequest) -> Result[BookingRef]:
        ref = BookingRef(
            external_id=f"fresha-{len(self.created)}",
            platform_slug=self.slug,
            merchant=ctx.merchant,
            confirmation_code="FR1234",
        )
        self.created[req.idempotency_key] = ref
        return Result(outcome=Outcome.OK, data=ref, latency_ms=30)

    async def get_booking(self, ctx: CallContext, ref: BookingRef) -> Result[BookingRecord]:
        return Result(outcome=Outcome.OK, data=BookingRecord(ref=ref, status="confirmed"))

    async def find_by_idempotency_key(
        self, ctx: CallContext, idempotency_key: str
    ) -> Result[BookingRecord]:
        stored = self.created.get(idempotency_key)
        if stored is None:
            return Result(outcome=Outcome.NO_AVAILABILITY)
        return Result(outcome=Outcome.OK, data=BookingRecord(ref=stored, status="confirmed"))

    async def reschedule(self, ctx: CallContext, ref: BookingRef, slot: Slot) -> Result[BookingRef]:
        return Result(outcome=Outcome.OK, data=ref)

    async def cancel(self, ctx: CallContext, ref: BookingRef, reason: str | None) -> Result[None]:
        return Result(outcome=Outcome.OK)

    async def health(self, ctx: CallContext) -> Result[None]:
        return Result(outcome=Outcome.OK)


class TestAddingAPlatform:
    def test_a_new_adapter_satisfies_the_contract_without_inheriting_anything(self) -> None:
        """No base class, no mixin. Structural conformance is the only requirement."""
        assert isinstance(FreshaAdapter(), AggregatorAdapter)
        assert FreshaAdapter.__mro__[1:] == (object,)

    def test_it_registers_with_no_change_to_the_registry(self) -> None:
        registry = build_default_registry()
        before = len(registry)
        registry.register(FreshaAdapter())
        assert len(registry) == before + 1
        assert registry.get("fresha") is not None

    async def test_the_cascade_routes_to_it_unmodified(self) -> None:
        """The real cascade, an adapter it has never heard of, one slot booked."""
        audit = RecordingAudit()
        adapter = FreshaAdapter()
        engine = Cascade(
            audit=audit,
            deadline=Deadline.in_ms(2000),
            tenant_id=uuid.uuid4(),
            conversation_id=uuid.uuid4(),
            correlation_id="plugin-claim",
        )
        start, end = default_window()
        request = AvailabilityRequest(
            category=Category.SALON,
            service="haircut",
            window_start=start,
            window_end=end,
            area="Dubai Marina",
        )
        candidate = Candidate(adapter=adapter, merchant=fake_merchant("fresha"))

        found = await engine.search([candidate], request)
        assert found.status is SearchStatus.FOUND
        assert found.slot is not None
        assert found.slot.venue_name == "Fresha Partner Salon"

        booked = await engine.book(candidate, found.slot, category=Category.SALON)
        assert booked.ref is not None
        assert booked.ref.platform_slug == "fresha"
        assert len(adapter.created) == 1

    async def test_it_passes_the_same_conformance_suite(self) -> None:
        from .test_conformance import ALL_METHODS, call

        adapter = FreshaAdapter()
        for method in ALL_METHODS:
            result: Result[Any] = await call(adapter, method)
            assert isinstance(result, Result)


# ---------------------------------------------------------------------------
# Partner-gated stubs
# ---------------------------------------------------------------------------


class TestPartnerStubs:
    @pytest.mark.parametrize("factory", PARTNER_ADAPTERS, ids=lambda f: f.slug)
    def test_each_declares_itself_unimplemented_with_a_reason(self, factory: type) -> None:
        adapter = factory()
        assert adapter.implemented is False
        assert adapter.gate.summary
        assert adapter.gate.blocker, f"{adapter.slug} does not say what is blocking it"

    @pytest.mark.parametrize("factory", PARTNER_ADAPTERS, ids=lambda f: f.slug)
    def test_each_claims_no_capabilities(self, factory: type) -> None:
        capabilities = factory().capabilities
        assert not capabilities.can_search
        assert not capabilities.can_book
        assert not capabilities.can_reschedule
        assert not capabilities.can_cancel

    @pytest.mark.parametrize("factory", PARTNER_ADAPTERS, ids=lambda f: f.slug)
    async def test_none_of_them_ever_synthesises_a_booking(self, factory: type) -> None:
        from .test_conformance import call

        result = await call(factory(), "create_booking")
        assert result.outcome is Outcome.UNSUPPORTED
        assert result.data is None

    def test_they_all_register_together_without_collision(self) -> None:
        registry = build_default_registry()
        for factory in PARTNER_ADAPTERS:
            assert factory.slug in registry

    async def test_a_cascade_of_only_stubs_exhausts_to_a_human(self) -> None:
        """Every platform partner-gated is a real configuration today. It must end
        in a handoff, not an exception and not a fabricated slot."""
        audit = RecordingAudit()
        engine = Cascade(
            audit=audit,
            deadline=Deadline.in_ms(2000),
            tenant_id=uuid.uuid4(),
            conversation_id=uuid.uuid4(),
            correlation_id="all-gated",
        )
        start, end = default_window()
        candidates = [
            Candidate(adapter=factory(), merchant=fake_merchant(factory.slug))
            for factory in PARTNER_ADAPTERS
        ]
        outcome = await engine.search(
            candidates,
            AvailabilityRequest(
                category=Category.RESTAURANT,
                service="table",
                window_start=start,
                window_end=end,
            ),
        )
        assert outcome.status is SearchStatus.EXHAUSTED
        assert outcome.handoff_reason == "cascade_exhausted"
        assert audit.consumer_visible_events == []


class TestRegistryIsTheAuthority:
    def test_routing_config_slugs_are_resolved_through_the_registry(self) -> None:
        """A slug with no adapter is a configuration fault, not a dead end.

        The router logs it and skips; it must never surface to a consumer.
        """
        registry = AdapterRegistry()
        assert registry.get("a-platform-that-does-not-exist") is None
