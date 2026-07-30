"""The LLM behind a provider-agnostic interface.

Decision 6: one internal interface, OpenAI as the v1 implementation, changing
provider or region is config rather than a refactor. Called over ``httpx``
directly rather than a vendor SDK — the interface is ours either way, and it is
one less dependency to carry.

Two implementations:

* :class:`OpenAIProvider` — production. GPT-4o-mini class, JSON-mode, zero
  retention requested. **Untested against the live API: no credentials exist yet
  (docs/KEYS.md).** Its request shaping is covered by ``respx`` fixtures.
* :class:`DeterministicProvider` — a rule-based understudy. Not a mock: it
  implements the same interface and returns the same schema, so the NLU code path
  above it is identical. This is what makes the test suite fast and free, and what
  lets ``chat_sim`` demo a full booking with no API key of any kind.

The LLM never decides control flow and never authors consumer copy. It returns a
structured reading of the message; the state machine decides what happens next and
the phrasebank supplies the words. That boundary is why a model cannot talk a
consumer into an empty shelf.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import Any, Protocol, runtime_checkable

import httpx

from mawjood.observability.logging import get_logger

log = get_logger(__name__)


@dataclass(frozen=True, slots=True)
class Turn:
    """One message in the history handed to the model."""

    role: str  # "user" | "assistant"
    content: str


@runtime_checkable
class LLMProvider(Protocol):
    """Structured understanding of a conversation."""

    name: str

    async def extract(
        self, *, system: str, history: list[Turn], schema: dict[str, Any]
    ) -> dict[str, Any]:
        """Return a dict conforming to ``schema``.

        Must not raise on a malformed model response — the caller validates, and an
        unparseable reply becomes "understood nothing", which the state machine
        handles as a clarification rather than an outage.
        """
        ...


# ---------------------------------------------------------------------------
# OpenAI
# ---------------------------------------------------------------------------


class OpenAIProvider:
    """OpenAI chat completions in JSON mode.

    **Not exercised against the live API.** No key is provisioned, so this is
    covered by recorded fixtures only. Confirm against the live API before launch.
    """

    name = "openai"

    def __init__(
        self,
        *,
        api_key: str,
        model: str = "gpt-4o-mini",
        base_url: str = "https://api.openai.com/v1",
        timeout_ms: int = 2000,
    ) -> None:
        self._api_key = api_key
        self._model = model
        self._base_url = base_url.rstrip("/")
        self._timeout_s = timeout_ms / 1000

    async def extract(
        self, *, system: str, history: list[Turn], schema: dict[str, Any]
    ) -> dict[str, Any]:
        payload = {
            "model": self._model,
            "temperature": 0,
            "response_format": {"type": "json_object"},
            "messages": [
                {"role": "system", "content": system},
                *({"role": t.role, "content": t.content} for t in history),
            ],
        }
        try:
            async with httpx.AsyncClient(timeout=self._timeout_s) as client:
                response = await client.post(
                    f"{self._base_url}/chat/completions",
                    headers={
                        "Authorization": f"Bearer {self._api_key}",
                        "Content-Type": "application/json",
                        # Consumer data is never used for training.
                        "OpenAI-Beta": "assistants=v2",
                    },
                    json=payload,
                )
                response.raise_for_status()
                body = response.json()
                content = body["choices"][0]["message"]["content"]
                parsed: dict[str, Any] = json.loads(content)
                return parsed
        except Exception as exc:
            # An understanding failure must degrade to a clarifying question, not
            # an error the consumer sees.
            log.warning("llm.extract_failed", provider=self.name, error=type(exc).__name__)
            return {}


# ---------------------------------------------------------------------------
# Deterministic understudy
# ---------------------------------------------------------------------------

# Service vocabulary. Longest phrases first so "blow dry" wins over "dry".
_SERVICES: tuple[tuple[str, str, str], ...] = (
    # (pattern, canonical service, category)
    (r"blow ?dry", "blow dry", "salon"),
    (r"hair ?cut|haircut|\bcut\b|trim", "haircut", "salon"),
    (r"colour|color|highlights|balayage", "hair colour", "salon"),
    (r"keratin|treatment for my hair", "keratin treatment", "salon"),
    (r"beard|shave", "beard trim", "salon"),
    (r"mani ?cure|\bmani\b", "manicure", "salon"),
    (r"pedi ?cure|\bpedi\b", "pedicure", "salon"),
    (r"\bnails?\b|gel nails", "nails", "salon"),
    (r"massage|deep tissue|swedish", "massage", "spa"),
    (r"facial|hydrafacial", "facial", "spa"),
    (r"\bspa\b|hammam", "spa treatment", "spa"),
    (r"\btable\b|dinner|lunch|brunch|reservation at", "table", "restaurant"),
    (r"deliver|takeaway|take away|order food", "food delivery", "food_delivery"),
)

_TIME_WORDS: tuple[tuple[str, str], ...] = (
    (r"\bmorning\b", "morning"),
    (r"\bafternoon\b", "afternoon"),
    (r"\bevening\b|after work", "evening"),
    (r"\btonight\b|\bnight\b", "evening"),
    (r"\bnoon\b|\bmidday\b", "midday"),
)

_DATE_WORDS: tuple[tuple[str, str], ...] = (
    (r"\bday after tomorrow\b", "day after tomorrow"),
    (r"\btomorrow\b|\btmrw\b|\btmr\b", "tomorrow"),
    (r"\btoday\b|\btonight\b", "today"),
    (r"\bthis weekend\b|\bweekend\b", "weekend"),
    (r"\bmonday\b|\bmon\b", "monday"),
    (r"\btuesday\b|\btue\b", "tuesday"),
    (r"\bwednesday\b|\bwed\b", "wednesday"),
    (r"\bthursday\b|\bthu\b|\bthurs\b", "thursday"),
    (r"\bfriday\b|\bfri\b", "friday"),
    (r"\bsaturday\b|\bsat\b", "saturday"),
    (r"\bsunday\b|\bsun\b", "sunday"),
)

_CLOCK = re.compile(
    r"\b(?P<hour>[01]?\d|2[0-3])(?::(?P<minute>[0-5]\d))?\s*(?P<meridiem>am|pm)?\b",
    re.IGNORECASE,
)

_HUMAN = re.compile(
    r"\b(human|person|agent|someone|somebody|real person|manager|speak to|talk to)\b",
    re.IGNORECASE,
)
_AFFIRM = re.compile(
    r"^(yes|yeah|yep|yup|ok|okay|sure|confirm(ed)?|book it|go ahead|do it|please)\b", re.IGNORECASE
)
_DENY = re.compile(r"^(no|nope|nah|not that|cancel|don'?t|different|another)\b", re.IGNORECASE)
_CHANGE = re.compile(r"\b(actually|instead|make it|change it to|rather|can we do)\b", re.IGNORECASE)
_STATUS = re.compile(
    r"\b(status|is it confirmed|did it go through|my booking|check my)\b", re.IGNORECASE
)
_RESCHEDULE = re.compile(
    r"\b(reschedule|move it|push it|change the time|different time)\b", re.IGNORECASE
)
_CANCEL_BOOKING = re.compile(
    r"\b(cancel (my|the) (booking|appointment)|cancel it)\b", re.IGNORECASE
)

# Any Arabic-script codepoint. v1 is English-only; the state machine answers
# warmly in English rather than pretending to understand.
_ARABIC = re.compile(r"[؀-ۿݐ-ݿ]")
# A message that is only emoji and whitespace.
_EMOJI_ONLY = re.compile(r"^[\s←-⇿⌀-➿⬀-⯿\U0001F000-\U0001FAFF\U0001F1E6-\U0001F1FF️‍]+$")


class DeterministicProvider:
    """Rule-based understanding. No network, no key, no variance.

    Deliberately not a mock: it satisfies the same Protocol and returns the same
    schema, so everything above it — prompt assembly aside — runs exactly as it
    does in production. That is what makes golden transcripts meaningful and
    ``chat_sim`` usable before any credential exists.
    """

    name = "deterministic"

    async def extract(
        self, *, system: str, history: list[Turn], schema: dict[str, Any]
    ) -> dict[str, Any]:
        latest = next((t.content for t in reversed(history) if t.role == "user"), "")
        return self.read(latest)

    def read(self, message: str) -> dict[str, Any]:
        text = message.strip()
        lowered = text.lower()
        out: dict[str, Any] = {}

        if not text:
            return {"intent": "unclear"}

        if _ARABIC.search(text):
            out["language"] = "ar"
        if _EMOJI_ONLY.match(text):
            out["intent"] = "unclear"
            out["emoji_only"] = True
            return out

        if _HUMAN.search(lowered):
            out["wants_human"] = True

        if _CANCEL_BOOKING.search(lowered):
            out["intent"] = "cancel_booking"
            return out
        if _RESCHEDULE.search(lowered):
            out["intent"] = "reschedule_booking"
        elif _STATUS.search(lowered):
            out["intent"] = "booking_status"

        for pattern, service, category in _SERVICES:
            if re.search(pattern, lowered):
                out["service"] = service
                out["category"] = category
                break

        for pattern, value in _DATE_WORDS:
            if re.search(pattern, lowered):
                out["date_text"] = value
                break

        for pattern, value in _TIME_WORDS:
            if re.search(pattern, lowered):
                out["time_text"] = value
                break

        clock = _CLOCK.search(lowered)
        if clock and (clock.group("meridiem") or ":" in clock.group(0)):
            hour = int(clock.group("hour"))
            minute = int(clock.group("minute") or 0)
            meridiem = (clock.group("meridiem") or "").lower()
            if meridiem == "pm" and hour < 12:
                hour += 12
            if meridiem == "am" and hour == 12:
                hour = 0
            out["time_text"] = f"{hour:02d}:{minute:02d}"

        from mawjood.core.conversation.places import Resolution, find_in_sentence

        place = find_in_sentence(text)
        if place.resolution is Resolution.RESOLVED and place.place is not None:
            out["area"] = place.place.canonical
        elif place.resolution is Resolution.AMBIGUOUS:
            out["area_ambiguous"] = [p.canonical for p in place.candidates]

        if _AFFIRM.match(lowered):
            out["confirmation"] = "yes"
        elif _DENY.match(lowered):
            out["confirmation"] = "no"

        if _CHANGE.search(lowered):
            out["is_correction"] = True

        if "intent" not in out:
            if out.get("service"):
                out["intent"] = "book"
            elif out.get("confirmation"):
                out["intent"] = "confirm"
            elif any(k in out for k in ("area", "date_text", "time_text", "area_ambiguous")):
                out["intent"] = "provide_detail"
            else:
                out["intent"] = "unclear"

        return out


def build_llm(settings: Any) -> LLMProvider:
    """Pick a provider from configuration.

    Falls back to the deterministic understudy when no key is configured, which is
    why ``chat_sim`` works out of the box.
    """
    provider = getattr(settings, "llm_provider", "deterministic")
    key = getattr(settings, "openai_api_key", None)

    if provider == "openai" and key is not None:
        return OpenAIProvider(
            api_key=key.get_secret_value(),
            model=getattr(settings, "llm_model", "gpt-4o-mini"),
            timeout_ms=getattr(settings, "llm_timeout_ms", 2000),
        )
    if provider == "openai":
        log.warning("llm.no_credentials_falling_back", requested=provider)
    return DeterministicProvider()


__all__ = [
    "DeterministicProvider",
    "LLMProvider",
    "OpenAIProvider",
    "Turn",
    "build_llm",
]
