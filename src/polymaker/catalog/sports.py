"""Sports event discovery helpers for Polymarket Gamma series.

MLB / WNBA (and future leagues) are discovered via `GET /events?series_slug=…`.
Moneyline markets use the game slug `{league}-{away}-{home}-YYYY-MM-DD`. Gamma's
`startDate` is listing time — game day is `eventDate`; tip-off is `startTime`.
"""

from __future__ import annotations

import re
from datetime import UTC, date, datetime, timedelta
from typing import Any

def default_sports_series_slugs(today: date | None = None) -> tuple[str, ...]:
    year = (today or datetime.now(UTC).date()).year
    return ("mlb", "wnba", "ufc", f"cfb-{year}")


# Supported Polymarket sports series slugs (Gamma `series_slug` query param).
SPORTS_SERIES_SLUGS: tuple[str, ...] = default_sports_series_slugs()

# Don't trade inside this window before tip-off — books are jumpy at kickoff.
DEFAULT_PREGAME_BUFFER_MINUTES = 5
CFB_LOOK_AHEAD_DAYS = 7

# Moneyline: mlb-atl-cws-2026-08-20 / wnba-wsh-gsv-2026-07-20 / ufc-ant-gre3-2026-08-22
# / cfb-hawaii-stan-2026-08-29
_MONEYLINE_RE = re.compile(
    r"^(?P<league>mlb|wnba|ufc|cfb)-[a-z0-9]+-[a-z0-9]+-(?P<ymd>\d{4}-\d{2}-\d{2})$"
)


def is_sports_series(slug: str | None) -> bool:
    if not slug:
        return False
    s = slug.lower()
    if s in {"mlb", "wnba", "ufc", "cfb"}:
        return True
    if s.startswith("cfb-") and s[4:].isdigit():
        return True
    return s in SPORTS_SERIES_SLUGS


def look_ahead_days_for_series(series_slug: str, default: int) -> int:
    """CFB weekend slates need a longer window than daily MLB/WNBA boards."""
    s = (series_slug or "").lower()
    if s == "cfb" or s.startswith("cfb-"):
        return max(default, CFB_LOOK_AHEAD_DAYS)
    return default


def is_moneyline_slug(slug: str | None) -> bool:
    """True for game moneylines; false for spreads/totals/props/first-five etc."""
    if not slug:
        return False
    return _MONEYLINE_RE.match(slug) is not None


def parse_event_date(value: str | None) -> date | None:
    """Parse Gamma `eventDate` (usually `YYYY-MM-DD`, sometimes full ISO)."""
    if not value:
        return None
    try:
        return date.fromisoformat(value[:10])
    except ValueError:
        return None


def parse_utc(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(UTC)
    except ValueError:
        return None


def event_date_in_window(
    event_date: str | None,
    *,
    look_ahead_days: int = 3,
    today: date | None = None,
) -> bool:
    """Keep games whose `eventDate` is today UTC through +look_ahead_days."""
    d = parse_event_date(event_date)
    if d is None:
        return False
    start = today or datetime.now(UTC).date()
    end = start + timedelta(days=look_ahead_days)
    return start <= d <= end


def event_looks_sports(event: dict[str, Any]) -> bool:
    """Identify sports events via seriesSlug or moneyline event slug."""
    if is_sports_series(event.get("seriesSlug")):
        return True
    return is_moneyline_slug(event.get("slug"))


def is_pre_game(
    event: dict[str, Any] | None,
    buffer_minutes: float = DEFAULT_PREGAME_BUFFER_MINUTES,
    *,
    now: datetime | None = None,
) -> bool:
    """True iff `startTime` is more than `buffer_minutes` in the future (UTC).

    Do not use `event.live` or `event.gameStatus` — those fields are often
    null even after a game has started (Gamma API bug). Missing or
    unparseable `startTime` is not pre-game (fail closed for trading).
    """
    if not event:
        return False
    start = parse_utc(event.get("startTime") or event.get("start_time"))
    if start is None:
        return False
    clock = now or datetime.now(UTC)
    return start > clock + timedelta(minutes=buffer_minutes)


def is_live_or_started(event: dict[str, Any], *, now: datetime | None = None) -> bool:
    """True when tip-off (`startTime`) is at or before `now`.

    Ignores `live` / `gameStatus` (unreliable). Missing `startTime` is not
    treated as started. Prefer `is_pre_game` for trade gating — that helper
    also applies the kickoff buffer and fails closed on missing startTime.
    """
    start = parse_utc((event or {}).get("startTime") or (event or {}).get("start_time"))
    if start is None:
        return False
    clock = now or datetime.now(UTC)
    return start <= clock


def should_skip_live_event(
    event: dict[str, Any],
    *,
    skip_live: bool = True,
    now: datetime | None = None,
    buffer_minutes: float = DEFAULT_PREGAME_BUFFER_MINUTES,
) -> bool:
    """Skip sports games that are not pre-game when skip_live is on.

    Uses `startTime` vs UTC (plus buffer). Does not trust `live` / `gameStatus`.
    """
    if not skip_live:
        return False
    if not event_looks_sports(event):
        return False
    return not is_pre_game(event, buffer_minutes, now=now)


def pick_moneyline_market(event: dict[str, Any]) -> dict[str, Any] | None:
    """Return the open moneyline market nested under a sports event, if any."""
    event_slug = event.get("slug") or ""
    if not is_moneyline_slug(event_slug):
        return None
    for raw in event.get("markets") or []:
        if raw.get("closed"):
            continue
        if raw.get("slug") == event_slug:
            return raw if isinstance(raw, dict) else None
    return None


def nested_event(raw_market: dict[str, Any]) -> dict[str, Any] | None:
    """First nested event on a Gamma market dict (used by metadata refresh)."""
    events = raw_market.get("events") or []
    if not events:
        return None
    first = events[0]
    return first if isinstance(first, dict) else None
