"""Fee-adjusted all-in cost comparison for sharp-money venue routing."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

from ev_trading.fair_value.tradable_pricer import (
    kalshi_fee_per_contract,
    polymarket_fee_per_contract,
)

VenueName = Literal["polymarket", "kalshi"]

# 1/100¢ — treat all-in costs this close as a tie (Polymarket wins by default).
TIE_EPSILON = 0.0001


@dataclass(frozen=True, slots=True)
class PricedVenue:
    venue: VenueName
    ask: float
    fee: float
    all_in: float
    usd: float
    contracts: int | None = None


def fee_per_contract(venue: VenueName, ask: float) -> float:
    """Taker fee in dollars per $1 contract at this ask."""
    px = min(max(float(ask), 0.0), 1.0)
    if venue == "kalshi":
        return kalshi_fee_per_contract(px)
    if venue == "polymarket":
        return polymarket_fee_per_contract(px)
    raise ValueError(f"unknown venue: {venue}")


def price_venue(
    venue: VenueName,
    ask: float,
    usd: float,
    *,
    contracts: int | None = None,
) -> PricedVenue:
    fee = fee_per_contract(venue, ask)
    return PricedVenue(
        venue=venue,
        ask=float(ask),
        fee=fee,
        all_in=float(ask) + fee,
        usd=float(usd),
        contracts=contracts,
    )


def pick_venue(
    candidates: list[PricedVenue],
    *,
    tie_venue: VenueName = "polymarket",
    epsilon: float = TIE_EPSILON,
) -> PricedVenue | None:
    """Choose the lowest all-in cost. Ties (within epsilon) go to `tie_venue`."""
    usable = [c for c in candidates if c.ask > 0 and c.ask < 1 and c.usd > 0]
    if not usable:
        return None
    by_venue = {c.venue: c for c in usable}
    preferred = by_venue.get(tie_venue)
    if preferred is not None:
        tied = [
            c
            for c in usable
            if c.venue != tie_venue and abs(c.all_in - preferred.all_in) <= epsilon
        ]
        if tied or all(abs(c.all_in - preferred.all_in) <= epsilon for c in usable):
            # Preferred venue is within epsilon of the best (or is the only quote).
            best = min(usable, key=lambda c: c.all_in)
            if abs(preferred.all_in - best.all_in) <= epsilon:
                return preferred
    return min(usable, key=lambda c: (c.all_in, 0 if c.venue == tie_venue else 1))
