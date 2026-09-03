"""Sports event discovery helpers for Polymarket Gamma series.

MLB / WNBA / UFC / CFB are discovered via `GET /events?series_slug=…`.
A Gamma sports event slug is the moneyline (`{league}-{away}-{home}-YYYY-MM-DD`);
spreads and totals are nested markets under that event. Gamma's `startDate` is
listing time — game day is `eventDate`; tip-off is `startTime`.
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
UFC_LOOK_AHEAD_DAYS = 14

# Skip a spread/total rather than buy a line more than this many points away.
LINE_MATCH_TOLERANCE = 1.0

# Moneyline / event: mlb-atl-cws-2026-08-20 / wnba-wsh-gsv-2026-07-20
# / ufc-ant-gre3-2026-08-22 / cfb-hawaii-stan-2026-08-29
_MONEYLINE_RE = re.compile(
    r"^(?P<league>mlb|wnba|ufc|cfb)-(?P<away>[a-z0-9]+)-(?P<home>[a-z0-9]+)"
    r"-(?P<ymd>\d{4}-\d{2}-\d{2})$"
)
_SPREAD_SLUG_RE = re.compile(
    r"^(?P<league>mlb|wnba|ufc|cfb)-(?P<away>[a-z0-9]+)-(?P<home>[a-z0-9]+)"
    r"-(?P<ymd>\d{4}-\d{2}-\d{2})-spread-(?P<favored>home|away)-(?P<pts>\d+(?:pt\d+)?)$"
)
_TOTAL_SLUG_RE = re.compile(
    r"^(?P<league>mlb|wnba|ufc|cfb)-(?P<away>[a-z0-9]+)-(?P<home>[a-z0-9]+)"
    r"-(?P<ymd>\d{4}-\d{2}-\d{2})-(?:total|totals)-(?P<pts>\d+(?:pt\d+)?)$"
)

# Public aliases — scrape_polymarket_odds and tests import these names.
EVENT_SLUG_RE = _MONEYLINE_RE
SPREAD_SLUG_RE = _SPREAD_SLUG_RE
TOTAL_SLUG_RE = _TOTAL_SLUG_RE


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
    """CFB/UFC weekend slates need a longer window than daily MLB/WNBA boards."""
    s = (series_slug or "").lower()
    if s == "cfb" or s.startswith("cfb-"):
        return max(default, CFB_LOOK_AHEAD_DAYS)
    if s == "ufc":
        return max(default, UFC_LOOK_AHEAD_DAYS)
    return default


def is_moneyline_slug(slug: str | None) -> bool:
    """True for game moneylines; false for spreads/totals/props/first-five etc."""
    if not slug:
        return False
    return _MONEYLINE_RE.match(slug) is not None


def is_spread_slug(slug: str | None) -> bool:
    if not slug:
        return False
    return _SPREAD_SLUG_RE.match(slug) is not None


def is_total_slug(slug: str | None) -> bool:
    if not slug:
        return False
    return _TOTAL_SLUG_RE.match(slug) is not None


def is_sports_market_slug(slug: str | None) -> bool:
    """Moneyline, spread, or total — not 1H / player props / first-five."""
    return is_moneyline_slug(slug) or is_spread_slug(slug) or is_total_slug(slug)


def parse_pt_number(raw: str) -> float | None:
    """Parse Polymarket `pt` decimals: `5pt5` → 5.5, `21` → 21.0."""
    text = (raw or "").strip().lower()
    if not text:
        return None
    if "pt" in text:
        left, _, right = text.partition("pt")
        try:
            return float(f"{left}.{right}")
        except ValueError:
            return None
    try:
        return float(text)
    except ValueError:
        return None


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


def market_liquidity(raw: dict[str, Any]) -> float:
    """Gamma `liquidityNum` / `liquidity`; 0 when missing or non-positive."""
    for key in ("liquidityNum", "liquidity"):
        val = raw.get(key)
        if val is None or val == "":
            continue
        try:
            num = float(val)
        except (TypeError, ValueError):
            continue
        if num > 0:
            return round(num, 2)
    return 0.0


def pick_moneyline_market(event: dict[str, Any]) -> dict[str, Any] | None:
    """Return the open moneyline market nested under a sports event, if any."""
    event_slug = event.get("slug") or ""
    if not is_moneyline_slug(event_slug):
        return None
    for raw in event.get("markets") or []:
        if not isinstance(raw, dict) or raw.get("closed"):
            continue
        if raw.get("slug") == event_slug:
            return raw
    return None


def classify_event_markets(event: dict[str, Any]) -> dict[str, Any]:
    """Split nested Gamma markets into moneyline / spreads / totals (ignore 1H/props)."""
    event_slug = str(event.get("slug") or "")
    moneyline = None
    spreads: list[dict[str, Any]] = []
    totals: list[dict[str, Any]] = []
    for raw in event.get("markets") or []:
        if not isinstance(raw, dict) or raw.get("closed"):
            continue
        slug = str(raw.get("slug") or "")
        if slug == event_slug and _MONEYLINE_RE.match(slug):
            moneyline = raw
            continue
        spread_m = _SPREAD_SLUG_RE.match(slug)
        if spread_m:
            pts = parse_pt_number(spread_m.group("pts"))
            if pts is None:
                continue
            spreads.append(
                {
                    "raw": raw,
                    "favored": spread_m.group("favored"),
                    "points": pts,
                    "liquidity": market_liquidity(raw),
                }
            )
            continue
        total_m = _TOTAL_SLUG_RE.match(slug)
        if total_m:
            pts = parse_pt_number(total_m.group("pts"))
            if pts is None:
                continue
            totals.append(
                {
                    "raw": raw,
                    "points": pts,
                    "liquidity": market_liquidity(raw),
                }
            )
    return {"moneyline": moneyline, "spreads": spreads, "totals": totals}


def implied_home_spread(favored: str, pts: float) -> float:
    """Polymarket `favored=home, pts=X` → home line -X; `away` → home line +X."""
    return -pts if favored == "home" else pts


def _side_line_number(side: dict[str, Any] | None) -> float | None:
    if not isinstance(side, dict):
        return None
    for key in ("live", "eva_line", "sbd_line", "open"):
        val = side.get(key)
        if isinstance(val, (int, float)) and not isinstance(val, bool):
            return float(val)
    return None


def pick_spread_market(
    spreads: list[dict[str, Any]],
    dest_spread: dict[str, Any] | None = None,
    *,
    target_home_line: float | None = None,
    max_line_delta: float | None = None,
) -> dict[str, Any] | None:
    """Closest liquid spread to the sportsbook home line (exact within 0.01 first)."""
    usable = [s for s in spreads if (s.get("liquidity") or 0) > 0]
    if not usable:
        return None
    target = target_home_line
    if target is None:
        home_line = _side_line_number((dest_spread or {}).get("home"))
        away_line = _side_line_number((dest_spread or {}).get("away"))
        target = home_line if home_line is not None else (
            -away_line if away_line is not None else None
        )
    if target is None:
        return max(usable, key=lambda r: float(r.get("liquidity") or 0))
    for row in usable:
        implied = implied_home_spread(str(row["favored"]), float(row["points"]))
        if abs(implied - target) < 0.01:
            return row
    best = min(
        usable,
        key=lambda r: abs(implied_home_spread(str(r["favored"]), float(r["points"])) - target),
    )
    implied = implied_home_spread(str(best["favored"]), float(best["points"]))
    if max_line_delta is not None and abs(implied - target) > max_line_delta:
        return None
    return best


def pick_total_market(
    totals: list[dict[str, Any]],
    dest_total: dict[str, Any] | None = None,
    *,
    target_line: float | None = None,
    max_line_delta: float | None = None,
) -> dict[str, Any] | None:
    """Closest liquid total to the sportsbook number (exact within 0.01 first)."""
    usable = [t for t in totals if (t.get("liquidity") or 0) > 0]
    if not usable:
        return None
    target = target_line
    if target is None:
        target = _side_line_number((dest_total or {}).get("over"))
        if target is None:
            target = _side_line_number((dest_total or {}).get("under"))
    if target is None:
        return max(usable, key=lambda r: float(r.get("liquidity") or 0))
    for row in usable:
        if abs(float(row["points"]) - target) < 0.01:
            return row
    best = min(usable, key=lambda r: abs(float(r["points"]) - target))
    if max_line_delta is not None and abs(float(best["points"]) - target) > max_line_delta:
        return None
    return best


def pick_market_for_kind(
    event: dict[str, Any],
    kind: str,
    target_line: float | None = None,
    *,
    max_line_delta: float = LINE_MATCH_TOLERANCE,
    play_line: float | None = None,
    home_away: str | None = None,
) -> tuple[dict[str, Any] | None, str]:
    """Pick the nested Gamma market for `moneyline` / `spread` / `total`.

    Returns `(raw_market, detail)`. `detail` is empty on success; otherwise a
    skip reason (`no open spread`, `spread line mismatch play=21.5 poly=14.5`).
    """
    kind_l = (kind or "moneyline").strip().lower()
    classified = classify_event_markets(event)
    if kind_l == "moneyline":
        raw = classified["moneyline"] or pick_moneyline_market(event)
        if raw is None:
            return None, "no open moneyline"
        return raw, ""
    if kind_l == "spread":
        picked = pick_spread_market(
            classified["spreads"],
            target_home_line=target_line,
            max_line_delta=max_line_delta,
        )
        if picked is not None:
            return picked["raw"] if isinstance(picked.get("raw"), dict) else None, ""
        usable = [s for s in classified["spreads"] if (s.get("liquidity") or 0) > 0]
        if not usable:
            return None, "no open spread"
        if target_line is None:
            return None, "no open spread"
        best = min(
            usable,
            key=lambda r: abs(
                implied_home_spread(str(r["favored"]), float(r["points"])) - target_line
            ),
        )
        implied = implied_home_spread(str(best["favored"]), float(best["points"]))
        ha = (home_away or "").lower()
        poly_for_play = -implied if ha == "away" else implied
        shown_play = play_line if play_line is not None else target_line
        shown_poly = poly_for_play if play_line is not None else implied
        return None, f"spread line mismatch play={shown_play:g} poly={shown_poly:g}"
    if kind_l == "total":
        picked = pick_total_market(
            classified["totals"],
            target_line=target_line,
            max_line_delta=max_line_delta,
        )
        if picked is not None:
            return picked["raw"] if isinstance(picked.get("raw"), dict) else None, ""
        usable = [t for t in classified["totals"] if (t.get("liquidity") or 0) > 0]
        if not usable:
            return None, "no open total"
        if target_line is None:
            return None, "no open total"
        best = min(usable, key=lambda r: abs(float(r["points"]) - target_line))
        shown_play = play_line if play_line is not None else target_line
        return None, f"total line mismatch play={shown_play:g} poly={float(best['points']):g}"
    return None, f"unsupported market kind {kind!r}"


def iter_open_sports_markets(event: dict[str, Any]) -> list[tuple[str, dict[str, Any]]]:
    """Moneyline + liquid spreads + liquid totals nested under a sports event."""
    classified = classify_event_markets(event)
    out: list[tuple[str, dict[str, Any]]] = []
    ml = classified["moneyline"]
    if isinstance(ml, dict):
        out.append(("moneyline", ml))
    for row in classified["spreads"]:
        if (row.get("liquidity") or 0) > 0 and isinstance(row.get("raw"), dict):
            out.append(("spread", row["raw"]))
    for row in classified["totals"]:
        if (row.get("liquidity") or 0) > 0 and isinstance(row.get("raw"), dict):
            out.append(("total", row["raw"]))
    return out


def nested_event(raw_market: dict[str, Any]) -> dict[str, Any] | None:
    """First nested event on a Gamma market dict (used by metadata refresh)."""
    events = raw_market.get("events") or []
    if not events:
        return None
    first = events[0]
    return first if isinstance(first, dict) else None
