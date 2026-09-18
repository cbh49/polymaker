"""Shared odds types so every venue emits the same shape."""

from __future__ import annotations

from dataclasses import asdict, dataclass
from enum import Enum
from typing import Any


class Venue(str, Enum):
    POLYMARKET = "polymarket"
    KALSHI = "kalshi"
    PROPHETX = "prophetx"
    SXBET = "sxbet"


def implied_prob(value: Any) -> float | None:
    """Coerce a venue price into a 0–1 probability."""
    if value is None or value == "":
        return None
    try:
        raw = float(value)
    except (TypeError, ValueError):
        return None
    if raw > 1.0:
        raw /= 100.0
    if raw < 0.0 or raw > 1.0:
        return None
    return raw


@dataclass(frozen=True, slots=True)
class VenueQuote:
    """One outcome's book on one venue. Prices are 0–1 probabilities."""

    venue: Venue
    market_id: str
    event_id: str
    question: str
    outcome: str
    bid: float | None = None
    ask: float | None = None
    last: float | None = None
    volume: float | None = None
    liquidity: float | None = None
    url: str | None = None
    end_date_iso: str | None = None
    fetched_at: float = 0.0

    @property
    def mid(self) -> float | None:
        if self.bid is not None and self.ask is not None:
            return (self.bid + self.ask) / 2.0
        if self.last is not None:
            return self.last
        return self.ask if self.ask is not None else self.bid

    def to_dict(self) -> dict[str, Any]:
        row = asdict(self)
        row["venue"] = self.venue.value
        row["mid"] = self.mid
        return row


@dataclass(frozen=True, slots=True)
class CrossVenueRow:
    """Quotes that look like the same event/outcome across two or more venues."""

    match_key: str
    question: str
    outcome: str
    quotes: tuple[VenueQuote, ...]
    spread: float
    cheap_venue: Venue
    rich_venue: Venue
    cheap_mid: float
    rich_mid: float

    def to_dict(self) -> dict[str, Any]:
        return {
            "match_key": self.match_key,
            "question": self.question,
            "outcome": self.outcome,
            "spread": self.spread,
            "cheap_venue": self.cheap_venue.value,
            "rich_venue": self.rich_venue.value,
            "cheap_mid": self.cheap_mid,
            "rich_mid": self.rich_mid,
            "quotes": [q.to_dict() for q in self.quotes],
        }
