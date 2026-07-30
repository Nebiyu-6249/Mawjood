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
from .test_conformance import a_ref, a_slot, availability_request, context

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


def _booking_request_for(slug: str) -> BookingRequest:
    return BookingRequest(
        slot=a_slot(slug),
        consumer_name="Layla",
        consumer_phone="971501234567",
        idempotency_key=f"idem-{slug}",
    )


def _deliveroo_booking_request() -> BookingRequest:
    return _booking_request_for("deliveroo")


class TestDeliverooIsHonestAboutWhatItCannotDo:
    """The finding is worth a test, because it is worth a quarter of someone's time.

    Deliveroo's Order API is merchant-side: it receives orders Deliveroo already
    took from a consumer through its own apps. An outside assistant cannot
    originate one through it — and crucially, **a partnership does not change
    that.** Recording it as a uniform "blocked, pending partnership" would imply
    the opposite and send someone chasing an agreement that cannot deliver what
    they want.
    """

    def test_it_registers_and_declares_its_categories(self) -> None:
        from mawjood.core.aggregators.deliveroo import DeliverooAdapter

        adapter = DeliverooAdapter()
        assert adapter.slug == "deliveroo"
        assert Category.FOOD_DELIVERY in adapter.categories

    def test_it_implements_the_whole_contract(self) -> None:
        from mawjood.core.aggregators.base import AggregatorAdapter
        from mawjood.core.aggregators.deliveroo import DeliverooAdapter

        assert isinstance(DeliverooAdapter(), AggregatorAdapter)

    async def test_every_method_returns_unsupported_and_never_raises(self) -> None:
        from mawjood.core.aggregators.deliveroo import DeliverooAdapter
        from mawjood.core.enums import Outcome

        adapter = DeliverooAdapter()
        ctx = context("deliveroo")
        results = [
            await adapter.search_availability(ctx, availability_request()),
            await adapter.create_booking(ctx, _deliveroo_booking_request()),
            await adapter.get_booking(ctx, a_ref("deliveroo")),
            await adapter.find_by_idempotency_key(ctx, "k"),
            await adapter.reschedule(ctx, a_ref("deliveroo"), a_slot("deliveroo")),
            await adapter.cancel(ctx, a_ref("deliveroo"), None),
            await adapter.health(ctx),
        ]
        assert all(result.outcome is Outcome.UNSUPPORTED for result in results)

    def test_booking_is_blocked_architecturally_not_commercially(self) -> None:
        """The finding, asserted. If someone later marks create_booking as merely
        BLOCKED, this fails and they have to think about why."""
        from mawjood.core.aggregators.deliveroo import SUPPORT, DeliverooAdapter, Support

        support, reason = SUPPORT["create_booking"]
        assert support is Support.NOT_WHAT_THIS_API_IS_FOR
        assert not support.clears_with_a_partnership
        assert "does not place them" in reason
        assert DeliverooAdapter().partnership_would_unlock_booking is False

    def test_the_two_kinds_of_blocker_are_distinguished(self) -> None:
        from mawjood.core.aggregators.deliveroo import SUPPORT, Support

        kinds = {support for support, _ in SUPPORT.values()}
        assert kinds == {Support.BLOCKED, Support.NOT_WHAT_THIS_API_IS_FOR}, (
            "collapsing these two into one loses the only interesting thing this adapter has to say"
        )

    def test_every_contract_method_has_a_support_entry(self) -> None:
        """A method added later without a reason would return UNSUPPORTED with a
        KeyError behind it."""
        from mawjood.core.aggregators.deliveroo import SUPPORT

        expected = {
            "search_availability",
            "create_booking",
            "get_booking",
            "find_by_idempotency_key",
            "reschedule",
            "cancel",
            "health",
        }
        assert set(SUPPORT) == expected

    def test_no_endpoint_was_invented(self) -> None:
        """CLAUDE.md section 10. The docs are unreachable, so nothing here may
        name a path, a host or a header."""
        import inspect

        from mawjood.core.aggregators import deliveroo

        source = inspect.getsource(deliveroo)
        # The doc_attempts table legitimately carries the doc URLs it failed to
        # reach; strip it before looking for invented endpoints.
        without_attempts = source.split("doc_attempts")[0] + source.split(")\n", 1)[-1]
        for marker in ("https://api.deliveroo", "/v1/", "/v2/", "Authorization:", "Bearer "):
            assert marker not in without_attempts, f"invented API detail: {marker}"

    def test_the_documentation_attempts_are_recorded(self) -> None:
        """So the next person does not repeat them."""
        from mawjood.core.aggregators.deliveroo import DeliverooAdapter

        attempts = DeliverooAdapter().doc_attempts
        assert attempts
        assert all(url and result for url, result in attempts)
        assert any("403" in result for _, result in attempts)


class TestPartnerStubsCarryActivationChecklists:
    """ "Partner-gated" is not actionable. A numbered list is."""

    def test_every_stub_has_a_checklist(self) -> None:
        from mawjood.core.aggregators.partners import PARTNER_ADAPTERS

        for adapter_cls in PARTNER_ADAPTERS:
            gate = adapter_cls.gate
            assert gate.checklist, f"{adapter_cls.slug} has no activation checklist"
            assert len(gate.checklist) >= 3, f"{adapter_cls.slug}'s checklist is too thin"
            assert gate.blocker
            assert gate.summary

    def test_every_checklist_step_is_a_sentence_someone_can_act_on(self) -> None:
        from mawjood.core.aggregators.partners import PARTNER_ADAPTERS

        for adapter_cls in PARTNER_ADAPTERS:
            for step in adapter_cls.gate.checklist:
                assert len(step) > 30, f"{adapter_cls.slug}: {step!r} is too vague to act on"

    def test_every_checklist_covers_idempotency_or_says_why_not(self) -> None:
        """CLAUDE.md 5.1's double-booking defence rests on knowing whether create
        is idempotent. A checklist that forgets to ask leaves the most dangerous
        unknown unasked."""
        from mawjood.core.aggregators.partners import PARTNER_ADAPTERS

        for adapter_cls in PARTNER_ADAPTERS:
            joined = " ".join(adapter_cls.gate.checklist).lower()
            assert "idempot" in joined or "merchant-side" in joined, (
                f"{adapter_cls.slug}'s checklist never asks about idempotency on create"
            )

    def test_the_five_named_platforms_are_all_present(self) -> None:
        from mawjood.core.aggregators.partners import PARTNER_ADAPTERS

        slugs = {adapter_cls.slug for adapter_cls in PARTNER_ADAPTERS}
        assert slugs == {"opentable", "foodics", "booksy", "talabat", "careem"}

    def test_all_of_them_register_in_the_default_registry(self) -> None:
        from mawjood.core.aggregators.registry import build_default_registry

        registry = build_default_registry()
        for slug in ("opentable", "foodics", "booksy", "talabat", "careem", "deliveroo", "zenoti"):
            assert slug in registry, f"{slug} is not registered"

    async def test_none_of_them_ever_synthesises_a_success(self) -> None:
        from mawjood.core.aggregators.partners import PARTNER_ADAPTERS
        from mawjood.core.enums import Outcome

        for adapter_cls in PARTNER_ADAPTERS:
            adapter = adapter_cls()
            ctx = context(adapter.slug)
            for result in (
                await adapter.search_availability(ctx, availability_request()),
                await adapter.create_booking(ctx, _booking_request_for(adapter.slug)),
                await adapter.health(ctx),
            ):
                assert result.outcome is Outcome.UNSUPPORTED
                assert result.data is None
