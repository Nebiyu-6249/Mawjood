"""Adapter discovery and capability declaration.

Adapters are stateless singletons held here. The registry is the authority on
which platform slugs exist; ``routing_config`` only decides their order. A slug
configured with no registered adapter is a configuration error the router
reports — it must never become a dead end for a consumer.
"""

from __future__ import annotations

from mawjood.core.aggregators.base import AggregatorAdapter
from mawjood.core.enums import Category
from mawjood.observability.logging import get_logger

log = get_logger(__name__)

# Warn once per process. Every turn builds a registry, and a warning on each
# one buries the signal it exists to carry.
_fakes_warned = False


class DuplicateAdapter(ValueError):
    """Two adapters claiming the same slug."""


class AdapterRegistry:
    """Slug to adapter."""

    def __init__(self) -> None:
        self._adapters: dict[str, AggregatorAdapter] = {}

    def register(self, adapter: AggregatorAdapter) -> AggregatorAdapter:
        slug = adapter.slug
        if slug in self._adapters:
            raise DuplicateAdapter(f"an adapter is already registered for slug {slug!r}")
        self._adapters[slug] = adapter
        log.debug(
            "aggregator.registered",
            slug=slug,
            categories=[str(c) for c in adapter.categories],
        )
        return adapter

    def get(self, slug: str) -> AggregatorAdapter | None:
        return self._adapters.get(slug)

    def require(self, slug: str) -> AggregatorAdapter:
        adapter = self.get(slug)
        if adapter is None:
            raise KeyError(f"no adapter registered for slug {slug!r}")
        return adapter

    @property
    def slugs(self) -> list[str]:
        return sorted(self._adapters)

    def for_category(self, category: Category) -> list[AggregatorAdapter]:
        return [a for a in self._adapters.values() if category in a.categories]

    def __contains__(self, slug: object) -> bool:
        return slug in self._adapters

    def __len__(self) -> int:
        return len(self._adapters)


def _warn_about_fakes(**fields: object) -> None:
    global _fakes_warned
    if not _fakes_warned:
        _fakes_warned = True
        log.warning("aggregator.fakes_registered", **fields)


def build_default_registry(*, include_fakes: bool = False) -> AdapterRegistry:
    """The adapters a running Mawjood has.

    Partner-gated platforms — and Zenoti, whose documentation is unreachable —
    register as documented stubs returning UNSUPPORTED, so they drop into the live
    system the day their contract is known without the router changing.

    ``include_fakes`` registers the test doubles. It is how a local demo books
    anything at all while no live adapter exists, and it is refused outright in
    production: a fake that reaches production would confirm bookings that do not
    exist, which is the worst failure this system could have.
    """
    from mawjood.core.aggregators.deliveroo import DeliverooAdapter
    from mawjood.core.aggregators.fake import (
        AuthFailFake,
        EmptyFake,
        FlakyFake,
        HappyFake,
        InconclusiveTimeoutFake,
        TimeoutFake,
    )
    from mawjood.core.aggregators.partners import PARTNER_ADAPTERS
    from mawjood.core.aggregators.zenoti import ZenotiAdapter

    registry = AdapterRegistry()
    registry.register(ZenotiAdapter())
    registry.register(DeliverooAdapter())
    for partner in PARTNER_ADAPTERS:
        registry.register(partner())

    if include_fakes:
        _warn_about_fakes(note="test doubles are active; bookings are not real")
        for fake in (
            HappyFake(),
            EmptyFake(),
            TimeoutFake(),
            InconclusiveTimeoutFake(),
            FlakyFake(),
            AuthFailFake(),
        ):
            registry.register(fake)

    return registry


def build_registry_for(settings: object) -> AdapterRegistry:
    """Build the registry a running process should use."""
    environment = str(getattr(settings, "environment", "local"))
    include_fakes = bool(getattr(settings, "enable_fake_adapters", False))
    if include_fakes and environment == "prod":
        raise RuntimeError(
            "fake adapters cannot be enabled in production: they would confirm "
            "bookings that do not exist"
        )
    return build_default_registry(include_fakes=include_fakes)


__all__ = [
    "AdapterRegistry",
    "DuplicateAdapter",
    "build_default_registry",
    "build_registry_for",
]
