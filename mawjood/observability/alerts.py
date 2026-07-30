"""Operational alerts.

One function, :func:`alert`, called from the places where something has gone
wrong that a *person* needs to know about. It writes a structured log line and,
when Sentry is configured, raises a Sentry event.

## What belongs here, and what does not

An alert is for a condition an operator must act on. A consumer being handed to a
human is normal and is not an alert. An aggregator returning no availability is
normal and is not an alert. A *credential* being rejected is an alert, because
nobody outside this system can discover it and every consumer routed to that
merchant is quietly worse off until someone does.

The rule of thumb: if the system has already handled it correctly and the only
consequence is a line in a dashboard, log it. If it will keep happening until a
person intervenes, alert.

## Never the consumer's problem

Nothing in this module is consumer-facing, and nothing here changes what a
consumer sees. That is the point of the invariant: an operator gets paged, the
consumer gets a booking. Alert payloads therefore carry operational detail —
merchant references, error strings, counts — and **no message bodies and no
phone numbers**, so an alerting backend never becomes an unlogged copy of the
transcript.
"""

from __future__ import annotations

import contextlib
from enum import StrEnum
from typing import Any

from mawjood.observability.logging import get_logger

log = get_logger(__name__)

# Fields that must never leave the system in an alert, whatever a caller passes.
# Belt and braces on top of "do not pass them": an alert added at 3am under
# pressure should fail safe.
_REDACT = frozenset(
    {
        "wa_id",
        "phone",
        "phone_e164",
        "body",
        "text",
        "consumer_text",
        "message",
        "display_name",
        "credentials",
        "secret",
        "api_key",
        "token",
    }
)


class Severity(StrEnum):
    """Deliberately a subset of Sentry's levels.

    The values match Sentry's ``LogLevelStr`` so they pass straight through
    without a mapping table that could drift.
    """

    WARNING = "warning"
    ERROR = "error"
    CRITICAL = "critical"


class AlertKind(StrEnum):
    """The conditions worth waking someone for.

    A closed set rather than free strings, so alert rules in Sentry can be
    written against known values and a typo cannot silently create an
    unmonitored category.
    """

    # A merchant's credential was rejected. Every consumer routed to that
    # merchant is degraded until someone re-issues it, and nothing outside this
    # system will notice.
    CREDENTIAL_DEGRADED = "credential_degraded"
    # A booking timed out and reconciliation could not tell whether it landed.
    # A human must check the venue before the consumer is told anything.
    RECONCILIATION_INCONCLUSIVE = "reconciliation_inconclusive"
    # The cascade found nothing anywhere. Expected occasionally; a spike means a
    # platform is down or the routing config is wrong.
    CASCADE_EXHAUSTED = "cascade_exhausted"
    # Someone has been waiting for a human too long.
    HANDOFF_AGEING = "handoff_ageing"
    # A platform is refusing us wholesale.
    AGGREGATOR_CIRCUIT_OPEN = "aggregator_circuit_open"
    # Outbound delivery failed at the provider.
    DELIVERY_FAILED = "delivery_failed"
    # The scheduled-message backlog is growing. Expected non-zero until
    # templates are approved; growth is the signal.
    NOTIFICATION_BACKLOG = "notification_backlog"
    # Configuration is wrong in a way the system routed around but should not
    # have had to.
    CONFIGURATION_FAULT = "configuration_fault"


def _scrub(fields: dict[str, Any]) -> dict[str, Any]:
    return {
        key: ("[redacted]" if key.lower() in _REDACT else value) for key, value in fields.items()
    }


def alert(
    kind: AlertKind,
    *,
    severity: Severity = Severity.ERROR,
    summary: str,
    **fields: Any,
) -> None:
    """Raise an operational alert.

    Safe to call from anywhere, including inside an exception handler: an
    alerting backend that is down or misconfigured must never take a booking
    with it, so every failure here is swallowed after being logged.
    """
    # The whole body is guarded, not just the Sentry call. Rendering a field can
    # raise too — an object with a broken __repr__ is enough — and this runs on
    # the booking path. An alert must never be the reason a consumer loses a
    # booking, so the last resort is a bare log line with no payload at all.
    try:
        payload = _scrub(fields)
        logger = {
            Severity.WARNING: log.warning,
            Severity.ERROR: log.error,
            Severity.CRITICAL: log.critical,
        }[severity]
        logger("alert", alert_kind=str(kind), summary=summary, **payload)

        import sentry_sdk

        if sentry_sdk.get_client().is_active():
            with sentry_sdk.new_scope() as scope:
                scope.set_tag("alert_kind", str(kind))
                scope.set_level(severity.value)
                for key, value in payload.items():
                    scope.set_extra(key, value)
                sentry_sdk.capture_message(
                    f"[{kind}] {summary}",
                    level=severity.value,
                )
    except Exception:
        # Nothing left to try. Silence beats crashing the caller.
        with contextlib.suppress(Exception):
            log.error("alert.delivery_failed", alert_kind=str(kind), summary=summary)


__all__ = ["AlertKind", "Severity", "alert"]
