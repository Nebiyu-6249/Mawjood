"""Adapter conformance.

Every registered adapter — fake, live, or a documented stub — passes this suite.

The load-bearing assertion: **adapters never raise into the router.** Under fault
injection at the transport layer, every method still returns a ``Result`` carrying
an ``Outcome``. An exception escaping an adapter is how a consumer ends up seeing
a failure instead of a graceful pivot, which is the one thing the product may not
do.
"""

from __future__ import annotations

import uuid
from typing import Any

import pytest

from mawjood.core.aggregators.base import (
    AggregatorAdapter,
    AvailabilityRequest,
    BookingRef,
    BookingRequest,
    CallContext,
    Capabilities,
    Deadline,
    Result,
    Slot,
    make_idempotency_key,
)
from mawjood.core.aggregators.fake import (
    AuthFailFake,
    EmptyFake,
    FlakyFake,
    HappyFake,
    InconclusiveTimeoutFake,
    LowConfidenceFake,
    RateLimitedFake,
    TimeoutFake,
    UnsupportedFake,
    default_window,
    fake_merchant,
)
from mawjood.core.aggregators.registry import (
    AdapterRegistry,
    DuplicateAdapter,
    build_default_registry,
)
from mawjood.core.aggregators.zenoti import ZenotiAdapter
from mawjood.core.enums import Category, Outcome

# Every adapter that exists. New ones are added here and must pass unchanged.
ADAPTER_FACTORIES = [
    HappyFake,
    EmptyFake,
    TimeoutFake,
    InconclusiveTimeoutFake,
    FlakyFake,
    AuthFailFake,
    UnsupportedFake,
    RateLimitedFake,
    LowConfidenceFake,
    ZenotiAdapter,
]

ALL_METHODS = (
    "search_availability",
    "create_booking",
    "get_booking",
    "find_by_idempotency_key",
    "reschedule",
    "cancel",
    "health",
)


@pytest.fixture(params=ADAPTER_FACTORIES, ids=lambda f: f.slug)
def adapter(request: pytest.FixtureRequest) -> AggregatorAdapter:
    factory: Any = request.param
    return factory()  # type: ignore[no-any-return]


def context(slug: str) -> CallContext:
    return CallContext(
        tenant_id=uuid.uuid4(),
        conversation_id=uuid.uuid4(),
        merchant=fake_merchant(slug),
        credentials={"api_key": "conformance"},
        deadline=Deadline.in_ms(2000),
        correlation_id="conformance",
    )


def availability_request() -> AvailabilityRequest:
    start, end = default_window()
    return AvailabilityRequest(
        category=Category.SALON,
        service="haircut",
        window_start=start,
        window_end=end,
        area="Dubai Marina",
    )


def a_slot(slug: str) -> Slot:
    start, end = default_window()
    merchant = fake_merchant(slug)
    return Slot(
        slot_id="s-1",
        start=start,
        end=end,
        venue_name="Test Venue",
        service_name="haircut",
        merchant=merchant,
    )


def a_ref(slug: str) -> BookingRef:
    return BookingRef(external_id="x-1", platform_slug=slug, merchant=fake_merchant(slug))


async def call(adapter: AggregatorAdapter, method: str) -> Result[Any]:
    slug = adapter.slug
    ctx = context(slug)
    match method:
        case "search_availability":
            return await adapter.search_availability(ctx, availability_request())
        case "create_booking":
            slot = a_slot(slug)
            return await adapter.create_booking(
                ctx,
                BookingRequest(
                    slot=slot,
                    idempotency_key=make_idempotency_key(
                        conversation_id=ctx.conversation_id,
                        category=Category.SALON,
                        slot_start=slot.start,
                        platform_slug=slug,
                    ),
                ),
            )
        case "get_booking":
            return await adapter.get_booking(ctx, a_ref(slug))
        case "find_by_idempotency_key":
            return await adapter.find_by_idempotency_key(ctx, "conv:salon:t:slug")
        case "reschedule":
            return await adapter.reschedule(ctx, a_ref(slug), a_slot(slug))
        case "cancel":
            return await adapter.cancel(ctx, a_ref(slug), "consumer changed their mind")
        case "health":
            return await adapter.health(ctx)
    raise AssertionError(f"unknown method {method}")


class TestTheContract:
    def test_the_adapter_satisfies_the_protocol(self, adapter: AggregatorAdapter) -> None:
        assert isinstance(adapter, AggregatorAdapter)

    def test_it_declares_a_slug_categories_and_capabilities(
        self, adapter: AggregatorAdapter
    ) -> None:
        assert adapter.slug
        assert isinstance(adapter.categories, list)
        assert adapter.categories
        assert isinstance(adapter.capabilities, Capabilities)

    def test_it_implements_every_method(self, adapter: AggregatorAdapter) -> None:
        for method in ALL_METHODS:
            assert callable(getattr(adapter, method, None)), f"{adapter.slug} lacks {method}"

    @pytest.mark.parametrize("method", ALL_METHODS)
    async def test_every_method_returns_a_result(
        self, adapter: AggregatorAdapter, method: str
    ) -> None:
        result = await call(adapter, method)
        assert isinstance(result, Result)
        assert isinstance(result.outcome, Outcome)

    @pytest.mark.parametrize("method", ALL_METHODS)
    async def test_no_method_raises(self, adapter: AggregatorAdapter, method: str) -> None:
        """Adapters do not raise into the router. Ever."""
        try:
            await call(adapter, method)
        except Exception as exc:
            pytest.fail(f"{adapter.slug}.{method} raised {type(exc).__name__}: {exc}")

    @pytest.mark.parametrize("method", ALL_METHODS)
    async def test_a_zero_budget_does_not_provoke_an_exception(
        self, adapter: AggregatorAdapter, method: str
    ) -> None:
        """An already-expired deadline is a normal condition mid-cascade."""
        ctx = CallContext(
            tenant_id=uuid.uuid4(),
            conversation_id=uuid.uuid4(),
            merchant=fake_merchant(adapter.slug),
            credentials={},
            deadline=Deadline.expired_now(),
            correlation_id="expired",
        )
        assert ctx.deadline.expired
        result = await adapter.health(ctx)
        assert isinstance(result, Result)

    async def test_missing_credentials_produce_an_outcome_not_a_crash(
        self, adapter: AggregatorAdapter
    ) -> None:
        ctx = CallContext(
            tenant_id=uuid.uuid4(),
            conversation_id=uuid.uuid4(),
            merchant=fake_merchant(adapter.slug),
            credentials={},  # nothing resolved
            deadline=Deadline.in_ms(1000),
            correlation_id="no-creds",
        )
        result = await adapter.search_availability(ctx, availability_request())
        assert isinstance(result.outcome, Outcome)


class TestFaultInjection:
    """Adapters that break their contract must still not take the router down."""

    async def test_an_adapter_that_raises_is_contained_by_the_cascade(self) -> None:
        from mawjood.core.routing.cascade import Candidate, Cascade, SearchStatus

        from ..cascade.conftest import RecordingAudit

        class Exploding(HappyFake):
            slug = "fake_exploding"

            async def search_availability(self, ctx: Any, req: Any) -> Any:
                raise RuntimeError("kaboom")

        audit = RecordingAudit()
        engine = Cascade(
            audit=audit,
            deadline=Deadline.in_ms(2000),
            tenant_id=uuid.uuid4(),
            conversation_id=uuid.uuid4(),
            correlation_id="fault",
        )
        healthy = HappyFake()
        result = await engine.search(
            [
                Candidate(adapter=Exploding(), merchant=fake_merchant("fake_exploding")),
                Candidate(adapter=healthy, merchant=fake_merchant("fake_happy")),
            ],
            availability_request(),
        )

        assert result.status is SearchStatus.FOUND
        attempt = audit.of_type("routing.attempt")[0]
        assert attempt["outcome"] is Outcome.UPSTREAM_ERROR
        assert "RuntimeError" in attempt["raw_error"]

    async def test_a_result_has_no_truth_value(self) -> None:
        """`if result:` would treat NO_AVAILABILITY as failure and TIMEOUT as
        success. Both are wrong, so the type refuses to be coerced."""
        result = Result[Any](outcome=Outcome.NO_AVAILABILITY)
        with pytest.raises(TypeError, match="no truth value"):
            bool(result)


class TestRegistry:
    def test_adapters_register_and_resolve_by_slug(self) -> None:
        registry = AdapterRegistry()
        adapter = registry.register(HappyFake())
        assert registry.get("fake_happy") is adapter
        assert "fake_happy" in registry
        assert len(registry) == 1

    def test_a_duplicate_slug_is_refused(self) -> None:
        registry = AdapterRegistry()
        registry.register(HappyFake())
        with pytest.raises(DuplicateAdapter):
            registry.register(HappyFake())

    def test_an_unknown_slug_is_none_not_an_exception(self) -> None:
        assert AdapterRegistry().get("nope") is None

    def test_require_names_the_missing_slug(self) -> None:
        with pytest.raises(KeyError, match="nope"):
            AdapterRegistry().require("nope")

    def test_lookup_by_category(self) -> None:
        registry = AdapterRegistry()
        registry.register(ZenotiAdapter())
        assert registry.for_category(Category.SALON)
        assert registry.for_category(Category.FOOD_DELIVERY) == []

    def test_the_default_registry_builds(self) -> None:
        registry = build_default_registry()
        assert "zenoti" in registry


class TestZenotiIsHonestlyBlocked:
    """Zenoti is a documented stub because its docs are unreachable.

    These assertions exist so the stub cannot be mistaken for a working adapter,
    and so that whoever implements it has to remove them deliberately.
    """

    def test_it_declares_itself_unimplemented(self) -> None:
        assert ZenotiAdapter.implemented is False
        assert "unreachable" in ZenotiAdapter.blocked_reason

    def test_it_claims_no_capabilities(self) -> None:
        capabilities = ZenotiAdapter().capabilities
        assert not capabilities.can_search
        assert not capabilities.can_book

    @pytest.mark.parametrize("method", ALL_METHODS)
    async def test_every_method_reports_unsupported(self, method: str) -> None:
        result = await call(ZenotiAdapter(), method)
        assert result.outcome is Outcome.UNSUPPORTED

    async def test_it_never_synthesises_a_booking(self) -> None:
        result = await call(ZenotiAdapter(), "create_booking")
        assert result.outcome is Outcome.UNSUPPORTED
        assert result.data is None
