"""Match keys and empty venue adapters."""

from __future__ import annotations

import asyncio

from ev_trading.edge import cross_venue_rows
from ev_trading.match import match_key, normalize_outcome
from ev_trading.models import Venue, VenueQuote
from ev_trading.venues import CLIENTS


def test_match_key_normalizes_yes_no() -> None:
    assert match_key("Will Team A win?", "YES") == match_key("will team a win", "y")
    assert normalize_outcome("No") == "no"


def test_cross_venue_spread() -> None:
    quotes = [
        VenueQuote(
            venue=Venue.POLYMARKET,
            market_id="p",
            event_id="e",
            question="Will Team A win?",
            outcome="Yes",
            bid=0.40,
            ask=0.42,
        ),
        VenueQuote(
            venue=Venue.KALSHI,
            market_id="k",
            event_id="e",
            question="Will Team A win?",
            outcome="Yes",
            bid=0.50,
            ask=0.52,
        ),
    ]
    rows = cross_venue_rows(quotes, min_spread=0.03)
    assert len(rows) == 1
    assert rows[0].cheap_venue is Venue.POLYMARKET
    assert rows[0].rich_venue is Venue.KALSHI


def test_all_four_venues_registered() -> None:
    assert set(CLIENTS) == {Venue.POLYMARKET, Venue.KALSHI, Venue.PROPHETX, Venue.SXBET}


def test_stub_clients_return_empty() -> None:
    async def _run() -> None:
        for factory in CLIENTS.values():
            client = factory()
            try:
                assert await client.fetch_quotes() == []
            finally:
                await client.aclose()

    asyncio.run(_run())
