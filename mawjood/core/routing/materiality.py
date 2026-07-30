"""Re-confirm on material change.

Decision 2. The consumer confirms a slot, the cascade fails over, and the next
candidate's best offer is not quite the same thing. A different venue, a time that
has moved, a higher price — book any of those silently and Mawjood has made a
decision that was not its to make.

The rule: **inside tolerance, book it and say plainly what was booked. Outside
tolerance, ask once.**

This is a consent boundary. Do not widen the tolerances to make a test pass.

Pure function, tolerances from config, so the boundary is one obvious place.
"""

from __future__ import annotations

from dataclasses import dataclass

from mawjood.core.aggregators.base import Slot


@dataclass(frozen=True, slots=True)
class Tolerances:
    """How much drift is acceptable without asking again."""

    # A different venue always needs asking — a name on a confirmation is the
    # thing a consumer actually turns up to.
    venue_must_match: bool = True
    start_minutes: int = 30
    price_percent: float = 15.0


@dataclass(frozen=True, slots=True)
class MaterialityVerdict:
    material: bool
    reasons: tuple[str, ...] = ()

    @property
    def needs_reconfirmation(self) -> bool:
        return self.material


def compare(
    confirmed: Slot, replacement: Slot, tolerances: Tolerances | None = None
) -> MaterialityVerdict:
    """Decide whether a substituted slot needs a fresh confirmation."""
    tolerances = tolerances or Tolerances()
    reasons: list[str] = []

    if tolerances.venue_must_match and _venue_key(confirmed) != _venue_key(replacement):
        reasons.append(f"venue changed from {confirmed.venue_name!r} to {replacement.venue_name!r}")

    drift_minutes = abs((replacement.start - confirmed.start).total_seconds()) / 60
    if drift_minutes > tolerances.start_minutes:
        reasons.append(
            f"start moved by {int(drift_minutes)} minutes, "
            f"beyond the {tolerances.start_minutes} minute tolerance"
        )

    if confirmed.price_amount and replacement.price_amount:
        delta = replacement.price_amount - confirmed.price_amount
        percent = (delta / confirmed.price_amount) * 100
        # Only an increase is material. A cheaper slot needs no permission.
        if percent > tolerances.price_percent:
            reasons.append(
                f"price rose {percent:.0f}%, beyond the {tolerances.price_percent:.0f}% tolerance"
            )
    elif bool(confirmed.price_amount) != bool(replacement.price_amount):
        reasons.append("price is known for one slot and not the other")

    if replacement.requires_deposit and not confirmed.requires_deposit:
        # v1 moves no money (decision 3), so a deposit is not a re-confirm — it
        # is a handoff. Flagged material so the caller stops and asks.
        reasons.append("replacement requires a deposit, which v1 does not handle")

    return MaterialityVerdict(material=bool(reasons), reasons=tuple(reasons))


def _venue_key(slot: Slot) -> tuple[str, str]:
    return (slot.venue_name.strip().lower(), slot.merchant.platform_slug)


__all__ = ["MaterialityVerdict", "Tolerances", "compare"]
