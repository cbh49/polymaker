"""SX Bet adapter. Endpoints filled in once their API docs land."""

from __future__ import annotations

from ev_trading.models import Venue, VenueQuote


class SxBetVenue:
    venue = Venue.SXBET

    async def aclose(self) -> None:
        return None

    async def fetch_quotes(
        self,
        *,
        query: str | None = None,
        limit: int = 400,
    ) -> list[VenueQuote]:
        del query, limit
        return []
