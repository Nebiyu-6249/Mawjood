#!/usr/bin/env python
"""Terminal chat harness for Mawjood.

Talk to Mawjood from a terminal with **no BSP credentials, no WhatsApp number and
no live aggregators**. This is how Mawjood is demonstrated before a number is
provisioned, and how you iterate without a webhook tunnel.

It is a *transport*, not a second implementation. It builds an ``InboundMessage``
and calls ``handle_inbound`` — the same function the WhatsApp webhook calls, with
the same persistence, the same consent capture and the same audit trail.
``tests/test_transport_parity.py`` fails the build if the two ever diverge.

    make simulate
    uv run python tools/chat_sim.py --audit
    uv run python tools/chat_sim.py --script "hi" "yes" "need a haircut in Marina"

Flags:
    --audit         print the audit trail after every turn
    --script ...    run scripted messages then exit (used for demos and CI)
    --wa-id ID      pretend to be a specific consumer (default: a fresh one)
    --reset         delete this consumer's history before starting
"""

from __future__ import annotations

import argparse
import asyncio
import os
import sys
import uuid
from pathlib import Path

# Allow `python tools/chat_sim.py` without installing the package.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from sqlalchemy import delete, text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from mawjood.config import Settings, get_settings
from mawjood.core.conversation.pipeline import (
    UnknownTenant,
    deliver_deferred,
    handle_inbound,
)
from mawjood.core.conversation.types import InboundMessage
from mawjood.core.enums import Channel
from mawjood.db.engine import create_engine, create_session_factory
from mawjood.db.models import AuditLog, Lead
from mawjood.db.repositories.core import TenantRepository
from mawjood.observability.audit import format_trail, replay_turn
from mawjood.observability.logging import configure_logging

BOLD = "\033[1m"
DIM = "\033[2m"
CYAN = "\033[36m"
GREEN = "\033[32m"
YELLOW = "\033[33m"
RESET = "\033[0m"


def _colour(enabled: bool, code: str, value: str) -> str:
    return f"{code}{value}{RESET}" if enabled else value


async def _reset_consumer(
    session_factory: async_sessionmaker[AsyncSession],
    tenant_id: uuid.UUID,
    wa_id: str,
) -> None:
    """Wipe one consumer so a demo starts from first contact.

    audit_log is append-only and refuses deletes unless the session opts in —
    the same flag the PDPL erasure path uses. Doing it here keeps that path
    exercised rather than theoretical.
    """
    async with session_factory() as session:
        await session.execute(text("SET LOCAL mawjood.allow_purge = 'on'"))
        lead = (
            await session.execute(
                delete(Lead)
                .where(Lead.tenant_id == tenant_id)
                .where(Lead.wa_id == wa_id)
                .returning(Lead.id)
            )
        ).scalar_one_or_none()
        if lead is not None:
            await session.execute(delete(AuditLog).where(AuditLog.lead_id == lead))
        await session.commit()


async def run(args: argparse.Namespace, settings: Settings) -> int:
    colour = sys.stdout.isatty() and not args.no_colour
    engine = create_engine(settings)
    session_factory = create_session_factory(engine)

    try:
        async with session_factory() as session:
            tenant = await TenantRepository(session).ensure(
                settings.default_tenant_slug, settings.default_tenant_name
            )
            await session.commit()
            tenant_id = tenant.id

        wa_id = args.wa_id or f"sim-{uuid.uuid4().hex[:10]}"
        if args.reset:
            await _reset_consumer(session_factory, tenant_id, wa_id)

        print(_colour(colour, BOLD, "Mawjood chat simulator"))
        print(
            _colour(
                colour,
                DIM,
                f"tenant={settings.default_tenant_slug}  consumer={wa_id}  "
                f"channel=console\nSame pipeline as the WhatsApp webhook. "
                f"Ctrl-D or /quit to exit.\n",
            )
        )

        scripted = list(args.script or [])
        correlation_id = uuid.uuid4().hex

        while True:
            if scripted:
                line = scripted.pop(0)
                print(f"{_colour(colour, CYAN, 'you')} > {line}")
            else:
                if args.script is not None:
                    break
                try:
                    # input() blocks; keep it off the event loop.
                    line = await asyncio.to_thread(input, f"{_colour(colour, CYAN, 'you')} > ")
                except (EOFError, KeyboardInterrupt):
                    print()
                    break
                if line.strip() in {"/quit", "/exit"}:
                    break

            inbound = InboundMessage(
                tenant_slug=settings.default_tenant_slug,
                channel=Channel.CONSOLE,
                wa_id=wa_id,
                text=line,
            )

            async with session_factory() as session:
                try:
                    turn = await handle_inbound(
                        session, inbound, correlation_id=correlation_id, settings=settings
                    )
                except UnknownTenant as exc:
                    print(_colour(colour, YELLOW, f"configuration problem: {exc}"))
                    return 1

            for message in turn.outbound:
                print(f"{_colour(colour, GREEN, 'mawjood')} > {message.text}")

            # The turn budget expired mid-cascade: the holding pivot is already
            # above, and this finishes the work and delivers the follow-up. On
            # WhatsApp this is a background task; here we await it so the demo
            # shows the whole exchange.
            if (
                turn.deferred is not None
                and turn.conversation_id is not None
                and turn.lead_id is not None
            ):
                async with session_factory() as session:
                    follow_up = await deliver_deferred(
                        session,
                        tenant_slug=settings.default_tenant_slug,
                        conversation_id=turn.conversation_id,
                        lead_id=turn.lead_id,
                        channel=Channel.CONSOLE,
                        deferred=turn.deferred,
                        correlation_id=correlation_id,
                    )
                for message in follow_up.outbound:
                    print(
                        f"{_colour(colour, DIM, '(follow-up)')} "
                        f"{_colour(colour, GREEN, 'mawjood')} > {message.text}"
                    )

            if args.show_state and turn.state:
                print(_colour(colour, DIM, f"    [state: {turn.state}]"))

            if args.audit:
                async with session_factory() as session:
                    rows = await replay_turn(session, tenant_id, turn.turn_id)
                print(_colour(colour, DIM, f"\n--- audit trail: turn {turn.turn_id} ---"))
                print(_colour(colour, DIM, format_trail(rows)))
                print(_colour(colour, DIM, "-" * 60 + "\n"))

        return 0
    finally:
        await engine.dispose()


def main() -> int:
    parser = argparse.ArgumentParser(description="Chat with Mawjood from a terminal.")
    parser.add_argument("--audit", action="store_true", help="print the audit trail per turn")
    parser.add_argument("--script", nargs="*", help="run these messages then exit")
    parser.add_argument("--wa-id", help="consumer identifier to use")
    parser.add_argument("--reset", action="store_true", help="erase this consumer first")
    parser.add_argument("--no-colour", action="store_true", help="disable ANSI colour")
    parser.add_argument(
        "--show-state", action="store_true", help="print the state machine position per turn"
    )
    args = parser.parse_args()

    # Console output should be readable, not JSON, unless asked otherwise.
    os.environ.setdefault("MAWJOOD_LOG_FORMAT", "console")
    os.environ.setdefault("MAWJOOD_LOG_LEVEL", "WARNING")

    settings = get_settings()
    configure_logging(
        level=settings.log_level,
        log_format=settings.log_format,
        service_name=settings.service_name,
        environment=str(settings.environment),
        region=settings.region,
    )
    return asyncio.run(run(args, settings))


if __name__ == "__main__":
    raise SystemExit(main())
