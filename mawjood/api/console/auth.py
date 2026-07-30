"""Console session authentication.

A signed, expiring cookie. No database table, no session store, no dependency:
the cookie *is* the session, and its integrity comes from an HMAC over the
payload keyed with the console secret.

That is a real tradeoff and worth stating. A stateless session cannot be revoked
before it expires — logging out clears the cookie in the browser, but a copied
cookie stays valid until its timestamp runs out. For an internal tool with one
operator credential, behind whatever ingress the deployment puts in front of it,
a short lifetime is the right answer and a session table is scope nobody asked
for. If per-user revocation is ever needed, that is the moment to add the table.

Two properties this gets right, both easy to get wrong:

* **The signature covers the expiry.** Signing only the username would let anyone
  with a valid cookie extend it forever by editing the timestamp.
* **Comparison is constant-time**, and both halves of the login are compared even
  when the first fails, so a wrong username and a wrong password take the same
  time.

Login is the one POST the console has. It mutates no domain data — it sets a
cookie — so the read-only guarantee is unaffected, and the AST test that enforces
it treats this module explicitly rather than by accident.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import secrets
import time
from dataclasses import dataclass

from fastapi import Request

COOKIE_NAME = "mawjood_console"
# Short by design: a stateless session cannot be revoked, so it should not live
# long. Eight hours is one shift.
DEFAULT_TTL_SECONDS = 8 * 60 * 60


@dataclass(frozen=True, slots=True)
class Session:
    username: str
    expires_at: int

    @property
    def expired(self) -> bool:
        return time.time() >= self.expires_at


def _sign(payload: str, secret: str) -> str:
    digest = hmac.new(secret.encode(), payload.encode(), hashlib.sha256).digest()
    return base64.urlsafe_b64encode(digest).decode().rstrip("=")


def issue(username: str, *, secret: str, ttl_seconds: int = DEFAULT_TTL_SECONDS) -> str:
    """Mint a cookie value for a successful login."""
    expires_at = int(time.time()) + ttl_seconds
    payload = f"{username}:{expires_at}"
    return f"{payload}:{_sign(payload, secret)}"


def verify(cookie: str | None, *, secret: str) -> Session | None:
    """Validate a cookie. ``None`` for anything that is not a live session.

    Every failure returns None rather than raising or distinguishing: a caller
    that could tell "expired" from "forged" would leak that distinction to
    whoever sent the cookie.
    """
    if not cookie:
        return None
    parts = cookie.rsplit(":", 2)
    if len(parts) != 3:
        return None
    username, raw_expiry, signature = parts

    expected = _sign(f"{username}:{raw_expiry}", secret)
    if not hmac.compare_digest(expected, signature):
        return None

    try:
        expires_at = int(raw_expiry)
    except ValueError:
        return None

    session = Session(username=username, expires_at=expires_at)
    return None if session.expired else session


def check_credentials(username: str, password: str, *, settings: object) -> bool:
    """Constant-time credential check.

    Both halves are always compared. Short-circuiting on the username would make
    a wrong username measurably faster than a wrong password, which tells an
    attacker when they have found a real account.
    """
    expected_user = str(getattr(settings, "console_username", ""))
    secret = getattr(settings, "console_password", None)
    if secret is None:
        return False
    expected_password = secret.get_secret_value()

    user_ok = secrets.compare_digest(username, expected_user)
    password_ok = secrets.compare_digest(password, expected_password)
    return user_ok and password_ok


def session_secret(settings: object) -> str | None:
    """The key cookies are signed with.

    Derived from the console password rather than configured separately, so
    there is one secret to manage and rotating the password invalidates every
    outstanding session — which is the behaviour an operator expects from
    changing a password anyway.
    """
    secret = getattr(settings, "console_password", None)
    if secret is None:
        return None
    return hashlib.sha256(
        b"mawjood.console.session/" + secret.get_secret_value().encode()
    ).hexdigest()


def current_session(request: Request) -> Session | None:
    secret = session_secret(request.app.state.settings)
    if secret is None:
        return None
    return verify(request.cookies.get(COOKIE_NAME), secret=secret)


__all__ = [
    "COOKIE_NAME",
    "DEFAULT_TTL_SECONDS",
    "Session",
    "check_credentials",
    "current_session",
    "issue",
    "session_secret",
    "verify",
]
