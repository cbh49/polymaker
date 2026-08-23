#!/usr/bin/env python3
"""
Scrape public betting splits (line movement + bet%/handle%) from PlayerProps.ai.

Source page (example):
  https://playerprops.ai/betting-splits/Baseball/2026-08-12/MLB

Data is loaded from their JSON API:
  /api/betprops/v2/betting-splits?dateFrom=...&dateTo=...

Sharp Money badge on the site is shown when:
  cardInfo.isValidSharpFavoredSide is true AND
  (handlePercent - betPercent) >= 25 for that side.

Usage:
  python scrape_playerprops_splits.py
  python scrape_playerprops_splits.py --date 2026-08-12
  python scrape_playerprops_splits.py --league MLB --out output/mlb_betting_splits.json
"""

from __future__ import annotations

import argparse
import json
import urllib.parse
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import requests

from mlb_team_map import DEFAULT_ABBREVS, DEFAULT_MATCHUPS, load_abbr_to_name, load_matchups

SCRIPT_DIR = Path(__file__).resolve().parent
DEFAULT_OUT = SCRIPT_DIR / "output" / "mlb_betting_splits.json"

API_URL = "https://playerprops.ai/api/betprops/v2/betting-splits"
PAGE_TZ = ZoneInfo("America/Los_Angeles")
SHARP_HANDLE_EDGE = 25

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/128.0.0.0 Safari/537.36"
    ),
    "Accept": "application/json",
}

# PlayerProps abbrs that differ from our mlbTeamAbbrevations.json keys.
PLAYERPROPS_ABBR_ALIASES: dict[str, str] = {
    "AZ": "Arizona Diamondbacks",
    "ARI": "Arizona Diamondbacks",
    "CWS": "Chicago White Sox",
    "CHW": "Chicago White Sox",
    "WSH": "Washington Nationals",
    "WAS": "Washington Nationals",
    "ATH": "Athletics",
    "OAK": "Athletics",
}


def pacific_day_window(day: date) -> tuple[str, str]:
    """Return UTC ISO dateFrom/dateTo matching PlayerProps' Pacific calendar day."""
    start_local = datetime(day.year, day.month, day.day, tzinfo=PAGE_TZ)
    end_local = start_local + timedelta(days=1)
    date_from = start_local.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.000Z")
    date_to = end_local.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.000Z")
    return date_from, date_to


def load_abbr_to_team(path: Path) -> dict[str, str]:
    mapping = load_abbr_to_name(path)
    mapping.update(PLAYERPROPS_ABBR_ALIASES)
    return mapping


def team_name_from_abbr(abbr: str, abbr_map: dict[str, str]) -> str | None:
    key = abbr.strip().upper()
    return abbr_map.get(key) or PLAYERPROPS_ABBR_ALIASES.get(key)


def match_matchup(
    away_name: str | None,
    home_name: str | None,
    matchups: list[dict[str, Any]],
) -> dict[str, Any] | None:
    if not away_name or not home_name:
        return None
    away_l, home_l = away_name.lower(), home_name.lower()
    for row in matchups:
        if str(row.get("away", "")).strip().lower() == away_l and str(
            row.get("home", "")
        ).strip().lower() == home_l:
            return row
    return None


def fetch_splits(day: date) -> dict[str, Any]:
    date_from, date_to = pacific_day_window(day)
    params = {"dateFrom": date_from, "dateTo": date_to}
    referer = (
        f"https://playerprops.ai/betting-splits/Baseball/{day.isoformat()}/MLB"
    )
    headers = {**HEADERS, "Referer": referer}
    # trust_env=False avoids broken corporate/sandbox HTTP(S)_PROXY tunnels.
    with requests.Session() as session:
        session.trust_env = False
        resp = session.get(API_URL, params=params, headers=headers, timeout=30)
    resp.raise_for_status()
    data = resp.json()
    if not isinstance(data, dict):
        raise ValueError("Unexpected betting-splits payload")
    return data


def side_label(market: str, side: str, team_name: str | None) -> str:
    if market == "TOTAL":
        return "Over" if side == "over" else "Under"
    return team_name or side


def is_sharp_money(card_market: dict[str, Any] | None, side: str) -> bool:
    """Mirror PlayerProps UI badge logic."""
    if not card_market or not card_market.get("isValidSharpFavoredSide"):
        return False
    side_data = card_market.get(side)
    if not isinstance(side_data, dict):
        return False
    bet = float(side_data.get("betPercent") or 0)
    handle = float(side_data.get("handlePercent") or 0)
    return (handle - bet) >= SHARP_HANDLE_EDGE


def _as_number(value: Any) -> float | None:
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, (int, float)):
        return float(value)
    try:
        return float(str(value).strip().replace("+", ""))
    except (TypeError, ValueError):
        return None


def american_odds_diff(opened: float, live: float) -> float:
    """Cents of American-odds movement, skipping the nonexistent -99..+99 band.

    Example: -108 → +103 is +11 (not +211), because odds jump from -100 to +100.
    """
    raw = live - opened
    if opened < 0 < live:
        return raw - 200
    if live < 0 < opened:
        return raw + 200
    return raw


def line_diff(opened: Any, live: Any, api_diff: Any = None) -> float | None:
    """Prefer a locally computed diff; fall back to the API value.

    Moneyline open/live are American odds (|x| >= 100). Spread/total are
    point numbers and use a plain live − open delta.
    """
    open_n = _as_number(opened)
    live_n = _as_number(live)
    if open_n is not None and live_n is not None:
        if abs(open_n) >= 100 and abs(live_n) >= 100:
            return american_odds_diff(open_n, live_n)
        return live_n - open_n
    return _as_number(api_diff)


def normalize_side(side_data: dict[str, Any] | None, sharp: bool) -> dict[str, Any] | None:
    if not isinstance(side_data, dict):
        return None
    opened = side_data.get("open")
    live = side_data.get("live")
    return {
        "open": opened,
        "live": live,
        "diff": line_diff(opened, live, side_data.get("difference")),
        "public_bet_pct": side_data.get("betPercent"),
        "handle_bet_pct": side_data.get("handlePercent"),
        "sharp_money": sharp,
    }


def event_home_away(event: dict[str, Any]) -> tuple[str | None, str | None]:
    home = away = None
    for key in ("team1", "team2"):
        team = event.get(key)
        if not isinstance(team, dict):
            continue
        name = team.get("teamName")
        side = (team.get("side") or "").lower()
        if side == "home":
            home = name
        elif side == "away":
            away = name
    if home is None or away is None:
        teams = event.get("teams") or []
        if isinstance(teams, list) and len(teams) >= 2:
            # Fallback: API often lists [home, away] for MLB cards.
            home = home or teams[0]
            away = away or teams[1]
    return away, home


def parse_event(
    event: dict[str, Any],
    abbr_map: dict[str, str],
    matchups: list[dict[str, Any]],
) -> dict[str, Any] | None:
    if event.get("league") != "MLB":
        return None

    away_abbr, home_abbr = event_home_away(event)
    if not away_abbr or not home_abbr:
        return None

    away_name = team_name_from_abbr(away_abbr, abbr_map)
    home_name = team_name_from_abbr(home_abbr, abbr_map)
    matched = match_matchup(away_name, home_name, matchups)

    card = event.get("cardInfo") if isinstance(event.get("cardInfo"), dict) else {}
    trends = event.get("TRENDS") if isinstance(event.get("TRENDS"), dict) else {}

    def market_sides(key: str) -> dict[str, Any]:
        """Prefer cardInfo (includes sharp flags); fall back to TRENDS."""
        from_card = card.get(key)
        if isinstance(from_card, dict) and any(
            isinstance(from_card.get(s), dict) for s in ("away", "home", "over", "under")
        ):
            return from_card
        from_trends = trends.get(key)
        return from_trends if isinstance(from_trends, dict) else {}

    money = market_sides("MONEYLINE")
    spread = market_sides("SPREAD")
    total = market_sides("TOTAL")

    moneyline = {
        "away": normalize_side(money.get("away"), is_sharp_money(money, "away")),
        "home": normalize_side(money.get("home"), is_sharp_money(money, "home")),
    }
    spread_out = {
        "away": normalize_side(spread.get("away"), is_sharp_money(spread, "away")),
        "home": normalize_side(spread.get("home"), is_sharp_money(spread, "home")),
    }
    total_out = {
        "over": normalize_side(total.get("over"), is_sharp_money(total, "over")),
        "under": normalize_side(total.get("under"), is_sharp_money(total, "under")),
    }
    # Attach selection labels for readability
    for side_key, abbr in (("away", away_abbr), ("home", home_abbr)):
        if moneyline.get(side_key):
            moneyline[side_key]["selection"] = side_label("MONEYLINE", side_key, abbr)
        if spread_out.get(side_key):
            spread_out[side_key]["selection"] = side_label("SPREAD", side_key, abbr)
    if total_out.get("over"):
        total_out["over"]["selection"] = "Over"
    if total_out.get("under"):
        total_out["under"]["selection"] = "Under"

    game: dict[str, Any] = {
        "matchup": f"{away_abbr} @ {home_abbr}",
        "away_abbr": away_abbr,
        "home_abbr": home_abbr,
        "away": away_name,
        "home": home_name,
        "game_time_utc": event.get("date"),
        "event_id": event.get("eventId") or event.get("id"),
        "moneyline": moneyline,
        "spread": spread_out,
        "total": total_out,
    }
    if matched:
        game["espn_game_id"] = matched.get("espn_game_id")
        game["game_time_local"] = matched.get("game_time")
        game["away_pitcher"] = matched.get("away_pitcher")
        game["home_pitcher"] = matched.get("home_pitcher")
    return game


def scrape(
    day: date,
    league: str = "MLB",
    matchups_path: Path = DEFAULT_MATCHUPS,
    abbrevs_path: Path = DEFAULT_ABBREVS,
) -> dict[str, Any]:
    abbr_map = load_abbr_to_team(abbrevs_path)
    matchups = load_matchups(matchups_path)

    payload = fetch_splits(day)
    events = payload.get("events") or []
    games: list[dict[str, Any]] = []
    for event in events:
        if not isinstance(event, dict):
            continue
        if league and event.get("league") != league:
            continue
        parsed = parse_event(event, abbr_map, matchups)
        if parsed:
            games.append(parsed)

    games.sort(key=lambda g: g.get("game_time_utc") or "")

    date_from, date_to = pacific_day_window(day)
    return {
        "source": "playerprops.ai",
        "source_page": f"https://playerprops.ai/betting-splits/Baseball/{day.isoformat()}/MLB",
        "api": f"{API_URL}?dateFrom={urllib.parse.quote(date_from)}&dateTo={urllib.parse.quote(date_to)}",
        "date": day.isoformat(),
        "league": league,
        "scraped_at": datetime.now(timezone.utc).isoformat(),
        "game_count": len(games),
        "games": games,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description="Scrape PlayerProps.ai betting splits")
    parser.add_argument(
        "--date",
        default=None,
        help="Slate date YYYY-MM-DD in America/Los_Angeles (default: today Pacific)",
    )
    parser.add_argument("--league", default="MLB", help="League filter (default: MLB)")
    parser.add_argument(
        "--matchups",
        type=Path,
        default=DEFAULT_MATCHUPS,
        help="Path to MLB matchups.json for joining full team names / espn ids",
    )
    parser.add_argument(
        "--abbrevs",
        type=Path,
        default=DEFAULT_ABBREVS,
        help="Path to MLB abbreviation map JSON",
    )
    parser.add_argument(
        "--out",
        type=Path,
        default=DEFAULT_OUT,
        help="Output JSON path",
    )
    args = parser.parse_args()

    if args.date:
        day = date.fromisoformat(args.date)
    else:
        day = datetime.now(PAGE_TZ).date()

    result = scrape(
        day=day,
        league=args.league,
        matchups_path=args.matchups,
        abbrevs_path=args.abbrevs,
    )

    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(result, indent=2) + "\n", encoding="utf-8")
    print(f"Wrote {result['game_count']} {args.league} games → {args.out}")


if __name__ == "__main__":
    main()
