"""Compare normalized quotes across venues."""

from __future__ import annotations

from collections import defaultdict

from ev_trading.match import match_key
from ev_trading.models import CrossVenueRow, Venue, VenueQuote


def group_quotes(quotes: list[VenueQuote]) -> dict[str, list[VenueQuote]]:
    grouped: dict[str, list[VenueQuote]] = defaultdict(list)
    for quote in quotes:
        grouped[match_key(quote.question, quote.outcome)].append(quote)
    return dict(grouped)


def cross_venue_rows(
    quotes: list[VenueQuote],
    *,
    min_spread: float = 0.03,
    min_venues: int = 2,
) -> list[CrossVenueRow]:
    """Rows where the same (question, outcome) prints on 2+ venues with a mid gap."""
    rows: list[CrossVenueRow] = []
    for key, bucket in group_quotes(quotes).items():
        by_venue: dict[Venue, VenueQuote] = {}
        for quote in bucket:
            mid = quote.mid
            if mid is None:
                continue
            prev = by_venue.get(quote.venue)
            if prev is None or (prev.mid is not None and mid > prev.mid):
                by_venue[quote.venue] = quote
        if len(by_venue) < min_venues:
            continue
        ranked = sorted(by_venue.values(), key=lambda q: q.mid or 0.0)
        cheap, rich = ranked[0], ranked[-1]
        cheap_mid, rich_mid = cheap.mid, rich.mid
        if cheap_mid is None or rich_mid is None:
            continue
        spread = rich_mid - cheap_mid
        if spread < min_spread:
            continue
        sample = ranked[0]
        rows.append(
            CrossVenueRow(
                match_key=key,
                question=sample.question,
                outcome=sample.outcome,
                quotes=tuple(ranked),
                spread=spread,
                cheap_venue=cheap.venue,
                rich_venue=rich.venue,
                cheap_mid=cheap_mid,
                rich_mid=rich_mid,
            )
        )
    rows.sort(key=lambda r: r.spread, reverse=True)
    return rows
