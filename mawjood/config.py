"""Application configuration.

Everything comes from the environment. There are no hardcoded credentials and no
defaults that carry a secret. See ``.env.example`` for the full key list.

Two compliance rules are enforced here rather than left to documentation:

* ``data_residency`` is a closed set of {gcc, eu, india}. A US-only deployment is
  not representable, so it cannot be reached by a typo or an unset variable.
* ``region`` is rejected if it looks like a US region code. "Never US-only" is a
  requirement, so it fails at startup rather than in an audit.
"""

from __future__ import annotations

import re
from enum import StrEnum
from functools import lru_cache
from typing import Literal
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from pydantic import Field, PostgresDsn, SecretStr, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

# Region codes that would put consumer data in the United States. Matched against
# the configured region and refused. Extend as providers are added.
_US_REGION_PATTERN = re.compile(
    r"^(us[-_]|northamerica-|.*-us-|useast|uswest|centralus|eastus|westus)",
    re.IGNORECASE,
)


class Environment(StrEnum):
    LOCAL = "local"
    DEV = "dev"
    STAGING = "staging"
    PROD = "prod"


class Settings(BaseSettings):
    """Runtime configuration, read from the environment (prefix ``MAWJOOD_``)."""

    model_config = SettingsConfigDict(
        env_prefix="MAWJOOD_",
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        frozen=True,
    )

    # --- Identity -----------------------------------------------------------
    service_name: str = "mawjood"
    environment: Environment = Environment.LOCAL
    debug: bool = False

    # --- Residency and locale ----------------------------------------------
    # The compliance-relevant declaration. Deliberately has no US member.
    data_residency: Literal["gcc", "eu", "india"] = "gcc"
    # The provider-specific region code, whichever cloud is chosen in Phase 4.
    region: str = "me-central-1"
    timezone: str = "Asia/Dubai"
    default_locale: str = "en"

    # --- Database -----------------------------------------------------------
    # Required: no default, because a default would embed a credential.
    database_url: PostgresDsn
    database_pool_size: int = Field(default=5, ge=1, le=50)
    database_max_overflow: int = Field(default=5, ge=0, le=50)
    database_connect_timeout_s: float = Field(default=5.0, gt=0)
    database_echo: bool = False

    # --- Conversation turn budget ------------------------------------------
    # The soft deadline that triggers the holding pivot and moves the rest of the
    # cascade into the background. See CLAUDE.md section 5.2.
    turn_deadline_ms: int = Field(default=2500, gt=0)

    # --- Observability ------------------------------------------------------
    log_level: Literal["DEBUG", "INFO", "WARNING", "ERROR"] = "INFO"
    log_format: Literal["json", "console"] = "json"
    sentry_dsn: SecretStr | None = None
    sentry_traces_sample_rate: float = Field(default=0.1, ge=0.0, le=1.0)

    # --- Retention ----------------------------------------------------------
    log_retention_days: int = Field(default=90, ge=1)
    data_retention_days: int = Field(default=365, ge=1)

    @field_validator("sentry_dsn", mode="before")
    @classmethod
    def _empty_string_is_unset(cls, value: object) -> object:
        """Treat an empty env var as absent.

        ``.env.example`` ships ``MAWJOOD_SENTRY_DSN=`` and compose passes it
        through as "". Without this, the field becomes SecretStr("") rather than
        None and the "is it configured?" check silently succeeds.
        """
        if isinstance(value, str) and not value.strip():
            return None
        return value

    @field_validator("region")
    @classmethod
    def _reject_us_region(cls, value: str) -> str:
        if _US_REGION_PATTERN.match(value.strip()):
            raise ValueError(
                f"region {value!r} looks like a US region. Mawjood must be deployable "
                "to a GCC, EU, or India region and never US-only."
            )
        return value.strip()

    @field_validator("timezone")
    @classmethod
    def _validate_timezone(cls, value: str) -> str:
        try:
            ZoneInfo(value)
        except (ZoneInfoNotFoundError, ValueError) as exc:
            raise ValueError(f"unknown timezone {value!r}") from exc
        return value

    @property
    def tzinfo(self) -> ZoneInfo:
        return ZoneInfo(self.timezone)

    @property
    def is_production(self) -> bool:
        return self.environment is Environment.PROD

    @property
    def database_url_safe(self) -> str:
        """The database URL with the password removed, safe to log.

        Startup logs the database it connected to. That line must never carry the
        password, so the safe form is built explicitly rather than by trusting a
        library's __str__.
        """
        url = self.database_url
        hosts = url.hosts()
        if not hosts:
            return f"{url.scheme}://"
        first = hosts[0]
        username = first.get("username")
        hostname = first.get("host") or ""
        port = first.get("port")

        userinfo = f"{username}@" if username else ""
        netloc = f"{hostname}:{port}" if port else hostname
        return f"{url.scheme}://{userinfo}{netloc}{url.path or ''}"


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Process-wide settings singleton.

    Cached so configuration is read once. Tests that change the environment must
    call ``get_settings.cache_clear()``.
    """
    return Settings()  # values come from the environment
