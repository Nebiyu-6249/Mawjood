"""Operator tools.

A package rather than loose scripts so one tool can import another — the routing
seeder is a door onto ``seed_dev`` rather than a second copy of it — and so the
type checker can resolve those imports.

Every file here still runs directly (``uv run python tools/seed_routing.py``);
each one puts the repository root on ``sys.path`` before importing ``mawjood``.
"""
