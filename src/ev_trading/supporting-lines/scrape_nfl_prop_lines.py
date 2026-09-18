"""Scrape all NFL player prop lines from RotoWire.

Source: https://www.rotowire.com/betting/nfl/player-props.php
"""

from __future__ import annotations

import json
import re
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import requests

SCRIPT_DIR = Path(__file__).resolve().parent
OUTPUT_PATH = SCRIPT_DIR / "nfl_player_props.json"

URL = "https://www.rotowire.com/betting/nfl/player-props.php"
HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/112.0.0.0 Safari/537.36"
    )
}

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

PROP_BLOCK_RE = re.compile(
    r'prop\s*=\s*"([a-z0-9]+)"\s*;\s*const propName\s*=\s*"([^"]+)"',
)


def normalize_team_name(team_name: str | None) -> str | None:
    if not team_name:
        return team_name
    cleaned = team_name.replace("@", "").strip()
    return TEAM_NAME_MAPPING.get(cleaned, cleaned)


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
    response = requests.get(URL, headers=HEADERS, timeout=30)
    if response.status_code != 200:
        raise RuntimeError(
            f"Failed to fetch NFL player props. Status code: {response.status_code}"
        )
    return response.text


def discover_props(html: str) -> list[tuple[str, str]]:
    seen: set[str] = set()
    props: list[tuple[str, str]] = []
    for prop_id, prop_name in PROP_BLOCK_RE.findall(html):
        if prop_id in seen:
            continue
        seen.add(prop_id)
        props.append((prop_id, prop_name))
    return props


def _extract_json_array(text: str, start: int) -> str:
    i = text.find("[", start)
    if i < 0:
        raise ValueError("no JSON array found")

    depth = 0
    in_str = False
    escape = False
    for j, ch in enumerate(text[i:], start=i):
        if in_str:
            if escape:
                escape = False
            elif ch == "\\":
                escape = True
            elif ch == '"':
                in_str = False
            continue
        if ch == '"':
            in_str = True
        elif ch == "[":
            depth += 1
        elif ch == "]":
            depth -= 1
            if depth == 0:
                return text[i : j + 1]
    raise ValueError("unterminated JSON array")


def extract_prop_rows(html: str, prop_id: str) -> list[dict[str, Any]]:
    marker = f'prop = "{prop_id}"'
    start = html.find(marker)
    if start < 0:
        print(f"Warning: {prop_id} block not found.")
        return []

    next_prop = html.find('prop = "', start + len(marker))
    block_end = next_prop if next_prop > 0 else len(html)
    data_idx = html.find("data:", start, block_end)
    if data_idx < 0:
        print(f"Warning: no data array found for {prop_id}.")
        return []

    try:
        raw = _extract_json_array(html, data_idx)
        rows = json.loads(raw)
    except (ValueError, json.JSONDecodeError) as exc:
        print(f"Warning: failed to decode {prop_id} data: {exc}")
        return []

    if not isinstance(rows, list):
        print(f"Warning: {prop_id} data was not a list.")
        return []
    return rows


def discover_books(item: dict[str, Any], prop_id: str) -> list[str]:
    suffix = f"_{prop_id}"
    books: set[str] = set()
    for key in item:
        if key.endswith(f"{suffix}Over"):
            books.add(key[: -len(f"{suffix}Over")])
        elif key.endswith(f"{suffix}Under"):
            books.add(key[: -len(f"{suffix}Under")])
        elif key.endswith(suffix):
            books.add(key[: -len(suffix)])
    return sorted(books)


def parse_book_quote(item: dict[str, Any], book: str, prop_id: str) -> dict[str, Any] | None:
    raw_value = item.get(f"{book}_{prop_id}")
    raw_over = item.get(f"{book}_{prop_id}Over")
    raw_under = item.get(f"{book}_{prop_id}Under")

    blank = (None, "")
    if raw_value in blank and raw_over in blank and raw_under in blank:
        return None

    over_odds = convert_int(raw_over)
    under_odds = convert_int(raw_under)
    if over_odds is not None or under_odds is not None:
        return {
            "line": convert_float(raw_value),
            "overOdds": over_odds,
            "underOdds": under_odds,
        }

    as_float = convert_float(raw_value)
    as_int = convert_int(raw_value)
    if as_int is not None and (as_float is None or float(as_int) == as_float):
        return {"odds": as_int}
    if as_float is not None:
        return {"line": as_float, "overOdds": None, "underOdds": None}
    return None


def build_player(item: dict[str, Any], prop_id: str) -> dict[str, Any]:
    books: dict[str, Any] = {}
    for book in discover_books(item, prop_id):
        quote = parse_book_quote(item, book, prop_id)
        if quote is not None:
            books[book] = quote

    return {
        "name": item.get("name"),
        "firstName": item.get("firstName"),
        "lastName": item.get("lastName"),
        "team": normalize_team_name(item.get("team")),
        "opponent": normalize_team_name(item.get("opp", "")),
        "playerID": item.get("playerID"),
        "gameID": item.get("gameID"),
        "playerLink": item.get("playerLink"),
        "books": books,
    }


def scrape_all_props(html: str) -> list[dict[str, Any]]:
    markets: list[dict[str, Any]] = []
    for prop_id, prop_name in discover_props(html):
        rows = extract_prop_rows(html, prop_id)
        players = [build_player(item, prop_id) for item in rows if item.get("name")]
        markets.append(
            {
                "prop": prop_id,
                "name": prop_name,
                "count": len(players),
                "players": players,
            }
        )
        print(f"Scraped {len(players)} {prop_name} line(s)")
    return markets


def save_json(payload: dict[str, Any], path: Path = OUTPUT_PATH) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
    return path


def main() -> Path:
    html = fetch_page()
    markets = scrape_all_props(html)
    payload = {
        "source": URL,
        "scraped_at": datetime.now(UTC).isoformat(),
        "market_count": len(markets),
        "player_row_count": sum(int(m["count"]) for m in markets),
        "markets": markets,
    }
    out = save_json(payload)
    print(
        f"Saved {payload['market_count']} markets "
        f"({payload['player_row_count']} player rows) to {out}"
    )
    return out


if __name__ == "__main__":
    main()
