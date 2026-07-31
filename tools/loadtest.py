#!/usr/bin/env python
"""Replay realistic traffic and report turn latency.

    uv run python tools/loadtest.py --consumers 50 --concurrency 10
    uv run python tools/loadtest.py --consumers 200 --concurrency 25 --json

## What it measures, and what it does not

It measures **turn latency**: the time from an inbound message entering
``handle_inbound`` to the reply being ready. That is the number CLAUDE.md sets a
budget for (median under 3s) and the number a consumer experiences, minus network.

It does **not** measure the WhatsApp round trip, because there is no WhatsApp
number yet, and it does not measure a live aggregator, because there is no live
adapter. Both gaps are real and both are stated in ACCEPTANCE.md rather than
papered over with an assumed constant.

The aggregators are fakes with **deliberate latency**: the happy fake sleeps,
the timeout fake burns the full deadline. So the cascade being exercised is real
cascade code doing real waiting — what is synthetic is the upstream, not the
routing.

## Why it drives the pipeline rather than HTTP

``handle_inbound`` is the single code path both transports use. Driving it
directly removes uvicorn's accept loop and the test client's overhead from the
measurement, so the number describes Mawjood rather than the harness. The
webhook adds one HMAC and a JSON parse on top — microseconds against a budget
measured in seconds.

## Reading the output

Percentiles are computed per *turn kind*, not just overall, because the kinds do
different amounts of work and a single blended median hides that. A greeting
never touches an aggregator; a search runs the whole cascade. Averaging them
together produces a flattering number that answers nobody's question.
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import json
import statistics
import sys
import time
import uuid
from collections import defaultdict
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from mawjood.config import get_settings
from mawjood.core.aggregators.registry import AdapterRegistry
from mawjood.core.conversation.pipeline import handle_inbound
from mawjood.core.conversation.types import InboundMessage
from mawjood.core.enums import Channel
from mawjood.db.engine import create_engine, create_session_factory

# Each entry is (label, message). The label is what the report groups by, because
# these steps cost very different amounts and a blended median hides that.
#
# Two scenarios, because a latency number from the happy path alone is a
# half-truth. The degraded one is the same conversation against a category whose
# whole priority list fails — which is the slowest path a consumer can take and
# the one the 2.5s turn budget exists for.
SCENARIOS: dict[str, tuple[tuple[str, str], ...]] = {
    "happy": (
        ("greeting", "hi"),
        ("consent", "yes"),
        ("search", "need a haircut in Marina tomorrow at 5pm"),
        ("booking", "yes"),
    ),
    "degraded": (
        ("greeting", "hi"),
        ("consent", "yes"),
        # Routed to a category configured with only failing adapters, so this
        # turn runs the full cascade including a fake that burns the deadline.
        ("cascade_exhausted", "spa treatment in JLT tomorrow evening"),
    ),
}


@dataclass(slots=True)
class Sample:
    kind: str
    seconds: float
    replies: int
    error: str | None = None


@dataclass(slots=True)
class Report:
    samples: list[Sample] = field(default_factory=list)
    wall_seconds: float = 0.0
    consumers: int = 0
    concurrency: int = 0
    scenario: str = "happy"
    upstream_ms: int = 0

    def by_kind(self) -> dict[str, list[float]]:
        grouped: dict[str, list[float]] = defaultdict(list)
        for sample in self.samples:
            if sample.error is None:
                grouped[sample.kind].append(sample.seconds)
        return grouped

    @property
    def errors(self) -> list[Sample]:
        return [s for s in self.samples if s.error is not None]

    def as_dict(self) -> dict[str, Any]:
        def stats(values: list[float]) -> dict[str, Any]:
            ordered = sorted(values)
            return {
                "n": len(ordered),
                "p50": _percentile(ordered, 50),
                "p95": _percentile(ordered, 95),
                "p99": _percentile(ordered, 99),
                "max": ordered[-1] if ordered else 0.0,
                "mean": statistics.fmean(ordered) if ordered else 0.0,
            }

        overall = [s.seconds for s in self.samples if s.error is None]
        return {
            "scenario": self.scenario,
            "upstream_ms": self.upstream_ms,
            "consumers": self.consumers,
            "concurrency": self.concurrency,
            "wall_seconds": round(self.wall_seconds, 2),
            "turns": len(self.samples),
            "errors": len(self.errors),
            "turns_per_second": round(len(self.samples) / self.wall_seconds, 1)
            if self.wall_seconds
            else 0.0,
            "overall": stats(overall),
            "by_kind": {kind: stats(values) for kind, values in sorted(self.by_kind().items())},
        }


def _percentile(ordered: list[float], pct: float) -> float:
    """Nearest-rank percentile.

    Nearest-rank rather than interpolated: at these sample sizes interpolation
    invents a value between two real measurements, and a latency figure should
    be a thing that actually happened.
    """
    if not ordered:
        return 0.0
    rank = max(1, round(pct / 100.0 * len(ordered)))
    return round(ordered[min(rank, len(ordered)) - 1], 4)


async def _one_consumer(
    session_factory: object,
    settings: object,
    wa_id: str,
    report: Report,
    script: tuple[tuple[str, str], ...],
    registry: AdapterRegistry | None = None,
) -> None:
    for kind, text in script:
        started = time.perf_counter()
        error: str | None = None
        replies = 0
        try:
            async with session_factory() as session:  # type: ignore[operator]
                turn = await handle_inbound(
                    session,
                    InboundMessage(
                        tenant_slug=settings.default_tenant_slug,  # type: ignore[attr-defined]
                        channel=Channel.WHATSAPP,
                        wa_id=wa_id,
                        text=text,
                    ),
                    correlation_id=f"load-{uuid.uuid4().hex[:8]}",
                    settings=settings,
                    registry=registry,
                )
                replies = len(turn.outbound)
        except Exception as exc:
            error = f"{type(exc).__name__}: {exc}"
        report.samples.append(
            Sample(
                kind=kind,
                seconds=time.perf_counter() - started,
                replies=replies,
                error=error,
            )
        )


def _registry_with_upstream_latency(delay_ms: int) -> AdapterRegistry | None:
    """A registry whose fakes actually wait.

    By default the fakes *report* a latency and return instantly, which is right
    for a test suite and wrong for a latency measurement: it would produce a
    number describing only Mawjood's own work — database, state machine,
    phrasebank, cascade orchestration — with a zero-cost upstream.

    Passing ``--upstream-ms`` makes them sleep, so the reported figure includes
    a realistic aggregator round trip. Both numbers are published in
    ACCEPTANCE.md, because they answer different questions and quoting only the
    flattering one would be the sort of measurement-tuning the brief rules out.
    """
    if delay_ms <= 0:
        return None

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

    delay_s = delay_ms / 1000.0
    registry = AdapterRegistry()
    registry.register(ZenotiAdapter())
    registry.register(DeliverooAdapter())
    for partner in PARTNER_ADAPTERS:
        registry.register(partner())
    for fake in (
        HappyFake(delay_s=delay_s),
        EmptyFake(delay_s=delay_s),
        TimeoutFake(delay_s=delay_s),
        InconclusiveTimeoutFake(),
        FlakyFake(),
        AuthFailFake(delay_s=delay_s),
    ):
        registry.register(fake)
    return registry


async def run(consumers: int, concurrency: int, scenario: str, upstream_ms: int = 0) -> Report:
    settings = get_settings()
    engine = create_engine(settings)
    session_factory = create_session_factory(engine)
    report = Report(
        consumers=consumers, concurrency=concurrency, scenario=scenario, upstream_ms=upstream_ms
    )
    script = SCENARIOS[scenario]
    registry = _registry_with_upstream_latency(upstream_ms)
    semaphore = asyncio.Semaphore(concurrency)

    # A per-run prefix so every run starts with consumers the system has never
    # seen. Without it a second run against the same database resumes the first
    # run's conversations — which are already booked, or already handed off and
    # muted — and measures a completely different (and much faster) code path.
    # That is how a load test reports 85ms and means nothing.
    run_id = uuid.uuid4().hex[:6]

    async def worker(index: int) -> None:
        async with semaphore:
            # A distinct wa_id per virtual consumer. Reusing one would serialise
            # everything behind a single conversation row and measure lock
            # contention instead of throughput.
            await _one_consumer(
                session_factory, settings, f"9715{run_id}{index:06d}", report, script, registry
            )

    started = time.perf_counter()
    try:
        await asyncio.gather(*(worker(i) for i in range(consumers)))
    finally:
        report.wall_seconds = time.perf_counter() - started
        await engine.dispose()
    return report


def render(report: Report, budget_s: float) -> None:
    data = report.as_dict()
    overall = data["overall"]

    print()
    print(f"  scenario       {data['scenario']}   upstream {data['upstream_ms']}ms")
    print(f"  consumers      {data['consumers']}   concurrency {data['concurrency']}")
    print(f"  turns          {data['turns']}   errors {data['errors']}")
    print(f"  wall           {data['wall_seconds']}s   ({data['turns_per_second']} turns/s)")
    print()
    print(f"  {'turn':<12} {'n':>5} {'p50':>9} {'p95':>9} {'p99':>9} {'max':>9}")
    print(f"  {'-' * 12} {'-' * 5} {'-' * 9} {'-' * 9} {'-' * 9} {'-' * 9}")
    for kind, stats in data["by_kind"].items():
        print(
            f"  {kind:<12} {stats['n']:>5} {stats['p50']:>9.4f} "
            f"{stats['p95']:>9.4f} {stats['p99']:>9.4f} {stats['max']:>9.4f}"
        )
    print(
        f"  {'ALL':<12} {overall['n']:>5} {overall['p50']:>9.4f} "
        f"{overall['p95']:>9.4f} {overall['p99']:>9.4f} {overall['max']:>9.4f}"
    )
    print()

    median = overall["p50"]
    verdict = "PASS" if median < budget_s else "MISS"
    print(f"  median {median:.4f}s against a {budget_s:g}s budget — {verdict}")
    if report.errors:
        print(f"\n  {len(report.errors)} failed turn(s):")
        for sample in report.errors[:5]:
            print(f"    {sample.kind}: {sample.error}")
    print()


def main() -> int:
    parser = argparse.ArgumentParser(description="Replay traffic and report turn latency.")
    parser.add_argument("--consumers", type=int, default=50, help="virtual consumers")
    parser.add_argument("--concurrency", type=int, default=10, help="in flight at once")
    parser.add_argument("--budget", type=float, default=3.0, help="median target, seconds")
    parser.add_argument(
        "--scenario",
        choices=sorted(SCENARIOS),
        default="happy",
        help="happy books; degraded exhausts the cascade",
    )
    parser.add_argument(
        "--upstream-ms",
        type=int,
        default=0,
        help="make the fake aggregators actually wait this long per call",
    )
    parser.add_argument("--json", action="store_true", help="machine-readable output")
    args = parser.parse_args()

    # structlog prints to stdout, and the degraded scenario legitimately raises
    # credential alerts while running. Without this the log lines interleave with
    # the report and --json emits something no parser will accept.
    with contextlib.redirect_stdout(sys.stderr):
        report = asyncio.run(run(args.consumers, args.concurrency, args.scenario, args.upstream_ms))
    if args.json:
        print(json.dumps(report.as_dict(), indent=2))
    else:
        render(report, args.budget)

    overall = report.as_dict()["overall"]
    return 0 if overall["p50"] < args.budget else 1


if __name__ == "__main__":
    raise SystemExit(main())
