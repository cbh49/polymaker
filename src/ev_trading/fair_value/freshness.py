"""Kickoff and quote-age guards shared by the pricer and both executors.

RotoWire `gameDate` is US Eastern wall time (`2026-10-04 13:00:00` is 1:00pm ET).
Kalshi `close_time` is when the market resolves, not kickoff, so it is not a clock.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any
from zoneinfo import ZoneInfo

from polymaker.catalog.sports import DEFAULT_PREGAME_BUFFER_MINUTES, is_pre_game

ET = ZoneInfo("America/New_York")

MAX_BOOK_VENUE_LAG_SECONDS = 600.0


def parse_utc(value: Any) -> datetime | None:
    """Parse an ISO timestamp. Naive values are treated as UTC."""
    if not isinstance(value, str):
        return None
    text = value.strip()
    if not text:
        return None
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)


def as_utc_iso(value: Any) -> str | None:
    parsed = parse_utc(value)
    if parsed is None:
        return None
    return parsed.isoformat()


def kickoff_from_rotowire(game_date: Any) -> str | None:
    """Eastern wall time from RotoWire `gameDate` → UTC ISO."""
    if not isinstance(game_date, str):
        return None
    text = game_date.strip()
    if not text:
        return None
    aware = parse_utc(text) if ("Z" in text or "+" in text[10:]) else None
    if aware is not None and ("Z" in text or "+" in text[10:]):
        return aware.isoformat()
    naive: datetime | None = None
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M:%S"):
        try:
            naive = datetime.strptime(text[:19], fmt)
            break
        except ValueError:
            continue
    if naive is None:
        return None
    return naive.replace(tzinfo=ET).astimezone(UTC).isoformat()


def kickoff_iso(game_date: Any, poly_start: Any = None) -> str | None:
    """Prefer the RotoWire Eastern kickoff. Gamma `startTime` is the backup."""
    return kickoff_from_rotowire(game_date) or as_utc_iso(poly_start)


def _text(value: Any) -> str | None:
    if isinstance(value, str) and value.strip():
        return value.strip()
    return None


@dataclass(frozen=True, slots=True)
class QuoteTimes:
    """Clocks copied from the aggregate onto each priced side."""

    kickoff: str | None = None
    books_quoted_at: str | None = None
    venue_kalshi: str | None = None
    venue_polymarket: str | None = None

    def venue_at(self, venue: str) -> str | None:
        if venue == "kalshi":
            return self.venue_kalshi
        return self.venue_polymarket


def quote_times_for(game: dict[str, Any], *, props: bool) -> QuoteTimes:
    books_key = "props_quoted_at" if props else "books_quoted_at"
    return QuoteTimes(
        kickoff=_text(game.get("kickoff")),
        books_quoted_at=_text(game.get(books_key)),
        venue_kalshi=_text(game.get("kalshi_quoted_at")),
        venue_polymarket=_text(game.get("polymarket_quoted_at")),
    )


def reference_is_stale(
    books_quoted_at: Any,
    venue_quoted_at: Any,
    *,
    max_lag_seconds: float = MAX_BOOK_VENUE_LAG_SECONDS,
) -> bool:
    """True when the book scrape is missing or more than `max_lag_seconds` older than the ask."""
    books = parse_utc(books_quoted_at)
    venue = parse_utc(venue_quoted_at)
    if books is None or venue is None:
        return True
    return (venue - books).total_seconds() > max_lag_seconds


def execution_block(
    row: dict[str, Any],
    *,
    buffer_minutes: float = DEFAULT_PREGAME_BUFFER_MINUTES,
    max_lag_seconds: float = MAX_BOOK_VENUE_LAG_SECONDS,
    now: datetime | None = None,
) -> str | None:
    """Why this row must not be sent. None means the clocks allow an order."""
    kickoff = row.get("kickoff")
    if parse_utc(kickoff) is None:
        return "missing kickoff"
    if not is_pre_game({"startTime": kickoff}, buffer_minutes, now=now):
        return "inside kickoff buffer"
    if (
        parse_utc(row.get("books_quoted_at")) is None
        or parse_utc(row.get("venue_quoted_at")) is None
    ):
        return "missing quote time"
    if reference_is_stale(
        row.get("books_quoted_at"),
        row.get("venue_quoted_at"),
        max_lag_seconds=max_lag_seconds,
    ):
        return "stale books"
    return None
