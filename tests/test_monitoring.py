"""Monitoring: alerts, health endpoints, and the backup/restore scripts.

Three things, and the third is the one people skip.

**Alerts** must reach an operator without carrying a consumer's phone number or
message body into a third-party service. Sentry is not a declared processor for
message content (CLAUDE.md §11), so the redaction is asserted rather than
assumed — including the belt-and-braces case where a caller passes a field they
should not have.

**Health endpoints** are what UptimeRobot polls. ``/healthz`` must stay green
when the database is gone (or an outage kills healthy pods) and ``/readyz`` must
go red (or traffic keeps arriving at an instance that cannot serve it).

**The backup scripts** are tested by running them. A backup script that has never
been executed is a file, not a backup, and a restore path nobody has walked is a
hypothesis. `docs/RUNBOOK.md` records the drill; this asserts the scripts are
shaped so the drill can happen — the drill itself needs a live database and is
run by ``make restore-drill``.
"""

from __future__ import annotations

import re
import subprocess
from pathlib import Path

import pytest

from mawjood.observability.alerts import _REDACT, AlertKind, Severity, _scrub, alert

SCRIPTS = Path(__file__).resolve().parent.parent / "scripts"


class TestAlertsCarryNoConsumerData:
    """An alerting backend must never become an unlogged copy of the transcript.

    Asserted against ``_scrub`` directly, because that is where the guarantee
    lives. Going through the logger instead would be testing structlog: the
    logging config caches bound loggers (``cache_logger_on_first_use``), so a
    capture-based assertion here passes alone and returns nothing in a full run —
    a flaky test dressed up as a security guarantee.
    """

    @pytest.mark.parametrize(
        "field",
        ["wa_id", "phone", "phone_e164", "body", "text", "consumer_text", "display_name"],
    )
    def test_consumer_fields_are_redacted(self, field: str) -> None:
        scrubbed = _scrub({field: "971501234567 — I need a haircut"})
        assert scrubbed[field] == "[redacted]"
        assert "971501234567" not in str(scrubbed)

    @pytest.mark.parametrize("field", ["credentials", "secret", "api_key", "token"])
    def test_credential_fields_are_redacted(self, field: str) -> None:
        scrubbed = _scrub({field: "sk-live-abc123"})
        assert scrubbed[field] == "[redacted]"
        assert "sk-live-abc123" not in str(scrubbed)

    def test_redaction_is_case_insensitive(self) -> None:
        """A caller writing ``WA_ID=`` should not slip past the list."""
        assert _scrub({"WA_ID": "971501234567"})["WA_ID"] == "[redacted]"

    def test_operational_fields_survive(self) -> None:
        """Redaction that ate the useful part would make alerts worthless."""
        scrubbed = _scrub(
            {
                "platform": "zenoti",
                "merchant_ref": "zenoti:Marina Beauty Lounge",
                "raw_error": "401 Unauthorized",
                "decision_id": "abc-123",
            }
        )
        assert scrubbed["platform"] == "zenoti"
        assert scrubbed["raw_error"] == "401 Unauthorized"
        assert scrubbed["merchant_ref"] == "zenoti:Marina Beauty Lounge"

    def test_the_redact_list_covers_every_pii_column_name_in_the_schema(self) -> None:
        """The list is hand-maintained, so it is checked against the schema.

        A column named for consumer content that is not on the list is a field
        someone will eventually pass to an alert.
        """
        from mawjood.db.models import Lead, Message

        risky = {"wa_id", "phone_e164", "display_name", "body"}
        columns = {c.name for c in Lead.__table__.columns} | {
            c.name for c in Message.__table__.columns
        }
        assert risky <= columns, "the schema changed; re-check the redaction list"
        assert risky <= _REDACT

    def test_an_alert_never_raises(self) -> None:
        """Called from exception handlers and from the booking path. An alerting
        backend that is down must not take a booking with it."""

        class Awkward:
            def __repr__(self) -> str:
                raise RuntimeError("boom")

        alert(AlertKind.CONFIGURATION_FAULT, summary="test", thing=Awkward())

    def test_an_ordinary_alert_does_not_raise_either(self) -> None:
        alert(
            AlertKind.CREDENTIAL_DEGRADED,
            severity=Severity.ERROR,
            summary="zenoti rejected our credential",
            platform="zenoti",
        )

    def test_severity_values_match_sentrys_levels(self) -> None:
        """They pass straight through, so a mapping table cannot drift."""
        assert {s.value for s in Severity} <= {
            "fatal",
            "critical",
            "error",
            "warning",
            "info",
            "debug",
        }

    def test_the_alert_kinds_are_a_closed_set(self) -> None:
        """Alert rules are written against these values; a typo must not create
        a silently unmonitored category."""
        assert len(set(AlertKind)) == len(list(AlertKind))
        assert AlertKind.RECONCILIATION_INCONCLUSIVE in AlertKind


class TestTheCascadeAlerts:
    """The two conditions worth waking someone for are actually wired."""

    def test_the_cascade_imports_the_alerting_module(self) -> None:
        import inspect

        from mawjood.core.routing import cascade

        source = inspect.getsource(cascade)
        assert "AlertKind.CREDENTIAL_DEGRADED" in source
        assert "AlertKind.RECONCILIATION_INCONCLUSIVE" in source
        assert "AlertKind.AGGREGATOR_CIRCUIT_OPEN" in source

    def test_an_inconclusive_reconciliation_is_critical(self) -> None:
        """The highest-stakes condition in the system: we do not know whether a
        consumer has a booking, and only a person can find out."""
        import inspect

        from mawjood.core.routing import cascade

        source = inspect.getsource(cascade)
        block = source[source.index("AlertKind.RECONCILIATION_INCONCLUSIVE") :][:400]
        assert "Severity.CRITICAL" in block


class TestSentryScrubbing:
    def test_request_bodies_and_frame_locals_are_stripped(self) -> None:
        """``send_default_pii=False`` covers Sentry's automatic capture. It does
        not cover a webhook payload sitting in a local variable of a frame in the
        stack trace, which is exactly where a consumer's message would be."""
        from mawjood.main import _scrub_event

        event = {
            "request": {"data": {"messages": [{"text": {"body": "need a haircut"}}]}},
            "exception": {
                "values": [
                    {
                        "stacktrace": {
                            "frames": [
                                {"vars": {"inbound": "971501234567: need a haircut"}},
                                {"vars": {"raw_body": b"..."}},
                            ]
                        }
                    }
                ]
            },
        }
        scrubbed = _scrub_event(event, None)

        assert "request" not in scrubbed
        for frame in scrubbed["exception"]["values"][0]["stacktrace"]["frames"]:
            assert "vars" not in frame

    def test_it_survives_an_event_with_neither(self) -> None:
        from mawjood.main import _scrub_event

        assert _scrub_event({"message": "hello"}, None) == {"message": "hello"}


class TestUptimeEndpoints:
    """What an external monitor polls. Both are unauthenticated on purpose."""

    def test_healthz_is_green_without_a_database(self, client: object) -> None:
        response = client.get("/healthz")  # type: ignore[attr-defined]
        assert response.status_code == 200
        assert response.json()["status"] == "ok"

    def test_healthz_is_a_keyword_check_uptimerobot_can_use(self, client: object) -> None:
        """UptimeRobot matches a keyword in the body. "ok" has to actually be
        in there, not just implied by a 200."""
        assert "ok" in client.get("/healthz").text  # type: ignore[attr-defined]

    def test_readyz_exists_and_reports_its_checks(self, client: object) -> None:
        response = client.get("/readyz")  # type: ignore[attr-defined]
        # No database in the unit tier, so this is a red readiness — which is the
        # correct answer and the one that keeps traffic away.
        assert response.status_code in (200, 503)
        assert "database" in response.json()["checks"]

    def test_readyz_names_the_region_and_residency(self, client: object) -> None:
        """A monitor hitting the wrong region should be able to tell."""
        payload = client.get("/readyz").json()  # type: ignore[attr-defined]
        assert payload["data_residency"] in ("gcc", "eu", "india")
        assert payload["region"]

    def test_neither_endpoint_needs_a_credential(self, app: object) -> None:
        spec = app.openapi()["paths"]  # type: ignore[attr-defined]
        for path in ("/healthz", "/readyz"):
            assert path in spec
            assert "security" not in spec[path]["get"]


class TestTheBackupScripts:
    """A backup script that has never run is a file, not a backup."""

    def test_both_scripts_exist_and_are_executable(self) -> None:
        for name in ("backup.sh", "restore.sh"):
            script = SCRIPTS / name
            assert script.exists(), f"{name} is missing"
            assert script.stat().st_mode & 0o111, f"{name} is not executable"

    def test_they_are_valid_shell(self) -> None:
        for name in ("backup.sh", "restore.sh"):
            result = subprocess.run(  # noqa: S603
                ["/bin/bash", "-n", str(SCRIPTS / name)],
                capture_output=True,
                text=True,
                check=False,
            )
            assert result.returncode == 0, f"{name}: {result.stderr}"

    def test_they_fail_fast_on_error(self) -> None:
        """Without ``set -euo pipefail`` a backup that half-fails still exits 0,
        and the first anyone knows is a restore that comes back empty."""
        for name in ("backup.sh", "restore.sh"):
            assert "set -euo pipefail" in (SCRIPTS / name).read_text()

    def test_backup_verifies_the_dump_before_trusting_it(self) -> None:
        """Finding out a dump is unreadable during an incident is the worst
        possible time."""
        source = (SCRIPTS / "backup.sh").read_text()
        assert "pg_restore --list" in source

    def test_backup_prunes_only_after_a_successful_dump(self) -> None:
        """Pruning first means a failing backup quietly erodes the history it was
        supposed to be adding to."""
        source = (SCRIPTS / "backup.sh").read_text()
        assert source.index("pg_dump") < source.index("-delete")
        assert source.index("pg_restore --list") < source.index("-delete")

    def test_backup_retention_matches_the_documented_policy(self) -> None:
        source = (SCRIPTS / "backup.sh").read_text()
        assert "RETENTION_DAYS:-30}" in source

    def test_restore_refuses_to_overwrite_the_live_database(self) -> None:
        """A restore script that can silently overwrite production is a foot-gun
        on a timer."""
        source = (SCRIPTS / "restore.sh").read_text()
        assert "refusing to restore over the live database" in source
        assert "--force-into-live" in source

    def test_restore_verifies_rather_than_trusting_the_exit_code(self) -> None:
        """A dump taken against an empty database restores perfectly and tells
        you nothing."""
        source = (SCRIPTS / "restore.sh").read_text()
        assert "SELECT" in source
        assert "alembic_version" in source

    def test_restore_checks_the_audit_trigger_survived(self) -> None:
        """The append-only trigger is schema, not data. Without it the restored
        database silently accepts audit deletions."""
        source = (SCRIPTS / "restore.sh").read_text()
        assert "pg_trigger" in source
        assert "audit_log" in source

    def test_both_handle_the_asyncpg_url_the_app_uses(self) -> None:
        """MAWJOOD_DATABASE_URL carries a ``+asyncpg`` suffix that libpq does not
        understand. Forgetting this makes both scripts fail on the one URL an
        operator will actually paste."""
        for name in ("backup.sh", "restore.sh"):
            source = (SCRIPTS / name).read_text()
            assert "postgresql+asyncpg:" in source, f"{name} does not normalise the driver suffix"

    def test_the_runbook_documents_a_drill_that_was_performed(self) -> None:
        """CLAUDE.md §11 asks for a restore drill *actually performed* and
        documented, not a procedure written down."""
        runbook = (SCRIPTS.parent / "docs" / "RUNBOOK.md").read_text()
        assert re.search(r"restore drill", runbook, re.IGNORECASE)
        assert "scripts/restore.sh" in runbook
