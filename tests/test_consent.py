"""Consent classification.

Decision 4: notice, then opt-in before any PII leaves Mawjood. Getting the
classifier wrong in the permissive direction records consent a consumer never
gave, so the affirmative set is deliberately narrow and matches whole messages
only.
"""

from __future__ import annotations

import pytest

from mawjood.core.conversation.consent import (
    ConsentSignal,
    ConsentState,
    classify,
    may_share_pii,
    needs_notice,
)


class TestAffirmative:
    @pytest.mark.parametrize(
        "text",
        [
            "yes",
            "Yes",
            "YES",
            "yes.",
            "yes!",
            " yes ",
            "ok",
            "okay",
            "sure",
            "agreed",
            "confirm",
            "go ahead",
            "👍",
            "yep",
            "fine",
        ],
    )
    def test_plain_agreement_is_recognised(self, text: str) -> None:
        assert classify(text) is ConsentSignal.AFFIRMATIVE

    @pytest.mark.parametrize(
        "text",
        [
            "yes, I need a table at Zuma tomorrow",
            "ok so what about a haircut",
            "sure thing, book me for 6pm at the Marina place",
            "yes please book it",
        ],
    )
    def test_agreement_plus_a_request_is_not_bare_consent(self, text: str) -> None:
        """A booking request that happens to open with "yes" is a request.

        Treating it as consent records agreement to a notice the consumer may
        never have read.
        """
        assert classify(text) is ConsentSignal.NONE


class TestWithdrawal:
    @pytest.mark.parametrize(
        "text",
        [
            "stop",
            "STOP",
            "Stop.",
            "unsubscribe",
            "opt out",
            "delete my data",
            "forget me",
            "remove me",
            "withdraw consent",
        ],
    )
    def test_withdrawal_is_recognised(self, text: str) -> None:
        assert classify(text) is ConsentSignal.WITHDRAWAL

    def test_stop_inside_a_sentence_is_not_a_withdrawal(self) -> None:
        assert classify("does the salon stop taking bookings at 8?") is ConsentSignal.NONE


class TestNeutral:
    @pytest.mark.parametrize(
        "text",
        ["", "   ", "need a haircut in Marina", "what time do you open?", "hi"],
    )
    def test_ordinary_messages_carry_no_signal(self, text: str) -> None:
        assert classify(text) is ConsentSignal.NONE


class TestTheGate:
    """Nothing personal reaches an aggregator without a recorded grant."""

    def test_only_granted_permits_pii_egress(self) -> None:
        assert may_share_pii(ConsentState.GRANTED) is True

    @pytest.mark.parametrize(
        "state",
        [ConsentState.UNKNOWN, ConsentState.NOTICE_SHOWN, ConsentState.WITHDRAWN],
    )
    def test_every_other_state_blocks_pii_egress(self, state: ConsentState) -> None:
        assert may_share_pii(state) is False

    def test_showing_the_notice_does_not_grant_consent(self) -> None:
        """The notice is displayed and Mawjood keeps helping, but that is not
        agreement and must not unlock PII egress."""
        assert may_share_pii(ConsentState.NOTICE_SHOWN) is False

    def test_notice_is_needed_only_when_never_shown(self) -> None:
        assert needs_notice(ConsentState.UNKNOWN) is True
        assert needs_notice(ConsentState.NOTICE_SHOWN) is False
        assert needs_notice(ConsentState.GRANTED) is False
