#!/usr/bin/env python3
"""
Scrape prediction-market odds from Covers.com (MLB, WNBA, and NCAAF).

Source pages:
  MLB:   https://www.covers.com/sport/baseball/mlb/odds
  WNBA:  https://www.covers.com/sport/basketball/wnba/odds
  NCAAF: https://www.covers.com/sport/football/ncaaf/odds

MLB/WNBA keep rows whose Time (ET) label is "Today". NCAAF keeps upcoming
weekend rows (not Final). Moneyline, spread, and total are read from the
matching tab panes. Columns are prediction markets (Polymarket, ProphetX,
Novig, OG, Crypto.com, DK Predictions) when present.

Usage:
  python scrape_covers_odds.py
  python scrape_covers_odds.py --league WNBA --out output/covers_wnba_odds.json
"""

from __future__ import annotations

import argparse
import json
import re
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import requests
from bs4 import BeautifulSoup, NavigableString

from mlb_team_map import DEFAULT_ABBREVS, load_abbr_to_name

SCRIPT_DIR = Path(__file__).resolve().parent
DEFAULT_OUT = {
    "MLB": SCRIPT_DIR / "output" / "covers_mlb_odds.json",
    "WNBA": SCRIPT_DIR / "output" / "covers_wnba_odds.json",
    "NCAAF": SCRIPT_DIR / "output" / "covers_ncaaf_odds.json",
}
PAGE_URLS = {
    "MLB": "https://www.covers.com/sport/baseball/mlb/odds",
    "WNBA": "https://www.covers.com/sport/basketball/wnba/odds",
    "NCAAF": "https://www.covers.com/sport/football/ncaaf/odds",
}
PAGE_TZ = ZoneInfo("America/New_York")
MARKET_PANES = (
    ("tab-moneyline", "moneyline"),
    ("tab-spread", "spread"),
    ("tab-total", "total"),
)
PREDICTION_BOOKS = {
    "polymarket",
    "prophetx",
    "novig",
    "og",
    "crypto.com",
    "cryptocom",
    "draftkings predictions",
    "kalshi",
    "rolr",
    "fanduel predicts",
    "fanatics markets",
}
BOOK_SLUGS = {
    "polymarket": "polymarket",
    "prophetx": "prophetx",
    "novig": "novig",
    "og": "og",
    "crypto.com": "crypto_com",
    "cryptocom": "crypto_com",
    "draftkings predictions": "dk_predictions",
    "kalshi": "kalshi",
    "rolr": "rolr",
    "fanduel predicts": "fd_predicts",
    "fanatics markets": "fanatics",
}
ABBR_ALIASES: dict[str, str] = {
    "AZ": "Arizona Diamondbacks",
    "ARI": "Arizona Diamondbacks",
    "CWS": "Chicago White Sox",
    "CHW": "Chicago White Sox",
    "WSH": "Washington Nationals",
    "WAS": "Washington Nationals",
    "ATH": "Athletics",
    "OAK": "Athletics",
}

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/128.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml;q=0.9",
}


def load_abbr_to_team(path: Path) -> dict[str, str]:
    mapping = load_abbr_to_name(path)
    mapping.update(ABBR_ALIASES)
    return mapping


def _parse_number(value: Any) -> float | int | None:
    if value is None or value == "":
        return None
    if isinstance(value, (int, float)):
        num = float(value)
        return int(num) if num.is_integer() else num
    text = str(value).strip().replace("+", "").replace(",", "")
    text = re.sub(r"^[ou]\s*", "", text, flags=re.I)
    try:
        num = float(text)
        return int(num) if num.is_integer() else num
    except ValueError:
        return None


def _is_prediction_book(name: str) -> bool:
    key = (name or "").strip().lower()
    if key in PREDICTION_BOOKS:
        return True
    return any(token in key for token in ("predict", "kalshi", "polymarket", "novig", "prophet", "rolr"))


def _book_slug(name: str) -> str:
    key = (name or "").strip().lower()
    if key in BOOK_SLUGS:
        return BOOK_SLUGS[key]
    return re.sub(r"[^a-z0-9]+", "_", key).strip("_") or "book"


def _canonical_team(abbr: str, league: str, abbr_map: dict[str, str]) -> tuple[str, str | None]:
    raw = (abbr or "").strip().upper()
    if league == "WNBA":
        from wnba_team_map import canonical_abbr, canonical_name

        canon_abbr = canonical_abbr(raw) or raw
        return canon_abbr, canonical_name(canon_abbr) or canonical_name(raw)
    if league == "NCAAF":
        from cfb_team_map import canonical_abbr, canonical_name

        canon_abbr = canonical_abbr(raw) or raw
        return canon_abbr, canonical_name(canon_abbr) or canonical_name(raw)
    return raw, abbr_map.get(raw)


def fetch_html(url: str) -> str:
    headers = {**HEADERS, "Referer": url}
    with requests.Session() as session:
        session.trust_env = False
        resp = session.get(url, headers=headers, timeout=30)
    resp.raise_for_status()
    return resp.text


def _row_is_today(row: Any) -> bool:
    cell = row.select_one(".game-time")
    if not cell:
        return False
    spans = [s.get_text(strip=True).rstrip(",") for s in cell.find_all("span")]
    label = spans[0] if spans else cell.get_text(" ", strip=True).split(",")[0]
    return label.lower() == "today"


def _row_is_upcoming(row: Any) -> bool:
    cell = row.select_one(".game-time")
    if not cell:
        return False
    text = cell.get_text(" ", strip=True).lower()
    if "final" in text or "postponed" in text:
        return False
    return True


def _row_date_label(row: Any) -> str | None:
    cell = row.select_one(".game-time")
    if not cell:
        return None
    spans = [s.get_text(strip=True).rstrip(",") for s in cell.find_all("span")]
    return spans[0] if spans else None


_MONTHS = {
    "jan": 1,
    "feb": 2,
    "mar": 3,
    "apr": 4,
    "may": 5,
    "jun": 6,
    "jul": 7,
    "aug": 8,
    "sep": 9,
    "oct": 10,
    "nov": 11,
    "dec": 12,
}


def _parse_covers_date(label: str | None, year: int) -> date | None:
    text = (label or "").strip()
    if text.lower() == "today":
        return None
    m = re.search(r"(jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)[a-z]*\s+(\d{1,2})", text, re.I)
    if not m:
        return None
    try:
        return date(year, _MONTHS[m.group(1).lower()[:3]], int(m.group(2)))
    except (KeyError, ValueError):
        return None


def _row_time(row: Any) -> str | None:
    cell = row.select_one(".game-time")
    if not cell:
        return None
    spans = [s.get_text(strip=True).rstrip(",") for s in cell.find_all("span")]
    if len(spans) >= 2:
        return spans[1]
    text = cell.get_text(" ", strip=True)
    m = re.search(r"(\d{1,2}:\d{2}(?:\s*[AP]M)?)", text, re.I)
    return m.group(1) if m else None


def _row_teams(row: Any) -> tuple[str | None, str | None]:
    away = row.select_one(".odds-team-and-actions-teams .away-cell strong")
    home = row.select_one(".odds-team-and-actions-teams .home-cell strong")
    away_abbr = away.get_text(strip=True).upper() if away else None
    home_abbr = home.get_text(strip=True).upper() if home else None
    return away_abbr, home_abbr


def _row_game_id(row: Any) -> str | None:
    cell = row.select_one("td.liveOddsCell[data-game]")
    if cell and cell.get("data-game"):
        return str(cell.get("data-game"))
    btn = row.select_one("[data-game]")
    return str(btn.get("data-game")) if btn and btn.get("data-game") else None


def _direct_line_text(anchor: Any) -> str:
    parts: list[str] = []
    if anchor is None:
        return ""
    for child in anchor.children:
        if isinstance(child, NavigableString):
            text = str(child).strip()
            if text:
                parts.append(text)
    return " ".join(parts)


def _parse_side(cell: Any, market: str) -> dict[str, Any] | None:
    if cell is None:
        return None
    american_el = cell.select_one("span.American")
    odds = _parse_number(american_el.get_text(strip=True) if american_el else None)
    line_text = _direct_line_text(cell.find("a") or cell)
    if market == "moneyline":
        return {"line": odds} if odds is not None else None
    line = _parse_number(line_text)
    if line is None and odds is None:
        return None
    out: dict[str, Any] = {}
    if line is not None:
        out["line"] = line
    if odds is not None:
        out["odds"] = odds
    return out or None


def _parse_book_cell(td: Any, market: str) -> dict[str, Any] | None:
    away = _parse_side(td.select_one(".away-cell"), market)
    home = _parse_side(td.select_one(".home-cell"), market)
    if not away and not home:
        return None
    if market == "total":
        return {"over": away, "under": home}
    return {"away": away, "home": home}


def parse_today_games(html: str, league: str, abbr_map: dict[str, str], day: date) -> list[dict[str, Any]]:
    soup = BeautifulSoup(html, "html.parser")
    by_id: dict[str, dict[str, Any]] = {}

    for pane_id, market in MARKET_PANES:
        pane = soup.select_one(f"#{pane_id}")
        if pane is None:
            continue
        for row in pane.select("tr.oddsGameRow"):
            keep = _row_is_upcoming(row) if league == "NCAAF" else _row_is_today(row)
            if not keep:
                continue
            game_id = _row_game_id(row)
            away_abbr_raw, home_abbr_raw = _row_teams(row)
            if not game_id or not away_abbr_raw or not home_abbr_raw:
                continue
            game = by_id.get(game_id)
            if game is None:
                away_abbr, away_name = _canonical_team(away_abbr_raw, league, abbr_map)
                home_abbr, home_name = _canonical_team(home_abbr_raw, league, abbr_map)
                row_day = _parse_covers_date(_row_date_label(row), day.year) or day
                game = {
                    "matchup": f"{away_abbr} @ {home_abbr}",
                    "away_abbr": away_abbr,
                    "home_abbr": home_abbr,
                    "away": away_name,
                    "home": home_name,
                    "date": row_day.isoformat(),
                    "game_time_et": _row_time(row),
                    "covers_game_id": game_id,
                    "covers_odds": {},
                }
                by_id[game_id] = game
            for td in row.select("td.liveOddsCell[data-book]"):
                book_name = (td.get("data-book") or "").strip()
                if not _is_prediction_book(book_name):
                    continue
                parsed = _parse_book_cell(td, market)
                if not parsed:
                    continue
                slug = _book_slug(book_name)
                book = game["covers_odds"].setdefault(
                    slug, {"book": book_name}
                )
                book[market] = parsed

    games = list(by_id.values())
    games.sort(key=lambda g: (g.get("game_time_et") or "", g.get("matchup") or ""))
    return games


def scrape(
    league: str = "MLB",
    day: date | None = None,
    abbrevs_path: Path = DEFAULT_ABBREVS,
) -> dict[str, Any]:
    league = (league or "MLB").strip().upper()
    if league == "CFB":
        league = "NCAAF"
    if league not in PAGE_URLS:
        raise ValueError(f"Unsupported Covers league: {league}")
    et_today = datetime.now(PAGE_TZ).date()
    page_url = PAGE_URLS[league]
    abbr_map = load_abbr_to_team(abbrevs_path) if league == "MLB" else {}
    html = fetch_html(page_url)
    games = parse_today_games(html, league, abbr_map, et_today)
    if day is not None and league != "NCAAF" and day != et_today:
        games = []
    return {
        "source": "covers.com",
        "source_page": page_url,
        "date": et_today.isoformat(),
        "league": league,
        "scraped_at": datetime.now(timezone.utc).isoformat(),
        "game_count": len(games),
        "books": sorted(
            {
                book.get("book")
                for game in games
                for book in (game.get("covers_odds") or {}).values()
                if isinstance(book, dict) and book.get("book")
            }
        ),
        "games": games,
    }


def merge_covers_into_game(game: dict[str, Any], covers_game: dict[str, Any]) -> None:
    """Copy prediction-market odds onto a combined splits game."""
    if covers_game.get("covers_odds"):
        game["covers_odds"] = covers_game["covers_odds"]
    if covers_game.get("covers_game_id"):
        game["covers_game_id"] = covers_game["covers_game_id"]
    if covers_game.get("game_time_et") and not game.get("game_time_et"):
        game["game_time_et"] = covers_game["game_time_et"]
    if covers_game.get("date"):
        game["covers_date"] = covers_game["date"]


def main() -> None:
    parser = argparse.ArgumentParser(description="Scrape Covers.com prediction-market odds")
    parser.add_argument("--league", default="MLB", choices=["MLB", "WNBA", "NCAAF", "CFB"])
    parser.add_argument(
        "--date",
        default=None,
        help="Keep Today rows only if this YYYY-MM-DD matches Eastern today",
    )
    parser.add_argument("--abbrevs", type=Path, default=DEFAULT_ABBREVS)
    parser.add_argument("--out", type=Path, default=None)
    args = parser.parse_args()

    day = date.fromisoformat(args.date) if args.date else None
    result = scrape(league=args.league, day=day, abbrevs_path=args.abbrevs)
    out = args.out or DEFAULT_OUT[result["league"]]
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(result, indent=2) + "\n", encoding="utf-8")
    print(f"Wrote {result['game_count']} {args.league} Today games → {out}")


if __name__ == "__main__":
    main()
