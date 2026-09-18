"""Scrape NFL game odds (spread, moneyline, totals) from RotoWire.

Source: https://www.rotowire.com/betting/nfl/odds

Covers full game, first half, second half, and all four quarters.
"""

from __future__ import annotations

import json
import re
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import requests

SCRIPT_DIR = Path(__file__).resolve().parent
OUTPUT_PATH = SCRIPT_DIR / "nfl_game_odds.json"

PAGE_URL = "https://www.rotowire.com/betting/nfl/odds"
DATA_URL = "https://www.rotowire.com/betting/nfl/tables/nfl-games-by-market.php"

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/112.0.0.0 Safari/537.36"
    ),
    "Referer": PAGE_URL,
    "X-Requested-With": "XMLHttpRequest",
}

WEEK_RE = re.compile(r"weekNFL\s*=\s*(\d+)")

PERIODS = (
    ("game", "Full Game", None),
    ("firsthalf", "First Half", "firsthalf"),
    ("secondhalf", "Second Half", "secondhalf"),
    ("firstquarter", "First Quarter", "firstquarter"),
    ("secondquarter", "Second Quarter", "secondquarter"),
    ("thirdquarter", "Third Quarter", "thirdquarter"),
    ("fourthquarter", "Fourth Quarter", "fourthquarter"),
)

TEAM_NAME_MAPPING = {
    "ARI": "Arizona Cardinals",
    "ATL": "Atlanta Falcons",
    "BAL": "Baltimore Ravens",
    "BUF": "Buffalo Bills",
    "CAR": "Carolina Panthers",
    "CHI": "Chicago Bears",
    "CIN": "Cincinnati Bengals",
    "CLE": "Cleveland Browns",
    "DAL": "Dallas Cowboys",
    "DEN": "Denver Broncos",
    "DET": "Detroit Lions",
    "GB": "Green Bay Packers",
    "HOU": "Houston Texans",
    "IND": "Indianapolis Colts",
    "JAC": "Jacksonville Jaguars",
    "JAX": "Jacksonville Jaguars",
    "KC": "Kansas City Chiefs",
    "LA": "Los Angeles Rams",
    "LAC": "Los Angeles Chargers",
    "LAR": "Los Angeles Rams",
    "LV": "Las Vegas Raiders",
    "LVR": "Las Vegas Raiders",
    "MIA": "Miami Dolphins",
    "MIN": "Minnesota Vikings",
    "NE": "New England Patriots",
    "NO": "New Orleans Saints",
    "NYG": "New York Giants",
    "NYJ": "New York Jets",
    "PHI": "Philadelphia Eagles",
    "PIT": "Pittsburgh Steelers",
    "SEA": "Seattle Seahawks",
    "SF": "San Francisco 49ers",
    "TB": "Tampa Bay Buccaneers",
    "TEN": "Tennessee Titans",
    "WAS": "Washington Commanders",
    "WSH": "Washington Commanders",
}


def normalize_team_name(team_name: str | None, abbr: str | None = None) -> str | None:
    if abbr:
        mapped = TEAM_NAME_MAPPING.get(abbr.strip())
        if mapped:
            return mapped
    if not team_name:
        return team_name
    return TEAM_NAME_MAPPING.get(team_name.strip(), team_name.strip())


def convert_float(value: Any) -> float | None:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def convert_int(value: Any) -> int | None:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def fetch_page() -> str:
    response = requests.get(PAGE_URL, headers=HEADERS, timeout=30)
    if response.status_code != 200:
        raise RuntimeError(
            f"Failed to fetch NFL odds page. Status code: {response.status_code}"
        )
    return response.text


def parse_week(html: str) -> int:
    match = WEEK_RE.search(html)
    if not match:
        print("Warning: weekNFL not found; defaulting to week 1.")
        return 1
    return int(match.group(1))


def fetch_period_rows(week: int, market: str | None) -> list[dict[str, Any]]:
    params: dict[str, str] = {"week": str(week)}
    if market:
        params["market"] = market
    response = requests.get(DATA_URL, headers=HEADERS, params=params, timeout=30)
    if response.status_code != 200:
        print(f"Warning: failed to fetch {market or 'game'} odds ({response.status_code}).")
        return []
    try:
        rows = response.json()
    except json.JSONDecodeError as exc:
        print(f"Warning: failed to decode {market or 'game'} odds: {exc}")
        return []
    if not isinstance(rows, list):
        print(f"Warning: {market or 'game'} odds was not a list.")
        return []
    return rows


def discover_books(item: dict[str, Any]) -> list[str]:
    books: set[str] = set()
    for key in item:
        if key.endswith("_has_spread"):
            book = key[: -len("_has_spread")]
            if book != "best":
                books.add(book)
        elif key.endswith("_spread") and not key.endswith("_spreadML"):
            book = key[: -len("_spread")]
            if book != "best":
                books.add(book)
    return sorted(books)


def parse_side_quotes(item: dict[str, Any], book: str) -> dict[str, Any] | None:
    spread = convert_float(item.get(f"{book}_spread"))
    spread_odds = convert_int(item.get(f"{book}_spreadML"))
    moneyline = convert_int(item.get(f"{book}_moneyline"))
    total = convert_float(item.get(f"{book}_ou"))
    total_odds = convert_int(item.get(f"{book}_ouML"))
    if all(value is None for value in (spread, spread_odds, moneyline, total, total_odds)):
        return None
    return {
        "spread": spread,
        "spreadOdds": spread_odds,
        "moneyline": moneyline,
        "total": total,
        "totalOdds": total_odds,
    }


def build_team(item: dict[str, Any]) -> dict[str, Any]:
    abbr = item.get("abbr")
    return {
        "team": normalize_team_name(item.get("name"), abbr),
        "abbr": abbr,
        "nickname": item.get("nickname"),
    }


def merge_book(
    away_quotes: dict[str, Any] | None,
    home_quotes: dict[str, Any] | None,
) -> dict[str, Any] | None:
    if away_quotes is None and home_quotes is None:
        return None

    away = away_quotes or {}
    home = home_quotes or {}
    book: dict[str, Any] = {}

    if any(away.get(k) is not None or home.get(k) is not None for k in ("spread", "spreadOdds")):
        book["spread"] = {
            "away": {"line": away.get("spread"), "odds": away.get("spreadOdds")},
            "home": {"line": home.get("spread"), "odds": home.get("spreadOdds")},
        }

    if away.get("moneyline") is not None or home.get("moneyline") is not None:
        book["moneyline"] = {
            "away": away.get("moneyline"),
            "home": home.get("moneyline"),
        }

    total_line = away.get("total") if away.get("total") is not None else home.get("total")
    over_odds = away.get("totalOdds")
    under_odds = home.get("totalOdds")
    if total_line is not None or over_odds is not None or under_odds is not None:
        book["overUnder"] = {
            "line": total_line,
            "overOdds": over_odds,
            "underOdds": under_odds,
        }

    return book or None


def build_game(away: dict[str, Any], home: dict[str, Any]) -> dict[str, Any]:
    books: dict[str, Any] = {}
    for book in sorted(set(discover_books(away)) | set(discover_books(home))):
        merged = merge_book(parse_side_quotes(away, book), parse_side_quotes(home, book))
        if merged is not None:
            books[book] = merged

    meta = home if home.get("homeAway") == "home" else away
    return {
        "gameID": meta.get("gameID") or away.get("gameID"),
        "gameDate": meta.get("gameDate"),
        "gameDay": meta.get("gameDay"),
        "gameDateTime": meta.get("gameDateTime"),
        "gameURL": meta.get("gameURL"),
        "away": build_team(away),
        "home": build_team(home),
        "books": books,
    }


def pair_games(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    by_game: dict[str, dict[str, dict[str, Any]]] = {}
    for item in rows:
        game_id = str(item.get("gameID") or "")
        if not game_id:
            continue
        side = str(item.get("homeAway") or "").lower()
        if side not in {"home", "away"}:
            continue
        by_game.setdefault(game_id, {})[side] = item

    games: list[dict[str, Any]] = []
    for game_id, sides in by_game.items():
        away = sides.get("away")
        home = sides.get("home")
        if away is None or home is None:
            print(f"Warning: game {game_id} missing home or away row.")
            continue
        games.append(build_game(away, home))

    games.sort(key=lambda game: (str(game.get("gameDate") or ""), str(game.get("gameID") or "")))
    return games


def scrape_all_periods(week: int) -> list[dict[str, Any]]:
    periods: list[dict[str, Any]] = []
    for period_id, period_name, market in PERIODS:
        rows = fetch_period_rows(week, market)
        games = pair_games(rows)
        with_odds = sum(1 for game in games if game["books"])
        periods.append(
            {
                "period": period_id,
                "name": period_name,
                "game_count": len(games),
                "games_with_odds": with_odds,
                "games": games,
            }
        )
        print(f"Scraped {period_name}: {len(games)} games ({with_odds} with odds)")
    return periods


def save_json(payload: dict[str, Any], path: Path = OUTPUT_PATH) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
    return path


def main() -> Path:
    html = fetch_page()
    week = parse_week(html)
    periods = scrape_all_periods(week)
    payload = {
        "source": PAGE_URL,
        "scraped_at": datetime.now(UTC).isoformat(),
        "week": week,
        "period_count": len(periods),
        "game_count": max((int(p["game_count"]) for p in periods), default=0),
        "periods": periods,
    }
    out = save_json(payload)
    print(f"Saved week {week} odds ({payload['period_count']} periods) to {out}")
    return out


if __name__ == "__main__":
    main()
