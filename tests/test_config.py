"""Configuration behaviour, including the residency rules.

"Never US-only" is a compliance requirement. It is enforced twice: the residency
declaration is a closed set with no US member, and the provider region code is
rejected if it looks American. These tests are the early instalment of the
Phase 4 "no US-region assumptions" check.
"""

from __future__ import annotations

from typing import Any

import pytest
from pydantic import ValidationError

from mawjood.config import Environment, Settings

from .conftest import PLACEHOLDER_DATABASE_URL


def _settings(**overrides: Any) -> Settings:
    """Build Settings with a valid DSN plus whatever the test wants to vary."""
    return Settings(database_url=PLACEHOLDER_DATABASE_URL, **overrides)


class TestResidency:
    @pytest.mark.parametrize(
        "region",
        [
            "us-east-1",
            "us-west-2",
            "US-EAST-1",
            "us_central1",
            "useast2",
            "eastus",
            "westus3",
            "centralus",
            "northamerica-northeast1",
        ],
    )
    def test_us_regions_are_refused(self, region: str) -> None:
        with pytest.raises(ValidationError, match="US region"):
            _settings(region=region)

    @pytest.mark.parametrize(
        "region",
        [
            "me-central-1",
            "me-central2",
            "me-south-1",
            "europe-west1",
            "eu-central-1",
            "asia-south1",
            "uaenorth",
        ],
    )
    def test_gcc_eu_and_india_regions_are_accepted(self, region: str) -> None:
        assert _settings(region=region).region == region

    def test_default_region_is_not_american(self) -> None:
        assert not _settings().region.lower().startswith("us")

    @pytest.mark.parametrize("residency", ["gcc", "eu", "india"])
    def test_permitted_residencies(self, residency: str) -> None:
        assert _settings(data_residency=residency).data_residency == residency

    @pytest.mark.parametrize("residency", ["us", "usa", "us-only", "global"])
    def test_us_residency_is_not_representable(self, residency: str) -> None:
        with pytest.raises(ValidationError):
            _settings(data_residency=residency)


class TestTimezone:
    def test_defaults_to_dubai(self) -> None:
        settings = _settings()
        assert settings.timezone == "Asia/Dubai"
        assert settings.tzinfo.key == "Asia/Dubai"

    def test_unknown_timezone_is_refused(self) -> None:
        with pytest.raises(ValidationError, match="unknown timezone"):
            _settings(timezone="Mars/Olympus_Mons")


class TestSecrets:
    def test_database_password_is_not_in_the_safe_url(self) -> None:
        safe = _settings().database_url_safe
        assert "mawjood@" in safe
        assert ":mawjood@127.0.0.1" not in safe
        assert safe.startswith("postgresql+asyncpg://")

    def test_database_url_is_required(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """No default, because a default would embed a credential."""
        monkeypatch.delenv("MAWJOOD_DATABASE_URL", raising=False)
        with pytest.raises(ValidationError):
            Settings(_env_file=None)

    def test_sentry_dsn_is_a_secret(self) -> None:
        settings = _settings(sentry_dsn="https://key@example.ingest.sentry.io/1")
        assert settings.sentry_dsn is not None
        assert "key" not in repr(settings.sentry_dsn)

    @pytest.mark.parametrize("blank", ["", "   "])
    def test_blank_sentry_dsn_is_unset_not_empty(self, blank: str) -> None:
        """.env.example ships MAWJOOD_SENTRY_DSN= and compose passes "" through.

        Without coercion the field becomes SecretStr("") rather than None, and
        the "is Sentry configured?" check silently passes with a blank DSN.
        """
        assert _settings(sentry_dsn=blank).sentry_dsn is None


class TestDefaults:
    def test_environment_defaults_to_local_and_is_not_production(self) -> None:
        settings = _settings()
        assert settings.environment is Environment.LOCAL
        assert settings.is_production is False

    def test_turn_deadline_matches_the_documented_soft_budget(self) -> None:
        """CLAUDE.md section 5.2: ~2.5s soft budget per turn."""
        assert _settings().turn_deadline_ms == 2500

    def test_retention_defaults(self) -> None:
        settings = _settings()
        assert settings.log_retention_days == 90
        assert settings.data_retention_days == 365

    def test_settings_are_frozen(self) -> None:
        with pytest.raises(ValidationError):
            _settings().region = "eu-west-1"  # type: ignore[misc]
