"""Polymarket adapter — imports the existing Gamma catalog. Fetch wiring comes later."""

from __future__ import annotations

from ev_trading.models import Venue, VenueQuote
from polymaker.catalog.gamma import GammaClient


class PolymarketVenue:
    venue = Venue.POLYMARKET

    def __init__(self) -> None:
        self._gamma = GammaClient()

    async def aclose(self) -> None:
        await self._gamma.aclose()

    async def fetch_quotes(
        self,
        *,
        query: str | None = None,
        limit: int = 400,
    ) -> list[VenueQuote]:
        del query, limit
        return []
