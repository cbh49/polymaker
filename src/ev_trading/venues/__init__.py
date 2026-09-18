"""Per-venue adapters. Polymarket wraps `polymaker`; the other three are stubs."""

from __future__ import annotations

from collections.abc import Callable

from ev_trading.models import Venue
from ev_trading.venues.base import VenueClient
from ev_trading.venues.kalshi import KalshiVenue
from ev_trading.venues.polymarket import PolymarketVenue
from ev_trading.venues.prophetx import ProphetXVenue
from ev_trading.venues.sxbet import SxBetVenue

CLIENTS: dict[Venue, Callable[[], VenueClient]] = {
    Venue.POLYMARKET: PolymarketVenue,
    Venue.KALSHI: KalshiVenue,
    Venue.PROPHETX: ProphetXVenue,
    Venue.SXBET: SxBetVenue,
}

__all__ = [
    "CLIENTS",
    "KalshiVenue",
    "PolymarketVenue",
    "ProphetXVenue",
    "SxBetVenue",
    "VenueClient",
]
