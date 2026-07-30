"""Emirates place names.

A consumer says "Marina", "JLT", "Al Nahda" — not "Jumeirah Lakes Towers, Dubai,
United Arab Emirates". Resolving that naturally is most of sounding local.

Two things matter here:

* **Aliases.** "marina", "dubai marina", "the marina" are one place.
* **Genuine ambiguity.** Some names exist in more than one emirate — *Al Nahda* is
  in both Dubai and Sharjah, *Al Qusais* likewise, and *Corniche* is Abu Dhabi's
  and Sharjah's. Guessing which one a consumer meant and booking there is worse
  than asking. Those return AMBIGUOUS and the state machine asks one short
  question.

Kept as data, so extending coverage never touches logic.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from enum import StrEnum, auto


class Emirate(StrEnum):
    DUBAI = "Dubai"
    ABU_DHABI = "Abu Dhabi"
    SHARJAH = "Sharjah"
    AJMAN = "Ajman"
    RAS_AL_KHAIMAH = "Ras Al Khaimah"
    FUJAIRAH = "Fujairah"
    UMM_AL_QUWAIN = "Umm Al Quwain"


@dataclass(frozen=True, slots=True)
class Place:
    canonical: str
    emirate: Emirate
    aliases: tuple[str, ...] = ()

    @property
    def display(self) -> str:
        return self.canonical


class Resolution(StrEnum):
    RESOLVED = auto()
    AMBIGUOUS = auto()
    UNKNOWN = auto()


@dataclass(frozen=True, slots=True)
class PlaceMatch:
    resolution: Resolution
    place: Place | None = None
    # Populated when the same name exists in more than one emirate.
    candidates: tuple[Place, ...] = ()
    raw: str = ""

    @property
    def needs_clarification(self) -> bool:
        return self.resolution is Resolution.AMBIGUOUS

    def describe_options(self) -> str:
        return " or ".join(f"{p.canonical} in {p.emirate}" for p in self.candidates)


# Coverage is the places a Dubai-first launch actually meets, plus the
# cross-emirate collisions that cause real mistakes.
PLACES: tuple[Place, ...] = (
    # --- Dubai ---
    Place("Dubai Marina", Emirate.DUBAI, ("marina", "the marina", "dubai marina")),
    Place("JLT", Emirate.DUBAI, ("jlt", "jumeirah lakes towers", "lakes towers")),
    Place("JBR", Emirate.DUBAI, ("jbr", "jumeirah beach residence", "the walk")),
    Place("Downtown Dubai", Emirate.DUBAI, ("downtown", "downtown dubai", "dubai mall area")),
    Place("Business Bay", Emirate.DUBAI, ("business bay", "bay")),
    Place("DIFC", Emirate.DUBAI, ("difc", "financial centre", "financial center")),
    Place("Jumeirah", Emirate.DUBAI, ("jumeirah", "jumeira")),
    Place("Umm Suqeim", Emirate.DUBAI, ("umm suqeim", "um suqeim")),
    Place("Al Barsha", Emirate.DUBAI, ("al barsha", "barsha")),
    Place("Barsha Heights", Emirate.DUBAI, ("barsha heights", "tecom")),
    Place("Deira", Emirate.DUBAI, ("deira",)),
    Place("Bur Dubai", Emirate.DUBAI, ("bur dubai",)),
    Place("Karama", Emirate.DUBAI, ("karama", "al karama")),
    Place("Satwa", Emirate.DUBAI, ("satwa", "al satwa")),
    Place("Mirdif", Emirate.DUBAI, ("mirdif",)),
    Place("Al Quoz", Emirate.DUBAI, ("al quoz", "quoz")),
    Place("Palm Jumeirah", Emirate.DUBAI, ("palm", "the palm", "palm jumeirah")),
    Place("City Walk", Emirate.DUBAI, ("city walk",)),
    Place("Dubai Silicon Oasis", Emirate.DUBAI, ("silicon oasis", "dso")),
    Place("Motor City", Emirate.DUBAI, ("motor city",)),
    Place("Dubai Sports City", Emirate.DUBAI, ("sports city",)),
    Place("Arabian Ranches", Emirate.DUBAI, ("arabian ranches", "ranches")),
    Place("Mudon", Emirate.DUBAI, ("mudon",)),
    Place("Dubai Hills", Emirate.DUBAI, ("dubai hills", "hills estate")),
    Place("Al Furjan", Emirate.DUBAI, ("al furjan", "furjan")),
    Place("Discovery Gardens", Emirate.DUBAI, ("discovery gardens",)),
    Place("Al Nahda, Dubai", Emirate.DUBAI, ("al nahda dubai", "nahda dubai")),
    Place("Al Qusais, Dubai", Emirate.DUBAI, ("al qusais dubai", "qusais dubai")),
    # --- Abu Dhabi ---
    Place("Al Reem Island", Emirate.ABU_DHABI, ("reem island", "al reem")),
    Place("Yas Island", Emirate.ABU_DHABI, ("yas island", "yas")),
    Place("Saadiyat", Emirate.ABU_DHABI, ("saadiyat", "saadiyat island")),
    Place("Khalifa City", Emirate.ABU_DHABI, ("khalifa city",)),
    Place("Al Bateen", Emirate.ABU_DHABI, ("al bateen", "bateen")),
    Place("Corniche, Abu Dhabi", Emirate.ABU_DHABI, ("corniche abu dhabi",)),
    # --- Sharjah ---
    Place("Al Majaz", Emirate.SHARJAH, ("al majaz", "majaz")),
    Place("Al Nahda, Sharjah", Emirate.SHARJAH, ("al nahda sharjah", "nahda sharjah")),
    Place("Al Qusais, Sharjah", Emirate.SHARJAH, ("al qusais sharjah",)),
    Place("Corniche, Sharjah", Emirate.SHARJAH, ("corniche sharjah",)),
    # --- Other emirates ---
    Place("Ajman Corniche", Emirate.AJMAN, ("ajman corniche",)),
    Place("Al Hamra", Emirate.RAS_AL_KHAIMAH, ("al hamra", "hamra")),
)

# Bare names that exist in more than one emirate. Asking beats assuming: booking a
# consumer into the wrong emirate is a forty-minute drive, not a rounding error.
AMBIGUOUS_NAMES: dict[str, tuple[str, ...]] = {
    "al nahda": ("Al Nahda, Dubai", "Al Nahda, Sharjah"),
    "nahda": ("Al Nahda, Dubai", "Al Nahda, Sharjah"),
    "al qusais": ("Al Qusais, Dubai", "Al Qusais, Sharjah"),
    "qusais": ("Al Qusais, Dubai", "Al Qusais, Sharjah"),
    "corniche": ("Corniche, Abu Dhabi", "Corniche, Sharjah"),
}


@dataclass(frozen=True, slots=True)
class _Index:
    by_alias: dict[str, Place] = field(default_factory=dict)
    by_canonical: dict[str, Place] = field(default_factory=dict)


def _build_index() -> _Index:
    index = _Index()
    for place in PLACES:
        index.by_canonical[place.canonical.lower()] = place
        index.by_alias[place.canonical.lower()] = place
        for alias in place.aliases:
            index.by_alias[alias.lower()] = place
    return index


_INDEX = _build_index()

_PUNCTUATION = re.compile(r"[.,!?;:]+")


def normalise(text: str) -> str:
    return _PUNCTUATION.sub("", text.strip().lower())


def resolve(text: str) -> PlaceMatch:
    """Resolve a consumer's words into a place, or ask."""
    cleaned = normalise(text)
    if not cleaned:
        return PlaceMatch(resolution=Resolution.UNKNOWN, raw=text)

    # Ambiguity is checked first: "al nahda" must not silently match the Dubai
    # entry just because it sorts earlier.
    if cleaned in AMBIGUOUS_NAMES:
        candidates = tuple(
            _INDEX.by_canonical[name.lower()]
            for name in AMBIGUOUS_NAMES[cleaned]
            if name.lower() in _INDEX.by_canonical
        )
        return PlaceMatch(resolution=Resolution.AMBIGUOUS, candidates=candidates, raw=text)

    exact = _INDEX.by_alias.get(cleaned)
    if exact is not None:
        return PlaceMatch(resolution=Resolution.RESOLVED, place=exact, raw=text)

    return PlaceMatch(resolution=Resolution.UNKNOWN, raw=text)


def find_in_sentence(sentence: str) -> PlaceMatch:
    """Pick a place out of a whole message.

    Longest alias first, so "al nahda dubai" beats the ambiguous "al nahda", and
    "dubai marina" is not shadowed by a shorter partial match.
    """
    cleaned = f" {normalise(sentence)} "

    disambiguated = sorted(
        (alias for alias in _INDEX.by_alias if alias not in AMBIGUOUS_NAMES),
        key=len,
        reverse=True,
    )
    for alias in disambiguated:
        if f" {alias} " in cleaned:
            return PlaceMatch(
                resolution=Resolution.RESOLVED, place=_INDEX.by_alias[alias], raw=alias
            )

    for name in sorted(AMBIGUOUS_NAMES, key=len, reverse=True):
        if f" {name} " in cleaned:
            return resolve(name)

    return PlaceMatch(resolution=Resolution.UNKNOWN, raw=sentence)


__all__ = [
    "AMBIGUOUS_NAMES",
    "PLACES",
    "Emirate",
    "Place",
    "PlaceMatch",
    "Resolution",
    "find_in_sentence",
    "normalise",
    "resolve",
]
