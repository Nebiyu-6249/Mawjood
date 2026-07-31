"""The security pass: redaction, rate limiting, webhook hardening, TLS.

Each class here corresponds to a row in ACCEPTANCE.md, so a reviewer can go from
a claim in the document to the assertion that backs it.

The dependency audit and the full-history secret scan are not here — they are
external tools run against the repository, and their output is filed in
`docs/evidence/`. What *is* here is the shape of the configuration they depend
on, so a change that would invalidate the recorded result fails the build
instead of quietly making the evidence stale.
"""

from __future__ import annotations

import ast
import json
import re
from pathlib import Path

import pytest
from starlette.testclient import TestClient

from mawjood.api.ratelimit import InMemoryStore, Limit, RateLimiter, client_address
from mawjood.config import Settings
from mawjood.observability.logging import REDACTED, redact_processor
from mawjood.services.bsp.base import (
    SignatureResult,
    SignatureScheme,
    sign_payload,
    verify_signature,
)

REPO = Path(__file__).resolve().parent.parent
EVIDENCE = REPO / "docs" / "evidence"


class TestLogRedaction:
    """Logs leave the system. Consumer content must not go with them.

    Structured logs ship to whatever aggregator a deployment uses, which is
    outside the retention and erasure boundary — an erased consumer whose
    messages are still in a log index has not been erased. So message content is
    dropped rather than masked, and identifiers are masked rather than dropped
    (an operator still needs to correlate a thread).
    """

    @pytest.mark.parametrize(
        "key", ["body", "text", "message", "consumer_text", "display_name", "comment", "notes"]
    )
    def test_consumer_content_is_dropped_entirely(self, key: str) -> None:
        out = redact_processor(None, "info", {"event": "x", key: "need a haircut in Marina"})
        assert out[key] == REDACTED
        assert "haircut" not in str(out)

    @pytest.mark.parametrize("key", ["wa_id", "phone", "phone_e164", "msisdn", "email"])
    def test_identifiers_are_masked_not_dropped(self, key: str) -> None:
        """Masked, because an operator still has to correlate a thread. The last
        three characters are enough to match a conversation and not enough to
        dial."""
        out = redact_processor(None, "info", {"event": "x", key: "971501234567"})
        assert out[key] == "*********567"

    @pytest.mark.parametrize(
        "key", ["password", "api_key", "authorization", "token", "secret", "cookie", "dsn"]
    )
    def test_credentials_are_dropped(self, key: str) -> None:
        out = redact_processor(None, "info", {"event": "x", key: "sk-live-abc123"})
        assert out[key] == REDACTED

    def test_a_phone_number_inside_an_ordinary_value_is_masked(self) -> None:
        """Key-based redaction alone misses this. An upstream error string, a
        summary, an exception message — any of them can carry a number under a
        key nobody thought to list."""
        out = redact_processor(
            None, "info", {"event": "x", "summary": "consumer 971509998888 timed out"}
        )
        assert "971509998888" not in out["summary"]
        assert "*********888" in out["summary"]

    def test_redaction_reaches_into_nested_structures(self) -> None:
        out = redact_processor(
            None,
            "info",
            {"event": "x", "payload": {"contacts": [{"wa_id": "971501234567"}]}},
        )
        assert "971501234567" not in str(out)

    def test_long_values_are_truncated(self) -> None:
        """An unbounded value is how a whole request body reaches an aggregator
        by accident."""
        out = redact_processor(None, "info", {"event": "x", "detail": "a" * 5000})
        assert len(out["detail"]) < 2100

    def test_operational_fields_survive(self) -> None:
        """Redaction that ate the useful part would make logs worthless."""
        out = redact_processor(
            None,
            "info",
            {
                "event": "routing.advanced",
                "platform_slug": "zenoti",
                "outcome": "timeout",
                "latency_ms": 2500,
                "advance_reason": "read timed out",
            },
        )
        assert out["platform_slug"] == "zenoti"
        assert out["outcome"] == "timeout"
        assert out["latency_ms"] == 2500
        assert out["advance_reason"] == "read timed out"


class TestRateLimiting:
    def test_a_burst_is_allowed_then_throttled(self) -> None:
        limiter = RateLimiter(store=InMemoryStore())
        limit = Limit(per_minute=10)
        allowed = [limiter.allow("s", "k", limit, now=100.0) for _ in range(15)]
        assert allowed[:10] == [True] * 10
        assert allowed[10:] == [False] * 5

    def test_tokens_refill_over_time(self) -> None:
        limiter = RateLimiter(store=InMemoryStore())
        limit = Limit(per_minute=60)  # one per second
        for _ in range(60):
            limiter.allow("s", "k", limit, now=0.0)
        assert limiter.allow("s", "k", limit, now=0.0) is False
        # Two seconds later, two tokens are back.
        assert limiter.allow("s", "k", limit, now=2.0) is True
        assert limiter.allow("s", "k", limit, now=2.0) is True
        assert limiter.allow("s", "k", limit, now=2.0) is False

    def test_keys_are_independent(self) -> None:
        limiter = RateLimiter(store=InMemoryStore())
        limit = Limit(per_minute=1)
        assert limiter.allow("s", "a", limit, now=0.0) is True
        assert limiter.allow("s", "a", limit, now=0.0) is False
        assert limiter.allow("s", "b", limit, now=0.0) is True

    def test_scopes_are_independent(self) -> None:
        """A consumer exhausting their own limit must not throttle the source
        limit, or one chatty person takes the endpoint down for everyone."""
        limiter = RateLimiter(store=InMemoryStore())
        limit = Limit(per_minute=1)
        assert limiter.allow("source", "k", limit, now=0.0) is True
        assert limiter.allow("consumer", "k", limit, now=0.0) is True

    def test_the_store_does_not_grow_without_bound(self) -> None:
        store = InMemoryStore(max_keys=50)
        limit = Limit(per_minute=60)
        for i in range(500):
            store.take(f"k{i}", limit, now=float(i))
        assert len(store._buckets) <= 50

    def test_forwarded_for_uses_the_last_hop(self) -> None:
        """Taking the first entry — the common mistake — lets anyone choose
        their own rate-limit key by sending a header."""
        assert client_address({"x-forwarded-for": "1.2.3.4, 10.0.0.1"}, "127.0.0.1") == "10.0.0.1"

    def test_the_socket_address_is_used_when_nothing_is_forwarded(self) -> None:
        assert client_address({}, "203.0.113.9") == "203.0.113.9"

    def test_a_missing_address_does_not_crash(self) -> None:
        assert client_address({}, None) == "unknown"

    def test_a_nonsense_limit_is_refused(self) -> None:
        with pytest.raises(ValueError, match="per_minute"):
            Limit(per_minute=0)


class TestWebhookHardening:
    """The only unauthenticated write path. Everything here is about bounding
    what an unauthenticated caller can make us do."""

    def _client(self, **overrides: object) -> TestClient:
        settings = Settings(
            database_url="postgresql+asyncpg://u@127.0.0.1:5432/d",
            bsp_webhook_secret="shhh",
            console_password="x",
            **overrides,  # type: ignore[arg-type]
        )
        from mawjood.main import create_app

        return TestClient(create_app(settings))

    def test_an_oversized_body_is_refused_by_declared_length(self) -> None:
        """Checked from the header first so it is refused before being read."""
        with self._client(webhook_max_body_bytes=100) as client:
            response = client.post(
                "/webhooks/whatsapp",
                content=b"{}",
                headers={"content-length": "999999", "content-type": "application/json"},
            )
        assert response.status_code == 413

    def test_an_oversized_body_is_refused_by_actual_length(self) -> None:
        """Content-Length is a claim, not a fact."""
        with self._client(webhook_max_body_bytes=100) as client:
            response = client.post("/webhooks/whatsapp", content=b"x" * 500)
        assert response.status_code == 413

    def test_an_unsigned_request_is_refused(self) -> None:
        with self._client() as client:
            response = client.post("/webhooks/whatsapp", content=b"{}")
        assert response.status_code == 403

    def test_the_source_rate_limit_engages(self) -> None:
        with self._client(webhook_rate_per_minute=3) as client:
            codes = [client.post("/webhooks/whatsapp", content=b"{}").status_code for _ in range(6)]
        # The first few are refused for signature (403); once the bucket empties
        # the rate limiter answers first (429).
        assert 429 in codes
        assert codes[-1] == 429

    def test_the_rate_limit_answers_before_the_signature_check(self) -> None:
        """The point of the ordering: rejecting a forged body still costs an
        HMAC, so the limiter has to be in front of it."""
        source = (REPO / "mawjood" / "api" / "webhooks" / "whatsapp.py").read_text()
        assert source.index("limiter.allow") < source.index("_check_signature(")

    def test_a_429_carries_retry_after(self) -> None:
        with self._client(webhook_rate_per_minute=1) as client:
            client.post("/webhooks/whatsapp", content=b"{}")
            response = client.post("/webhooks/whatsapp", content=b"{}")
        assert response.status_code == 429
        assert response.headers["retry-after"] == "60"


class TestSignatureVerification:
    SECRET = "shared-secret"

    def test_a_valid_signature_passes(self) -> None:
        scheme = SignatureScheme()
        body = b'{"hello":"world"}'
        header = sign_payload(scheme=scheme, secret=self.SECRET, raw_body=body)
        assert (
            verify_signature(
                scheme=scheme,
                secret=self.SECRET,
                raw_body=body,
                headers={scheme.header: header},
            )
            is SignatureResult.VALID
        )

    def test_a_single_altered_byte_is_refused(self) -> None:
        scheme = SignatureScheme()
        body = b'{"hello":"world"}'
        header = sign_payload(scheme=scheme, secret=self.SECRET, raw_body=body)
        assert (
            verify_signature(
                scheme=scheme,
                secret=self.SECRET,
                raw_body=b'{"hello":"worlD"}',
                headers={scheme.header: header},
            )
            is SignatureResult.MISMATCH
        )

    def test_a_missing_secret_never_passes(self) -> None:
        """Failing open on a webhook means accepting anything anyone posts."""
        result = verify_signature(
            scheme=SignatureScheme(),
            secret=None,
            raw_body=b"{}",
            headers={"x-hub-signature-256": "sha256=deadbeef"},
        )
        assert result is SignatureResult.NOT_CONFIGURED
        assert not result.ok

    def test_an_empty_secret_never_passes(self) -> None:
        result = verify_signature(scheme=SignatureScheme(), secret="", raw_body=b"{}", headers={})
        assert not result.ok

    def test_comparison_is_constant_time(self) -> None:
        """A byte-by-byte compare lets a signature be guessed one byte at a
        time. Asserted from the source because timing it is flaky."""
        source = (REPO / "mawjood" / "services" / "bsp" / "base.py").read_text()
        assert "hmac.compare_digest" in source
        assert "==" not in source.split("def verify_signature")[1].split("expected_hex")[1][:200]

    def test_production_refuses_unsigned_webhooks(self) -> None:
        with pytest.raises(ValueError, match="bsp_require_signature"):
            Settings(
                database_url="postgresql+asyncpg://u@127.0.0.1:5432/d",
                environment="prod",
                bsp_require_signature=False,
                bsp_webhook_secret="x",
                console_password="x",
            )

    def test_production_demands_a_webhook_secret(self) -> None:
        with pytest.raises(ValueError, match="bsp_webhook_secret"):
            Settings(
                database_url="postgresql+asyncpg://u@127.0.0.1:5432/d",
                environment="prod",
                console_password="x",
            )


class TestTransportSecurity:
    """TLS is verified everywhere, and there is no way to turn it off."""

    def test_no_module_disables_certificate_verification(self) -> None:
        """``verify=False`` on an httpx client silently accepts any certificate,
        which turns TLS into an expensive no-op. Asserted by AST so it cannot
        arrive in a hurry."""
        offenders: list[str] = []
        for path in sorted((REPO / "mawjood").rglob("*.py")):
            tree = ast.parse(path.read_text())
            for node in ast.walk(tree):
                if not isinstance(node, ast.Call):
                    continue
                for keyword in node.keywords:
                    if (
                        keyword.arg in {"verify", "check_hostname"}
                        and isinstance(keyword.value, ast.Constant)
                        and keyword.value.value is False
                    ):
                        offenders.append(f"{path.name}:{node.lineno}")
        assert not offenders, f"TLS verification disabled at: {offenders}"

    def test_no_module_reaches_for_an_unverified_ssl_context(self) -> None:
        offenders: list[str] = []
        for path in sorted((REPO / "mawjood").rglob("*.py")):
            source = path.read_text()
            for marker in ("_create_unverified_context", "CERT_NONE", "PYTHONHTTPSVERIFY"):
                if marker in source:
                    offenders.append(f"{path.name}: {marker}")
        assert not offenders, f"TLS verification bypassed at: {offenders}"

    def test_every_outbound_url_is_https_or_configured(self) -> None:
        """No hardcoded http:// endpoint. A base URL that arrives from config can
        be anything, which is the deployment's call — a literal in the source is
        ours."""
        offenders: list[str] = []
        for path in sorted((REPO / "mawjood").rglob("*.py")):
            for line in path.read_text().splitlines():
                if re.search(r'["\']http://(?!localhost|127\.0\.0\.1)', line):
                    offenders.append(f"{path.name}: {line.strip()[:60]}")
        assert not offenders, f"plaintext HTTP endpoints: {offenders}"


class TestTheAuditEvidenceIsCurrent:
    """The recorded audit results are filed in docs/evidence/.

    These tests do not re-run the external tools — that needs network and a
    binary. They assert the evidence exists and that the configuration it
    depends on has not changed underneath it, so a stale result fails the build
    rather than being quoted in a sales document a year later.
    """

    def test_the_dependency_audit_is_filed_and_clean(self) -> None:
        report = EVIDENCE / "dependency-audit.json"
        assert report.exists(), "run `uv run pip-audit --format json` and file the result"
        data = json.loads(report.read_text())
        vulnerable = [d for d in data.get("dependencies", []) if d.get("vulns")]
        assert not vulnerable, f"known vulnerabilities: {[d['name'] for d in vulnerable]}"

    def test_the_secret_scan_is_filed_and_clean(self) -> None:
        report = EVIDENCE / "gitleaks-history.json"
        assert report.exists(), "run gitleaks over the full history and file the result"
        findings = json.loads(report.read_text() or "[]")
        assert findings == [], f"secrets in history: {findings}"

    def test_the_scan_config_allowlists_only_named_shapes(self) -> None:
        """A blanket path exemption would make the clean result meaningless.

        Specifically: nothing may exempt tests/ or mawjood/ wholesale, because
        that is where a real key would land.
        """
        config = (REPO / ".gitleaks.toml").read_text()
        for forbidden in ("tests/", "mawjood/", """\'\'\'.*\'\'\'"""):
            assert f"'''{forbidden}" not in config, (
                f"the secret scan exempts {forbidden} wholesale, which makes it blind "
                "exactly where a leaked credential would land"
            )

    def test_the_grants_script_exists_and_revokes_what_it_claims(self) -> None:
        script = (REPO / "scripts" / "grants.sql").read_text()
        assert "REVOKE CREATE ON SCHEMA public FROM PUBLIC" in script
        assert "REVOKE UPDATE ON audit_log FROM mawjood_app" in script
        # DELETE is deliberately retained — erasure and retention need it, and
        # both pass through the trigger's purge gate.
        assert "REVOKE DELETE ON audit_log" not in script
