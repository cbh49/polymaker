#!/usr/bin/env python3
"""
Scrape betting splits from VSiN (MLB, WNBA, or UFC).

Sources:
  MLB:  https://data.vsin.com/betting-splits/?bookid=dk&view=mlb
  WNBA: https://data.vsin.com/betting-splits/?source=DK&sport=WNBA
  UFC:  https://data.vsin.com/betting-splits/?source=DK&sport=UFC

Table columns per team/fighter row:
  Spread LINE | HANDLE | BETS | Total LINE | HANDLE | BETS | Money LINE | HANDLE | BETS

HANDLE -> vsin_handle_bet_pct
BETS   -> vsin_public_bet_pct
LINE   -> vsin_line

UFC is moneyline-first (spread/total are usually blank) and has no team
hrefs — names come from td.sp-cell-team. Duplicate listings of the same
bout (swapped corners) are dropped.

Usage:
  python scrape_vsin_splits.py
  python scrape_vsin_splits.py --league WNBA --out output/vsin_wnba_betting_splits.json
  python scrape_vsin_splits.py --league UFC --out output/vsin_ufc_betting_splits.json
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
from bs4 import BeautifulSoup

from mlb_team_map import DEFAULT_ABBREVS
from mlb_team_map import DEFAULT_MATCHUPS
from mlb_team_map import load_abbr_maps
from mlb_team_map import load_matchups

SCRIPT_DIR = Path(__file__).resolve().parent
DEFAULT_OUT = SCRIPT_DIR / "output" / "vsin_betting_splits.json"
DEFAULT_WNBA_OUT = SCRIPT_DIR / "output" / "vsin_wnba_betting_splits.json"
DEFAULT_UFC_OUT = SCRIPT_DIR / "output" / "vsin_ufc_betting_splits.json"

PAGE_URLS = {
    "MLB": "https://data.vsin.com/betting-splits/?bookid=dk&view=mlb",
    "WNBA": "https://data.vsin.com/betting-splits/?source=DK&sport=WNBA",
    "UFC": "https://data.vsin.com/betting-splits/?source=DK&sport=UFC",
}
PAGE_URL = PAGE_URLS["MLB"]
TEAM_HREF = {
    "MLB": r"/mlb/teams/",
    "WNBA": r"/wnba/teams/",
    "UFC": r"/ufc/teams/",
}
PAGE_TZ = ZoneInfo("America/Los_Angeles")

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/112.0.0.0 Safari/537.36"
    ),
}

# VSIN naming quirks -> our matchups.json names
TEAM_NAME_ALIASES: dict[str, str] = {
    "st louis cardinals": "St. Louis Cardinals",
    "st. louis cardinals": "St. Louis Cardinals",
    "athletics": "Athletics",
    "oakland athletics": "Athletics",
    "arizona diamondbacks": "Arizona Diamondbacks",
    "chicago white sox": "Chicago White Sox",
    "washington nationals": "Washington Nationals",
}


def normalize_team_name(name: str) -> str:
    key = re.sub(r"\s+", " ", name.strip().lower())
    if key in TEAM_NAME_ALIASES:
        return TEAM_NAME_ALIASES[key]
    # Title-case fallback; keep common MLB casing via aliases above.
    return name.strip()


def match_matchup(
    away_name: str,
    home_name: str,
    matchups: list[dict[str, Any]],
) -> dict[str, Any] | None:
    away_l, home_l = away_name.lower(), home_name.lower()
    for row in matchups:
        if str(row.get("away", "")).strip().lower() == away_l and str(
            row.get("home", "")
        ).strip().lower() == home_l:
            return row
    return None


def parse_pct(text: str) -> int | None:
    cleaned = re.sub(r"[▲▼]", "", text or "")
    m = re.search(r"(-?\d+(?:\.\d+)?)\s*%", cleaned)
    if not m:
        return None
    return int(round(float(m.group(1))))


def parse_line(text: str) -> float | int | str | None:
    cleaned = re.sub(r"[▲▼↺]", "", text or "").strip()
    cleaned = cleaned.replace("\xa0", " ")
    if not cleaned or cleaned in {"-", "—"}:
        return None
    m = re.search(r"([+-]?\d+(?:\.\d+)?)", cleaned.replace(",", ""))
    if not m:
        return cleaned
    num = float(m.group(1))
    # Preserve leading + for american odds only when original had it and value is odds-like.
    if num.is_integer():
        return int(num)
    return num


def side_payload(line: Any, handle: int | None, bets: int | None, selection: str) -> dict[str, Any]:
    return {
        "selection": selection,
        "vsin_line": line,
        "vsin_handle_bet_pct": handle,
        "vsin_public_bet_pct": bets,
    }


def fetch_html(url: str = PAGE_URL) -> str:
    with requests.Session() as session:
        session.trust_env = False
        resp = session.get(url, headers=HEADERS, timeout=30)
    resp.raise_for_status()
    try:
        return resp.content.decode("utf-8")
    except UnicodeDecodeError:
        return resp.content.decode("latin-1")


def _header_gamedate(row: Any) -> date | None:
    classes = " ".join(row.get("class") or [])
    if "sp-sport-header" not in classes:
        return None
    link = row.find("a", href=re.compile(r"gamedate="))
    if not link:
        return None
    href = link.get("href") or ""
    m = re.search(r"gamedate=(\d{4}-\d{2}-\d{2})", href)
    if not m:
        return None
    return date.fromisoformat(m.group(1))


def parse_games(
    html: str,
    abbr_to_name: dict[str, str],
    name_to_abbr: dict[str, str],
    matchups: list[dict[str, Any]],
    team_href: str = TEAM_HREF["MLB"],
    day: date | None = None,
    normalize_name_fn=normalize_team_name,
    fighter_mode: bool = False,
) -> list[dict[str, Any]]:
    soup = BeautifulSoup(html, "html.parser")
    rows = soup.find_all("tr")
    games: list[dict[str, Any]] = []
    section_day: date | None = None
    href_re = re.compile(team_href)
    seen_pairs: set[tuple[str, str]] = set()

    i = 0
    while i + 1 < len(rows):
        header_day = _header_gamedate(rows[i])
        if header_day is not None:
            section_day = header_day
            i += 1
            continue

        r1, r2 = rows[i], rows[i + 1]
        tds1 = r1.find_all("td")
        tds2 = r2.find_all("td")

        def team_from_row(row: Any) -> str | None:
            if fighter_mode:
                cell = row.select_one("td.sp-cell-team")
                if not cell:
                    return None
                return normalize_name_fn(cell.get_text(" ", strip=True))
            a = row.find("a", href=href_re)
            if not a:
                return None
            return normalize_name_fn(a.get_text(strip=True))

        away_name = team_from_row(r1)
        home_name = team_from_row(r2)
        if not away_name or not home_name or len(tds1) < 11 or len(tds2) < 11:
            i += 1
            continue
        if day is not None and section_day is not None and section_day != day:
            i += 2
            continue
        if fighter_mode:
            from ufc_fighter_map import pair_key

            key = pair_key(away_name, home_name)
            if key is None:
                i += 2
                continue
            if key in seen_pairs:
                games[:] = [
                    g for g in games if pair_key(g.get("away"), g.get("home")) != key
                ]
            seen_pairs.add(key)

        a = [td.get_text(" ", strip=True) for td in tds1]
        h = [td.get_text(" ", strip=True) for td in tds2]

        away_abbr = name_to_abbr.get(away_name.lower())
        home_abbr = name_to_abbr.get(home_name.lower())
        # Fallback: try resolving through abbr_to_name values.
        if not away_abbr:
            for abbr, name in abbr_to_name.items():
                if name.lower() == away_name.lower():
                    away_abbr = abbr
                    break
        if not home_abbr:
            for abbr, name in abbr_to_name.items():
                if name.lower() == home_name.lower():
                    home_abbr = abbr
                    break
        away_abbr = away_abbr or away_name
        home_abbr = home_abbr or home_name

        moneyline = {
            "away": side_payload(parse_line(a[8]), parse_pct(a[9]), parse_pct(a[10]), away_abbr),
            "home": side_payload(parse_line(h[8]), parse_pct(h[9]), parse_pct(h[10]), home_abbr),
        }
        spread = {
            "away": side_payload(parse_line(a[2]), parse_pct(a[3]), parse_pct(a[4]), away_abbr),
            "home": side_payload(parse_line(h[2]), parse_pct(h[3]), parse_pct(h[4]), home_abbr),
        }
        # Away row carries Over; home row carries Under.
        total = {
            "over": side_payload(parse_line(a[5]), parse_pct(a[6]), parse_pct(a[7]), "Over"),
            "under": side_payload(parse_line(h[5]), parse_pct(h[6]), parse_pct(h[7]), "Under"),
        }

        matched = match_matchup(away_name, home_name, matchups)
        if fighter_mode:
            matchup = f"{away_name} vs {home_name}"
            away_abbr = away_name
            home_abbr = home_name
        else:
            matchup = f"{away_abbr} @ {home_abbr}"
        game: dict[str, Any] = {
            "matchup": matchup,
            "away_abbr": away_abbr,
            "home_abbr": home_abbr,
            "away": away_name,
            "home": home_name,
            "moneyline": moneyline,
            "spread": spread,
            "total": total,
        }
        if section_day is not None:
            game["date"] = section_day.isoformat()
        hist = r1.select_one("[data-gamecode]")
        if hist and hist.get("data-gamecode"):
            game["vsin_gamecode"] = hist.get("data-gamecode")
        if matched:
            game["espn_game_id"] = matched.get("espn_game_id")
            game["game_time_local"] = matched.get("game_time")
            game["away_pitcher"] = matched.get("away_pitcher")
            game["home_pitcher"] = matched.get("home_pitcher")
        games.append(game)
        i += 2

    return games


def _wnba_normalize(name: str) -> str:
    from wnba_team_map import canonical_name

    return canonical_name(name) or name.strip()


def _ufc_normalize(name: str) -> str:
    from ufc_fighter_map import canonical_name

    return canonical_name(name) or name.strip()


def scrape(
    matchups_path: Path | None = None,
    abbrevs_path: Path = DEFAULT_ABBREVS,
    url: str | None = None,
    league: str = "MLB",
    day: date | None = None,
) -> dict[str, Any]:
    league = (league or "MLB").upper()
    day = day or datetime.now(PAGE_TZ).date()
    page_url = url or PAGE_URLS.get(league, PAGE_URL)
    team_href = TEAM_HREF.get(league, TEAM_HREF["MLB"])

    fighter_mode = league == "UFC"
    if league == "WNBA":
        from wnba_team_map import ABBR_TO_NAME, NAME_TO_ABBR
        from wnba_team_map import DEFAULT_MATCHUPS as WNBA_MATCHUPS

        abbr_to_name = dict(ABBR_TO_NAME)
        name_to_abbr = dict(NAME_TO_ABBR)
        matchups = load_matchups(matchups_path or WNBA_MATCHUPS)
        normalize_name_fn = _wnba_normalize
        filter_day = day
    elif fighter_mode:
        abbr_to_name = {}
        name_to_abbr = {}
        matchups = []
        normalize_name_fn = _ufc_normalize
        filter_day = day
    else:
        abbr_to_name, name_to_abbr = load_abbr_maps(abbrevs_path)
        matchups = load_matchups(matchups_path or DEFAULT_MATCHUPS)
        normalize_name_fn = normalize_team_name
        filter_day = day

    html = fetch_html(page_url)
    games = parse_games(
        html,
        abbr_to_name,
        name_to_abbr,
        matchups,
        team_href=team_href,
        day=filter_day,
        normalize_name_fn=normalize_name_fn,
        fighter_mode=fighter_mode,
    )
    return {
        "source": "data.vsin.com",
        "source_page": page_url,
        "date": day.isoformat(),
        "league": league,
        "scraped_at": datetime.now(timezone.utc).isoformat(),
        "game_count": len(games),
        "games": games,
    }


def merge_vsin_into_game(game: dict[str, Any], vsin_game: dict[str, Any]) -> None:
    """Copy vsin_* fields onto matching side objects."""
    for market in ("moneyline", "spread", "total"):
        src_market = vsin_game.get(market)
        if not isinstance(src_market, dict):
            continue
        dst_market = game.get(market)
        if not isinstance(dst_market, dict):
            dst_market = {}
            game[market] = dst_market
        for side, src_side in src_market.items():
            if not isinstance(src_side, dict):
                continue
            dst_side = dst_market.get(side)
            if not isinstance(dst_side, dict):
                dst_side = {"selection": src_side.get("selection")}
                dst_market[side] = dst_side
            for key, value in src_side.items():
                if key == "selection":
                    continue
                if key.startswith("vsin_"):
                    dst_side[key] = value


def main() -> None:
    parser = argparse.ArgumentParser(description="Scrape VSiN betting splits")
    parser.add_argument("--league", default="MLB", choices=["MLB", "WNBA", "UFC"])
    parser.add_argument(
        "--date",
        default=None,
        help="Slate date YYYY-MM-DD (WNBA filters to this day; default today Pacific)",
    )
    parser.add_argument("--matchups", type=Path, default=None)
    parser.add_argument("--abbrevs", type=Path, default=DEFAULT_ABBREVS)
    parser.add_argument("--out", type=Path, default=None)
    parser.add_argument("--url", default=None)
    args = parser.parse_args()

    day = date.fromisoformat(args.date) if args.date else datetime.now(PAGE_TZ).date()
    out = args.out or {
        "WNBA": DEFAULT_WNBA_OUT,
        "UFC": DEFAULT_UFC_OUT,
    }.get(args.league, DEFAULT_OUT)
    result = scrape(
        matchups_path=args.matchups,
        abbrevs_path=args.abbrevs,
        url=args.url,
        league=args.league,
        day=day,
    )
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(result, indent=2) + "\n", encoding="utf-8")
    print(f"Wrote {result['game_count']} {args.league} games → {out}")


if __name__ == "__main__":
    main()
