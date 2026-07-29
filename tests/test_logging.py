"""Logging redaction.

Adapters are handed resolved credentials at call time. If those reach a log line
we have shipped every merchant's API key to log storage with 90-day retention.
Phase 1 asserts this end to end over captured adapter output; this is the unit
level guarantee underneath it.
"""

from __future__ import annotations

import json

import pytest

from mawjood.observability.logging import REDACTED, redact_processor


def _process(event: dict[str, object]) -> dict[str, object]:
    return dict(redact_processor(None, "info", event))


class TestCredentialRedaction:
    @pytest.mark.parametrize(
        "key",
        [
            "password",
            "passwd",
            "api_key",
            "apiKey",
            "api-key",
            "secret",
            "client_secret",
            "token",
            "access_token",
            "authorization",
            "Authorization",
            "credential",
            "credentials",
            "private_key",
            "aws_access_key",
            "signature",
            "cookie",
            "database_dsn",
        ],
    )
    def test_credential_keys_are_redacted(self, key: str) -> None:
        assert _process({key: "hunter2-the-real-value"})[key] == REDACTED

    def test_nested_credentials_are_redacted(self) -> None:
        out = _process(
            {
                "event": "adapter.call",
                "ctx": {
                    # Invented fixture value, never a real key.
                    "merchant": {"slug": "zenoti", "api_key": "zk_live_abcd1234"},  # gitleaks:allow
                    "deadline_ms": 2500,
                },
            }
        )
        assert "zk_live_abcd1234" not in json.dumps(out)
        ctx = out["ctx"]
        assert isinstance(ctx, dict)
        assert ctx["merchant"]["api_key"] == REDACTED
        assert ctx["merchant"]["slug"] == "zenoti"

    def test_credentials_inside_lists_are_redacted(self) -> None:
        out = _process({"attempts": [{"slug": "zenoti", "token": "t_secret_value"}]})
        assert "t_secret_value" not in json.dumps(out)

    def test_ordinary_fields_survive_untouched(self) -> None:
        out = _process({"event": "routing.advanced", "outcome": "no_availability", "attempt": 2})
        assert out["event"] == "routing.advanced"
        assert out["outcome"] == "no_availability"
        assert out["attempt"] == 2


class TestPiiMasking:
    @pytest.mark.parametrize("key", ["phone", "phone_number", "msisdn", "wa_id", "email"])
    def test_pii_keys_are_masked_to_the_last_three_characters(self, key: str) -> None:
        masked = _process({key: "+971501234567"})[key]
        assert masked == "**********567"

    def test_masking_keeps_enough_to_correlate_but_not_to_contact(self) -> None:
        masked = _process({"phone": "+971501234567"})["phone"]
        assert isinstance(masked, str)
        assert "971501234" not in masked
        assert masked.endswith("567")

    def test_short_values_are_fully_masked(self) -> None:
        assert _process({"phone": "12"})["phone"] == "**"
