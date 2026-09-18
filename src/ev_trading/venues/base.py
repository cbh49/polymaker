"""Venue client protocol + registry."""

from __future__ import annotations

from typing import Protocol

from ev_trading.models import Venue, VenueQuote


class VenueClient(Protocol):
    venue: Venue

    async def fetch_quotes(
        self,
        *,
        query: str | None = None,
        limit: int = 400,
    ) -> list[VenueQuote]: ...

    async def aclose(self) -> None: ...
