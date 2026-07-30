"""The conversation engine's pure parts.

State machine, place resolution, time windows, materiality and attribution. All
pure, all fast, all exhaustively coverable — which is the point of keeping the
decision logic separate from the I/O that acts on it.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

import pytest

from mawjood.core.aggregators.base import MerchantRef, Slot
from mawjood.core.attribution import (
    build_wa_link,
    medium_for,
    parse_source_code,
    strip_source_code,
)
from mawjood.core.conversation.consent import ConsentSignal, ConsentState
from mawjood.core.conversation.phrasebank import PhraseKey
from mawjood.core.conversation.places import Resolution, find_in_sentence, resolve
from mawjood.core.conversation.states import (
    CollectedSlots,
    ConversationState,
    MachineInput,
    TurnAction,
    Understanding,
    advance,
)
from mawjood.core.enums import AttributionMedium
from mawjood.core.routing.materiality import Tolerances, compare
from mawjood.core.routing.when import resolve_window
from mawjood.services.llm import DeterministicProvider

TZ = ZoneInfo("Asia/Dubai")


# ---------------------------------------------------------------------------
# Places
# ---------------------------------------------------------------------------


class TestPlaces:
    @pytest.mark.parametrize(
        ("said", "expected"),
        [
            ("Marina", "Dubai Marina"),
            ("the marina", "Dubai Marina"),
            ("JLT", "JLT"),
            ("jumeirah lakes towers", "JLT"),
            ("JBR", "JBR"),
            ("downtown", "Downtown Dubai"),
            ("tecom", "Barsha Heights"),
            ("difc", "DIFC"),
            ("the palm", "Palm Jumeirah"),
            ("yas", "Yas Island"),
        ],
    )
    def test_local_shorthand_resolves(self, said: str, expected: str) -> None:
        match = resolve(said)
        assert match.resolution is Resolution.RESOLVED
        assert match.place is not None
        assert match.place.canonical == expected

    @pytest.mark.parametrize("said", ["Al Nahda", "al qusais", "corniche"])
    def test_cross_emirate_names_are_ambiguous(self, said: str) -> None:
        """Booking the wrong emirate is a forty-minute drive, not a rounding
        error. Ask rather than guess."""
        match = resolve(said)
        assert match.resolution is Resolution.AMBIGUOUS
        assert len(match.candidates) >= 2

    def test_a_disambiguated_name_resolves(self) -> None:
        match = resolve("al nahda dubai")
        assert match.resolution is Resolution.RESOLVED
        assert match.place is not None
        assert match.place.canonical == "Al Nahda, Dubai"

    def test_a_place_is_found_inside_a_sentence(self) -> None:
        match = find_in_sentence("need a haircut in Dubai Marina tomorrow evening")
        assert match.resolution is Resolution.RESOLVED
        assert match.place is not None
        assert match.place.canonical == "Dubai Marina"

    def test_the_longest_alias_wins(self) -> None:
        """'al nahda dubai' must not be shadowed by the ambiguous 'al nahda'."""
        match = find_in_sentence("somewhere in al nahda dubai please")
        assert match.resolution is Resolution.RESOLVED
        assert match.place is not None
        assert match.place.canonical == "Al Nahda, Dubai"

    def test_an_unknown_place_is_unknown_not_a_guess(self) -> None:
        assert find_in_sentence("somewhere in Narnia").resolution is Resolution.UNKNOWN


# ---------------------------------------------------------------------------
# Time windows
# ---------------------------------------------------------------------------


class TestWindows:
    def test_tomorrow_evening_is_local_evening(self) -> None:
        now = datetime(2026, 8, 1, 10, 0, tzinfo=UTC)
        start, end = resolve_window("tomorrow", "evening", tz=TZ, now=now)
        assert start.astimezone(TZ).hour == 17
        assert end.astimezone(TZ).hour == 22
        assert start.astimezone(TZ).date() == datetime(2026, 8, 2, tzinfo=TZ).date()

    def test_a_clock_time_gets_a_window_either_side(self) -> None:
        """ "6pm" should find 5:30 and 6:30 too — a window too narrow finds
        nothing and sends someone to a human for no reason."""
        now = datetime(2026, 8, 1, 6, 0, tzinfo=UTC)
        start, end = resolve_window("tomorrow", "18:00", tz=TZ, now=now)
        assert start.astimezone(TZ).hour == 16
        assert end.astimezone(TZ).hour == 19

    def test_a_weekday_means_the_next_one(self) -> None:
        # 2026-08-01 is a Saturday.
        now = datetime(2026, 8, 1, 10, 0, tzinfo=UTC)
        start, _ = resolve_window("thursday", None, tz=TZ, now=now)
        assert start.astimezone(TZ).strftime("%A") == "Thursday"

    def test_the_same_weekday_means_next_week_not_the_past(self) -> None:
        now = datetime(2026, 8, 1, 10, 0, tzinfo=UTC)  # Saturday
        start, _ = resolve_window("saturday", None, tz=TZ, now=now)
        assert start.astimezone(TZ).date() > now.astimezone(TZ).date()

    def test_today_never_proposes_a_time_already_past(self) -> None:
        now = datetime(2026, 8, 1, 16, 0, tzinfo=UTC)  # 20:00 Dubai
        start, _ = resolve_window("today", None, tz=TZ, now=now)
        assert start >= now


# ---------------------------------------------------------------------------
# The state machine
# ---------------------------------------------------------------------------


def machine(**kwargs: object) -> MachineInput:
    base: dict[str, object] = {
        "state": ConversationState.SLOT_FILLING,
        "collected": CollectedSlots(),
        "understanding": Understanding(),
        "consent_state": ConsentState.GRANTED,
        "consent_signal": ConsentSignal.NONE,
        "is_new_lead": False,
    }
    base.update(kwargs)
    return MachineInput(**base)  # type: ignore[arg-type]


class TestNothingBooksWithoutConfirmation:
    """The consent boundary that matters most."""

    def test_booking_requires_a_pending_offer(self) -> None:
        transition = advance(
            machine(
                state=ConversationState.CONFIRMATION,
                understanding=Understanding(intent="confirm", confirmation="yes"),
                pending_offer=None,
            )
        )
        assert transition.action is not TurnAction.BOOK

    def test_booking_requires_an_explicit_yes(self) -> None:
        for understanding in (
            Understanding(intent="unclear"),
            Understanding(intent="provide_detail", date_text="thursday"),
            Understanding(intent="confirm", confirmation="no"),
        ):
            transition = advance(
                machine(
                    state=ConversationState.CONFIRMATION,
                    understanding=understanding,
                    pending_offer={"venue_name": "X"},
                )
            )
            assert transition.action is not TurnAction.BOOK, understanding

    def test_silence_is_not_consent(self) -> None:
        transition = advance(
            machine(
                state=ConversationState.CONFIRMATION,
                understanding=Understanding(intent="unclear"),
                pending_offer={"venue_name": "X"},
            )
        )
        assert transition.action is TurnAction.NONE
        assert transition.phrases[0].key == PhraseKey.CONFIRM_REQUEST

    def test_booking_requires_recorded_consent(self) -> None:
        """Decision 4: a booking sends a name and number to a venue."""
        transition = advance(
            machine(
                state=ConversationState.CONFIRMATION,
                consent_state=ConsentState.NOTICE_SHOWN,
                understanding=Understanding(intent="confirm", confirmation="yes"),
                pending_offer={"venue_name": "X"},
            )
        )
        assert transition.action is not TurnAction.BOOK
        assert transition.phrases[0].key == PhraseKey.CONSENT_NOTICE

    def test_an_explicit_yes_with_consent_books(self) -> None:
        transition = advance(
            machine(
                state=ConversationState.CONFIRMATION,
                consent_state=ConsentState.GRANTED,
                understanding=Understanding(intent="confirm", confirmation="yes"),
                pending_offer={"venue_name": "X"},
            )
        )
        assert transition.action is TurnAction.BOOK
        assert transition.state is ConversationState.BOOKING


class TestOverrides:
    def test_asking_for_a_human_wins_from_any_state(self) -> None:
        for state in ConversationState:
            transition = advance(
                machine(state=state, understanding=Understanding(wants_human=True))
            )
            assert transition.action is TurnAction.HANDOFF, state

    def test_a_withdrawal_clears_everything(self) -> None:
        transition = advance(
            machine(
                state=ConversationState.CONFIRMATION,
                collected=CollectedSlots(service="haircut", area="Dubai Marina"),
                consent_signal=ConsentSignal.WITHDRAWAL,
                pending_offer={"venue_name": "X"},
            )
        )
        assert transition.collected.service is None
        assert transition.clear_pending_offer is True

    def test_arabic_is_answered_in_english_without_apology(self) -> None:
        transition = advance(machine(understanding=Understanding(language="ar")))
        assert transition.phrases[0].key == PhraseKey.LANGUAGE_ENGLISH_ONLY
        assert transition.action is TurnAction.NONE


class TestSlotFilling:
    def test_one_question_at_a_time(self) -> None:
        transition = advance(machine(collected=CollectedSlots(service="haircut")))
        assert len(transition.phrases) == 1
        assert transition.phrases[0].key == PhraseKey.ASK_AREA

    def test_a_complete_request_searches(self) -> None:
        transition = advance(
            machine(
                collected=CollectedSlots(
                    service="haircut", category="salon", area="Dubai Marina", date_text="tomorrow"
                )
            )
        )
        assert transition.action is TurnAction.SEARCH

    def test_a_correction_overwrites_rather_than_appends(self) -> None:
        transition = advance(
            machine(
                collected=CollectedSlots(
                    service="haircut", category="salon", area="Dubai Marina", date_text="tomorrow"
                ),
                understanding=Understanding(date_text="thursday", is_correction=True),
            )
        )
        assert transition.collected.date_text == "thursday"


# ---------------------------------------------------------------------------
# Materiality
# ---------------------------------------------------------------------------


def slot(*, venue: str = "Marina Beauty Lounge", hour: int = 18, price: float = 120.0) -> Slot:
    start = datetime(2026, 8, 2, hour, 0, tzinfo=UTC)
    return Slot(
        slot_id="s",
        start=start,
        end=start + timedelta(minutes=45),
        venue_name=venue,
        service_name="haircut",
        merchant=MerchantRef(
            platform_slug="fake_happy",
            merchant_id=__import__("uuid").uuid4(),
            display_name=venue,
        ),
        price_amount=price,
        price_currency="AED",
    )


class TestMateriality:
    def test_an_identical_replacement_is_not_material(self) -> None:
        assert compare(slot(), slot()).material is False

    def test_a_different_venue_always_needs_asking(self) -> None:
        verdict = compare(slot(), slot(venue="Some Other Salon"))
        assert verdict.needs_reconfirmation
        assert any("venue" in reason for reason in verdict.reasons)

    def test_a_small_time_shift_is_tolerated(self) -> None:
        assert compare(slot(hour=18), slot(hour=18)).material is False

    def test_a_large_time_shift_needs_asking(self) -> None:
        verdict = compare(slot(hour=18), slot(hour=21))
        assert verdict.needs_reconfirmation

    def test_a_price_rise_beyond_tolerance_needs_asking(self) -> None:
        verdict = compare(slot(price=100), slot(price=150))
        assert verdict.needs_reconfirmation

    def test_a_cheaper_slot_needs_no_permission(self) -> None:
        assert compare(slot(price=150), slot(price=100)).material is False

    def test_tolerances_are_configurable_not_hardcoded(self) -> None:
        tight = Tolerances(start_minutes=5)
        assert compare(slot(hour=18), slot(hour=19), tight).material is True


# ---------------------------------------------------------------------------
# Attribution
# ---------------------------------------------------------------------------


class TestAttribution:
    @pytest.mark.parametrize(
        ("text", "expected"),
        [
            ("SRC12", "SRC12"),
            ("src12", "SRC12"),
            ("SRC12 need a haircut", "SRC12"),
            ("hello SRCMARINA01 please", "SRCMARINA01"),
            ("no code here", None),
            ("", None),
        ],
    )
    def test_source_codes_are_parsed(self, text: str, expected: str | None) -> None:
        assert parse_source_code(text) == expected

    def test_the_prefill_is_stripped_before_the_engine_sees_it(self) -> None:
        assert strip_source_code("SRC12 need a haircut") == "need a haircut"
        assert strip_source_code("SRC12") == ""

    def test_a_bare_code_reads_as_a_qr_scan(self) -> None:
        """Nobody types SRC12 unprompted."""
        assert medium_for("SRC12") is AttributionMedium.QR
        assert medium_for("SRC12 need a haircut") is AttributionMedium.WA_LINK

    def test_the_click_to_chat_link_is_well_formed(self) -> None:
        assert build_wa_link("+971 50 123 4567", "SRC12") == "https://wa.me/971501234567?text=SRC12"

    async def test_a_prefilled_message_still_books(self) -> None:
        """A QR scan must not derail understanding."""
        provider = DeterministicProvider()
        raw = "SRC12 need a haircut in Marina tomorrow evening"
        reading = await provider.extract(
            system="",
            history=[
                __import__("mawjood.services.llm", fromlist=["Turn"]).Turn(
                    role="user", content=strip_source_code(raw)
                )
            ],
            schema={},
        )
        assert reading["service"] == "haircut"
        assert reading["area"] == "Dubai Marina"


# ---------------------------------------------------------------------------
# The deterministic understudy
# ---------------------------------------------------------------------------


class TestDeterministicProvider:
    @pytest.mark.parametrize(
        ("message", "field", "expected"),
        [
            ("need a haircut", "service", "haircut"),
            ("book me a massage", "service", "massage"),
            ("table for 4", "service", "table"),
            ("need a haircut", "category", "salon"),
            ("book me a massage", "category", "spa"),
            ("tomorrow evening", "date_text", "tomorrow"),
            ("tomorrow evening", "time_text", "evening"),
            ("at 6pm", "time_text", "18:00"),
            ("in Marina", "area", "Dubai Marina"),
        ],
    )
    def test_extraction(self, message: str, field: str, expected: str) -> None:
        assert DeterministicProvider().read(message)[field] == expected

    def test_a_bare_yes_is_a_confirmation(self) -> None:
        assert DeterministicProvider().read("yes")["confirmation"] == "yes"

    def test_a_yes_carrying_a_request_is_not_a_bare_confirmation(self) -> None:
        """Booking on a misread yes is the worst available mistake."""
        reading = DeterministicProvider().read("yes but make it Thursday")
        assert reading.get("is_correction") is True
        assert reading.get("date_text") == "thursday"

    def test_it_is_deterministic(self) -> None:
        provider = DeterministicProvider()
        message = "need a haircut in Marina tomorrow evening"
        assert provider.read(message) == provider.read(message)
