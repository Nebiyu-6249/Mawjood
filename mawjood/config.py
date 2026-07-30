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

from pydantic import Field, PostgresDsn, SecretStr, field_validator, model_validator
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
    # Tags Sentry events so an alert can name the build it came from.
    service_version: str = "0.1.0"
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

    # --- Tenancy ------------------------------------------------------------
    # v1 runs one operator. The slug is configuration rather than a constant so
    # multi-tenant routing is a resolver change, not a schema change.
    default_tenant_slug: str = "mawjood"
    default_tenant_name: str = "Mawjood"

    # --- Conversation engine (LLM) -----------------------------------------
    # Provider-agnostic by decision 6. "deterministic" is a rule-based understudy
    # that needs no credentials — it is what lets chat_sim and the whole test
    # suite run for free, and it is the default until a key is provisioned.
    llm_provider: Literal["openai", "deterministic"] = "deterministic"
    llm_model: str = "gpt-4o-mini"
    openai_api_key: SecretStr | None = None
    llm_timeout_ms: int = Field(default=2000, gt=0)

    # Registers the fake adapters so a local demo can complete a booking while
    # no live adapter exists. Refused outright in production by
    # aggregators/registry.py — a fake in production confirms bookings that do
    # not exist.
    enable_fake_adapters: bool = True

    # --- Scheduled messages ---------------------------------------------------
    # Offsets relative to the booking, in hours. Two reminders, because 24h out
    # and 2h out answer different questions: one lets a consumer move their day
    # around the booking, the other tells them to leave.
    notify_reminder_24h_before: float = Field(default=24.0, ge=0)
    notify_reminder_2h_before: float = Field(default=2.0, ge=0)
    notify_follow_up_after: float = Field(default=2.0, ge=0)
    notify_satisfaction_after: float = Field(default=24.0, ge=0)
    # How long a notification stays sendable after its moment passes. A
    # scheduler down for an hour should still send; one down for a day must not.
    notify_grace_hours: float = Field(default=2.0, ge=0)
    # WhatsApp's customer-service window. Outside it only an approved template
    # may be sent. Configurable because it is a provider policy, not a law of
    # nature — see mawjood/core/notifications/.
    whatsapp_session_window_hours: float = Field(default=24.0, gt=0)

    # --- WhatsApp templates ---------------------------------------------------
    # Names are configuration, never constants. A template's registered name is
    # chosen by whoever submits it, in a portal, on a day nobody will remember —
    # and if a resubmission has to take a new name, that must not be a deploy.
    template_name_reminder_24h: str = "booking_reminder_24h"
    template_name_reminder_2h: str = "booking_reminder_2h"
    template_name_follow_up: str = "booking_follow_up"
    template_name_satisfaction: str = "booking_satisfaction"
    # The names Meta has actually approved. A name here flips its template to
    # approved and opens the out-of-window send path with no code change.
    # Empty today: template approval is an operational dependency and nothing has
    # been submitted. Until then, scheduled messages degrade to session messages
    # inside the 24-hour window and defer outside it.
    approved_templates: tuple[str, ...] = ()

    # --- Ops console ----------------------------------------------------------
    # HTTP Basic. The console refuses to serve at all when no password is set —
    # an unauthenticated console exposes every consumer's transcript.
    console_username: str = "ops"
    console_password: SecretStr | None = None

    # --- Secrets --------------------------------------------------------------
    secrets_backend: Literal["env", "static"] = "env"

    # --- Messaging provider (BSP) ------------------------------------------
    bsp_provider: Literal["360dialog", "wati", "console"] = "360dialog"
    # HMAC key for inbound webhook signatures.
    bsp_webhook_secret: SecretStr | None = None
    # Shared token for the provider's subscription handshake.
    bsp_verify_token: SecretStr | None = None
    # Refusing unsigned webhooks is the default. Turning this off is a local
    # development affordance and is rejected outright in production below.
    bsp_require_signature: bool = True
    bsp_api_base: str | None = None
    bsp_api_key: SecretStr | None = None

    # --- Startup behaviour --------------------------------------------------
    # docker-compose sets this so one command gives a working stack. Deployments
    # run migrations as a deliberate step, so it stays off by default.
    run_migrations_on_start: bool = False
    seed_dev_tenant_on_start: bool = False

    # --- Observability ------------------------------------------------------
    log_level: Literal["DEBUG", "INFO", "WARNING", "ERROR"] = "INFO"
    log_format: Literal["json", "console"] = "json"
    sentry_dsn: SecretStr | None = None
    sentry_traces_sample_rate: float = Field(default=0.1, ge=0.0, le=1.0)

    # --- Retention ----------------------------------------------------------
    log_retention_days: int = Field(default=90, ge=1)
    data_retention_days: int = Field(default=365, ge=1)

    @field_validator(
        "sentry_dsn",
        "bsp_webhook_secret",
        "bsp_verify_token",
        "bsp_api_key",
        "bsp_api_base",
        "openai_api_key",
        "console_password",
        mode="before",
    )
    @classmethod
    def _empty_string_is_unset(cls, value: object) -> object:
        """Treat an empty env var as absent.

        ``.env.example`` ships these keys blank and compose passes them through as
        "". Without this they become SecretStr("") rather than None, and every
        "is it configured?" check silently succeeds on an empty credential.
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

    @model_validator(mode="after")
    def _production_demands_signed_webhooks(self) -> Settings:
        """An unsigned webhook endpoint in production accepts spoofed messages.

        Disabling verification is a local convenience for poking the endpoint
        with curl. Allowing it to reach production by way of a stray environment
        variable is how a booking system ends up taking instructions from anyone
        who can find the URL.
        """
        if self.environment is Environment.PROD:
            if not self.bsp_require_signature:
                raise ValueError(
                    "bsp_require_signature cannot be disabled in production: "
                    "unsigned webhooks accept spoofed consumer messages"
                )
            if self.bsp_provider != "console" and self.bsp_webhook_secret is None:
                raise ValueError(
                    "bsp_webhook_secret is required in production so inbound "
                    "webhooks can be verified"
                )
            if self.console_password is None:
                raise ValueError(
                    "console_password is required in production: the ops console "
                    "exposes every consumer's transcript"
                )
        return self

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
