"""Secret resolution.

Decision 5: an env backend behind an interface, with a documented seam for a
managed store. Region and provider are configuration, so swapping the backend is
one class and no caller changes.

Decision 1: aggregator credentials are **per merchant**, not per platform. Zenoti
and its peers are merchant-side — each salon has its own centre ids and key — so
the store is keyed by a ``secret_ref`` held on the merchant row, and the router
resolves it just-in-time and hands the values to the adapter in ``CallContext``.

Adapters never touch this module. That keeps least privilege real and makes every
adapter testable without a secrets backend.
"""

from __future__ import annotations

import json
import os
from collections.abc import Mapping
from typing import Protocol, runtime_checkable

from mawjood.observability.logging import get_logger

log = get_logger(__name__)

# Environment variables holding merchant credentials use this prefix, so a secret
# scan and an ops review can both find them by pattern.
ENV_PREFIX = "MAWJOOD_MERCHANT_"


class SecretNotFound(LookupError):
    """A referenced secret does not exist.

    Raised loudly: a missing credential is a configuration fault. The router turns
    it into an advance and a pivot, so the consumer never learns of it.
    """


@runtime_checkable
class SecretStore(Protocol):
    """Where credentials come from."""

    name: str

    def get(self, ref: str) -> Mapping[str, str]:
        """Resolve a reference into credential values."""
        ...


class EnvSecretStore:
    """Credentials from the environment. The v1 backend.

    A ``secret_ref`` of ``zenoti_marina_01`` reads
    ``MAWJOOD_MERCHANT_ZENOTI_MARINA_01``, whose value is a JSON object of
    credential fields. JSON rather than one variable per field because a merchant
    needs several values together (key, centre id, org id) and splitting them
    across variables invites a half-configured merchant.
    """

    name = "env"

    def __init__(self, environ: Mapping[str, str] | None = None) -> None:
        self._environ = environ if environ is not None else os.environ

    def _key(self, ref: str) -> str:
        return f"{ENV_PREFIX}{ref.upper().replace('-', '_')}"

    def get(self, ref: str) -> Mapping[str, str]:
        raw = self._environ.get(self._key(ref))
        if raw is None:
            raise SecretNotFound(
                f"no secret for ref {ref!r}; expected environment variable {self._key(ref)}"
            )
        try:
            parsed = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise SecretNotFound(
                f"secret {self._key(ref)} is not valid JSON; expected an object of "
                "credential fields"
            ) from exc
        if not isinstance(parsed, dict):
            raise SecretNotFound(f"secret {self._key(ref)} must be a JSON object")
        return {str(k): str(v) for k, v in parsed.items()}


class StaticSecretStore:
    """In-memory credentials. Tests and the credential-free local demo."""

    name = "static"

    def __init__(self, secrets: Mapping[str, Mapping[str, str]] | None = None) -> None:
        self._secrets = dict(secrets or {})

    def put(self, ref: str, values: Mapping[str, str]) -> None:
        self._secrets[ref] = dict(values)

    def get(self, ref: str) -> Mapping[str, str]:
        if ref not in self._secrets:
            raise SecretNotFound(f"no secret for ref {ref!r}")
        return self._secrets[ref]


class CredentialResolver:
    """Resolves a merchant's ``secret_ref`` at call time.

    Nothing here is cached beyond the process and nothing is persisted. Values are
    never logged — the logging redaction processor covers the accident, and this
    class simply does not log them.
    """

    def __init__(self, store: SecretStore) -> None:
        self._store = store

    @property
    def backend(self) -> str:
        return self._store.name

    def resolve(self, secret_ref: str | None) -> Mapping[str, str]:
        """Return credentials, or an empty mapping when a merchant needs none.

        An unresolvable ref is logged as an operational fault and returns empty
        rather than raising: the adapter will report AUTH_ERROR, the router will
        advance, and the consumer sees a pivot. A raise here would take the whole
        turn down over one misconfigured merchant.
        """
        if not secret_ref:
            return {}
        try:
            return self._store.get(secret_ref)
        except SecretNotFound:
            log.error("credentials.unresolved", secret_ref=secret_ref, backend=self.backend)
            return {}


def build_secret_store(settings: object) -> SecretStore:
    """Pick a backend.

    The seam for a managed store: add a branch here and a class above. Callers,
    adapters and the router are unaffected — which is what makes the Phase 4
    cloud decision a configuration change rather than a refactor.
    """
    backend = getattr(settings, "secrets_backend", "env")
    if backend == "static":
        return StaticSecretStore()
    if backend != "env":
        log.warning(
            "secrets.backend_not_implemented",
            requested=backend,
            note="falling back to env; add the implementation in services/secrets.py",
        )
    return EnvSecretStore()


__all__ = [
    "ENV_PREFIX",
    "CredentialResolver",
    "EnvSecretStore",
    "SecretNotFound",
    "SecretStore",
    "StaticSecretStore",
    "build_secret_store",
]
