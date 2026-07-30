#!/usr/bin/env python
"""Seed the routing configuration: merchants, and the priority list per category.

The entry point CLAUDE.md section 12 names. It is a door onto ``seed_dev.py``,
not a second implementation — two files that both write routing rows would drift,
and the one that drifted would be whichever nobody ran that week.

    uv run python tools/seed_routing.py
    uv run python tools/seed_routing.py --all-down

``--all-down`` configures salons so every candidate fails in a different way.
That is the invariant demo: the consumer still never hears that a shelf is
empty, they reach a person instead.

Idempotent. Running it twice changes nothing the second time.
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tools.seed_dev import seed


def main() -> int:
    parser = argparse.ArgumentParser(description="Seed merchants and routing priority lists.")
    parser.add_argument(
        "--all-down",
        action="store_true",
        help="configure salon routing so every candidate fails, for the invariant demo",
    )
    args = parser.parse_args()
    # with_routing is always on here — routing is the whole point of this door.
    return asyncio.run(seed(with_routing=True, all_down=args.all_down))


if __name__ == "__main__":
    raise SystemExit(main())
