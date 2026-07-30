"""Understanding one message.

Thin by design. It assembles a prompt, asks a provider, and validates the answer
into an :class:`Understanding`. Everything consequential — what to do about it —
lives in ``states.py``.

Two guarantees this layer owns:

* **It never raises.** A provider that times out, returns prose instead of JSON, or
  invents fields degrades to "understood nothing", which the state machine answers
  with a clarifying question. An NLU outage must not become a consumer-visible one.
* **It never produces copy.** The return type cannot carry text for a consumer. A
  model behind this interface has no route to the send path.
"""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path
from typing import Any

from mawjood.core.conversation.states import Understanding
from mawjood.observability.logging import get_logger
from mawjood.services.llm import LLMProvider, Turn

log = get_logger(__name__)

PROMPTS_DIR = Path(__file__).parent / "prompts"
PROMPT_VERSION = "nlu_v1"

# Handed to the provider so a JSON-schema-capable model can be constrained.
UNDERSTANDING_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "intent": {
            "type": "string",
            "enum": [
                "book",
                "provide_detail",
                "confirm",
                "booking_status",
                "reschedule_booking",
                "cancel_booking",
                "unclear",
            ],
        },
        "service": {"type": "string"},
        "category": {
            "type": "string",
            "enum": ["salon", "spa", "restaurant", "food_delivery", "ride", "other"],
        },
        "area": {"type": "string"},
        "area_ambiguous": {"type": "array", "items": {"type": "string"}},
        "date_text": {"type": "string"},
        "time_text": {"type": "string"},
        "confirmation": {"type": "string", "enum": ["yes", "no"]},
        "wants_human": {"type": "boolean"},
        "is_correction": {"type": "boolean"},
        "emoji_only": {"type": "boolean"},
        "language": {"type": "string"},
    },
    "additionalProperties": False,
}

# How much history the model sees. Enough for "make it Thursday" to have a
# referent; not so much that an old message re-triggers an intent.
HISTORY_TURNS = 6


@lru_cache(maxsize=4)
def load_prompt(version: str = PROMPT_VERSION) -> str:
    """Prompts are versioned files, not inline strings.

    So a prompt change is reviewable in a diff, and an audit row can name the
    version that produced a reading.
    """
    path = PROMPTS_DIR / f"{version}.md"
    if not path.exists():
        raise FileNotFoundError(f"prompt {version!r} not found at {path}")
    return path.read_text(encoding="utf-8")


async def understand(
    provider: LLMProvider,
    *,
    message: str,
    history: list[Turn] | None = None,
    prompt_version: str = PROMPT_VERSION,
) -> Understanding:
    """Read one message. Never raises."""
    turns = list(history or [])[-HISTORY_TURNS:]
    turns.append(Turn(role="user", content=message))

    try:
        raw = await provider.extract(
            system=load_prompt(prompt_version),
            history=turns,
            schema=UNDERSTANDING_SCHEMA,
        )
    except Exception as exc:
        log.warning("nlu.provider_failed", provider=provider.name, error=type(exc).__name__)
        return Understanding(intent="unclear")

    if not isinstance(raw, dict):
        log.warning("nlu.non_object_response", provider=provider.name)
        return Understanding(intent="unclear")

    try:
        return Understanding.from_dict(raw)
    except Exception as exc:
        log.warning("nlu.unparseable_response", provider=provider.name, error=type(exc).__name__)
        return Understanding(intent="unclear")


__all__ = [
    "HISTORY_TURNS",
    "PROMPT_VERSION",
    "UNDERSTANDING_SCHEMA",
    "load_prompt",
    "understand",
]
