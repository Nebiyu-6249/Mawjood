#!/usr/bin/env python
"""Work the handoff queue: list, claim, reply, release.

This exists because the console is strictly read-only. CLAUDE.md decision 7 wants
"queue + console reply + bot mute"; the queue and the mute live in the console,
and the reply lives here. The capability is unchanged — an operator can still
answer a consumer and hand the thread back to Mawjood — but it is not an HTTP
mutation route, so the console's read-only guarantee holds without argument.

    uv run python tools/handoff.py list
    uv run python tools/handoff.py claim <handoff-id> --operator dana
    uv run python tools/handoff.py reply <handoff-id> --operator dana --text "On it."
    uv run python tools/handoff.py release <handoff-id> --operator dana --notes "booked by phone"

An operator reply is the one consumer-facing text that does not come from the
phrasebank, because a human wrote it. That exception stays narrow: it is
attributable to a named operator, recorded verbatim under the
``operator.reply`` sentinel, and the assistant is silent while it happens.
"""

from __future__ import annotations

import argparse
import asyncio
import sys
import uuid
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from mawjood.config import get_settings
from mawjood.core.handoff import (
    HandoffAlreadyClaimed,
    HandoffNotFound,
    claim,
    open_queue,
    release,
    reply,
)
from mawjood.db.engine import create_engine, create_session_factory
from mawjood.db.repositories.core import TenantRepository


async def _run(args: argparse.Namespace) -> int:
    settings = get_settings()
    engine = create_engine(settings)
    session_factory = create_session_factory(engine)

    try:
        async with session_factory() as session:
            tenant = await TenantRepository(session).get_by_slug(settings.default_tenant_slug)
            if tenant is None:
                print(f"no tenant with slug {settings.default_tenant_slug!r}")
                return 1

            if args.command == "list":
                queue = await open_queue(session, tenant.id)
                if not queue:
                    print("Everyone has been looked after — nothing waiting.")
                    return 0
                print(f"{'handoff id':38}  {'waiting since':17}  {'reason':28}  status")
                for view in queue:
                    print(
                        f"{view.handoff.id!s:38}  "
                        f"{view.handoff.created_at:%d %b %H:%M}     "
                        f"{view.handoff.reason!s:28}  "
                        f"{view.handoff.status}"
                        + (f" ({view.handoff.claimed_by})" if view.handoff.claimed_by else "")
                    )
                return 0

            handoff_id = uuid.UUID(args.handoff_id)
            try:
                if args.command == "claim":
                    view = await claim(
                        session,
                        tenant_id=tenant.id,
                        handoff_id=handoff_id,
                        operator=args.operator,
                        correlation_id="cli",
                    )
                    print(f"claimed by {args.operator}: conversation {view.conversation.id}")
                elif args.command == "reply":
                    message = await reply(
                        session,
                        tenant_id=tenant.id,
                        handoff_id=handoff_id,
                        operator=args.operator,
                        text=args.text,
                        correlation_id="cli",
                    )
                    print(f"sent to conversation {message.conversation_id}: {message.body}")
                elif args.command == "release":
                    view = await release(
                        session,
                        tenant_id=tenant.id,
                        handoff_id=handoff_id,
                        operator=args.operator,
                        notes=args.notes,
                        correlation_id="cli",
                    )
                    print(
                        f"released: conversation {view.conversation.id} is back with Mawjood "
                        "and unmuted"
                    )
            except HandoffNotFound as exc:
                print(f"not found: {exc}")
                return 1
            except HandoffAlreadyClaimed as exc:
                print(f"refused: {exc}")
                return 1
            except ValueError as exc:
                print(f"refused: {exc}")
                return 1

            await session.commit()
        return 0
    finally:
        await engine.dispose()


def main() -> int:
    parser = argparse.ArgumentParser(description="Work the Mawjood handoff queue.")
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("list", help="everything waiting for a person, oldest first")

    for name, help_text in (
        ("claim", "take ownership of a conversation"),
        ("reply", "send a message to the consumer"),
        ("release", "hand the conversation back to Mawjood and unmute it"),
    ):
        p = sub.add_parser(name, help=help_text)
        p.add_argument("handoff_id", help="the handoff id, from `list`")
        p.add_argument("--operator", required=True, help="who is doing this")
        if name == "reply":
            p.add_argument("--text", required=True, help="what to send")
        if name == "release":
            p.add_argument("--notes", default=None, help="how it was resolved")

    return asyncio.run(_run(parser.parse_args()))


if __name__ == "__main__":
    raise SystemExit(main())
