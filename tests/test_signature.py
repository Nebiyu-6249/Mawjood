"""Webhook signature verification.

An unsigned or badly-verified webhook endpoint means anyone who finds the URL can
put words in a consumer's mouth and drive bookings. The two failure modes that
matter are failing open (accepting when no secret is set) and verifying against a
re-serialised body instead of the bytes that were signed.
"""

from __future__ import annotations

import json

import pytest

from mawjood.services.bsp.base import (
    SignatureResult,
    SignatureScheme,
    sign_payload,
    verify_signature,
)

SECRET = "a-test-app-secret"
SCHEME = SignatureScheme()


def _headers(body: bytes, secret: str = SECRET) -> dict[str, str]:
    return {"x-hub-signature-256": sign_payload(scheme=SCHEME, secret=secret, raw_body=body)}


class TestValidSignatures:
    def test_a_correctly_signed_body_is_accepted(self) -> None:
        body = b'{"entry":[]}'
        assert (
            verify_signature(scheme=SCHEME, secret=SECRET, raw_body=body, headers=_headers(body))
            is SignatureResult.VALID
        )

    def test_header_lookup_is_case_insensitive(self) -> None:
        body = b'{"entry":[]}'
        signature = sign_payload(scheme=SCHEME, secret=SECRET, raw_body=body)
        assert (
            verify_signature(
                scheme=SCHEME,
                secret=SECRET,
                raw_body=body,
                headers={"X-Hub-Signature-256": signature},
            )
            is SignatureResult.VALID
        )

    def test_uppercase_hex_digest_is_accepted(self) -> None:
        body = b'{"entry":[]}'
        signature = sign_payload(scheme=SCHEME, secret=SECRET, raw_body=body).upper()
        signature = signature.replace("SHA256=", "sha256=")
        assert (
            verify_signature(
                scheme=SCHEME,
                secret=SECRET,
                raw_body=body,
                headers={"x-hub-signature-256": signature},
            )
            is SignatureResult.VALID
        )


class TestRejection:
    def test_a_tampered_body_is_rejected(self) -> None:
        original = b'{"amount":10}'
        headers = _headers(original)
        tampered = b'{"amount":10000}'
        assert (
            verify_signature(scheme=SCHEME, secret=SECRET, raw_body=tampered, headers=headers)
            is SignatureResult.MISMATCH
        )

    def test_a_signature_from_a_different_secret_is_rejected(self) -> None:
        body = b'{"entry":[]}'
        assert (
            verify_signature(
                scheme=SCHEME,
                secret=SECRET,
                raw_body=body,
                headers=_headers(body, secret="attacker-secret"),
            )
            is SignatureResult.MISMATCH
        )

    def test_a_missing_header_is_rejected(self) -> None:
        assert (
            verify_signature(scheme=SCHEME, secret=SECRET, raw_body=b"{}", headers={})
            is SignatureResult.MISSING_HEADER
        )

    @pytest.mark.parametrize("value", ["deadbeef", "md5=deadbeef", "", "sha256="])
    def test_malformed_headers_are_rejected(self, value: str) -> None:
        result = verify_signature(
            scheme=SCHEME,
            secret=SECRET,
            raw_body=b"{}",
            headers={"x-hub-signature-256": value},
        )
        assert result is not SignatureResult.VALID
        assert not result.ok


class TestFailingClosed:
    """A webhook verifier that fails open is worse than none: it looks secure."""

    @pytest.mark.parametrize("secret", [None, ""])
    def test_an_unset_secret_never_passes(self, secret: str | None) -> None:
        body = b'{"entry":[]}'
        result = verify_signature(
            scheme=SCHEME, secret=secret, raw_body=body, headers=_headers(body)
        )
        assert result is SignatureResult.NOT_CONFIGURED
        assert not result.ok

    def test_only_valid_and_skipped_are_ok(self) -> None:
        ok = {result for result in SignatureResult if result.ok}
        assert ok == {SignatureResult.VALID, SignatureResult.SKIPPED}


class TestRawBodyDiscipline:
    def test_reserialising_the_body_breaks_the_signature(self) -> None:
        """Documents why the endpoint hashes request.body() and never a re-dump.

        A parse/re-dump round trip changes whitespace and key order, so a
        verifier built on it would reject every genuine webhook.
        """
        original = b'{"b": 1,  "a":   2}'
        headers = _headers(original)
        reserialised = json.dumps(json.loads(original)).encode()

        assert (
            verify_signature(scheme=SCHEME, secret=SECRET, raw_body=original, headers=headers)
            is SignatureResult.VALID
        )
        assert (
            verify_signature(scheme=SCHEME, secret=SECRET, raw_body=reserialised, headers=headers)
            is SignatureResult.MISMATCH
        )
