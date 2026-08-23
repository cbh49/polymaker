#!/usr/bin/env python3
"""
Scrape DraftKings Network betting splits for WNBA or UFC.

PlayerProps.ai does not publish WNBA/UFC splits; this is the stand-in
for scrape_playerprops_splits.py.

Sources:
  WNBA: ...?tb_eg=WNBA&tb_edate=today&tb_emt=0&itm_content=WNBA
  UFC:  ...?tb_eg=UFC&tb_edate=n30days&tb_emt=0&itm_content=UFC

The page is server-rendered (and 403s to plain requests), so HTML is
loaded with Playwright. Per-game cards expose Moneyline / Spread / Total
with % Handle and % Bets. UFC paginates via tb_page and uses "A vs B"
titles; we scrape every page then keep the requested slate date.

Fields (unprefixed, same role as PlayerProps on MLB):
  public_bet_pct  <- % Bets
  handle_bet_pct  <- % Handle
  live            <- current American odds (ML) or spread/total number
  live_odds       <- juice on spread/total

Usage:
  python scrape_dk_splits.py
  python scrape_dk_splits.py --league UFC --out output/dk_ufc_betting_splits.json
"""

from __future__ import annotations

import argparse
import json
import re
import time
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from scrape_browser import fetch_rendered_html
from wnba_team_map import DEFAULT_MATCHUPS
from wnba_team_map import canonical_abbr as wnba_canonical_abbr
from wnba_team_map import canonical_name as wnba_canonical_name
from wnba_team_map import load_matchups
from wnba_team_map import match_matchup as wnba_match_matchup

SCRIPT_DIR = Path(__file__).resolve().parent
DEFAULT_OUT = SCRIPT_DIR / "output" / "dk_wnba_betting_splits.json"
DEFAULT_UFC_OUT = SCRIPT_DIR / "output" / "dk_ufc_betting_splits.json"
PAGE_TZ = ZoneInfo("America/Los_Angeles")

PAGE_URLS = {
    "WNBA": (
        "https://dknetwork.draftkings.com/draftkings-sportsbook-betting-splits/"
        "?tb_eg=WNBA&tb_edate=today&tb_emt=0&itm_content=WNBA"
    ),
    "UFC": (
        "https://dknetwork.draftkings.com/draftkings-sportsbook-betting-splits/"
        "?tb_eg=UFC&tb_edate=n30days&tb_emt=0&itm_content=UFC"
    ),
}
PAGE_URL = PAGE_URLS["WNBA"]
VS_SPLIT = re.compile(r"\s+vs\.?\s+", re.IGNORECASE)
WHEN_MD = re.compile(r"^(\d{1,2})/(\d{1,2})")
MINUS_CHARS = str.maketrans({"−": "-", "–": "-", "—": "-"})
LINE_IN_LABEL = re.compile(
    r"^(?P<name>.+?)\s+(?P<line>[+-]?\d+(?:\.\d+)?)\s*$"
)


def _identity_abbr(name: str) -> str:
    return name


def _clean_odds_text(text: str) -> str:
    return (text or "").translate(MINUS_CHARS).replace("\xa0", " ").strip()


def parse_number(value: Any) -> float | int | None:
    if value is None or value == "":
        return None
    if isinstance(value, (int, float)):
        num = float(value)
        return int(num) if num.is_integer() else num
    cleaned = _clean_odds_text(str(value)).replace(",", "")
    m = re.search(r"([+-]?\d+(?:\.\d+)?)", cleaned)
    if not m:
        return None
    num = float(m.group(1))
    return int(num) if num.is_integer() else num


def parse_pct(text: str) -> int | None:
    m = re.search(r"(-?\d+(?:\.\d+)?)\s*%", text or "")
    if not m:
        return None
    return int(round(float(m.group(1))))


def _direct_div_texts(row: Any) -> list[str]:
    return [div.get_text(" ", strip=True) for div in row.find_all("div", recursive=False)]


def parse_slipline(label: str) -> tuple[str, float | int | None]:
    """Split 'WAS Mystics -6.5' or 'Over 172.5' into (name, line)."""
    text = _clean_odds_text(label)
    m = LINE_IN_LABEL.match(text)
    if m:
        return m.group("name").strip(), parse_number(m.group("line"))
    return text, None


def _row_side(row: Any) -> dict[str, Any] | None:
    parts = _direct_div_texts(row)
    if len(parts) < 4:
        return None
    name, embedded_line = parse_slipline(parts[0])
    return {
        "label": name,
        "embedded_line": embedded_line,
        "odds": parse_number(parts[1]),
        "handle_bet_pct": parse_pct(parts[2]),
        "public_bet_pct": parse_pct(parts[3]),
    }


def _parse_when_date(when: str | None, year: int) -> date | None:
    if not when:
        return None
    m = WHEN_MD.match(when.strip())
    if not m:
        return None
    try:
        return date(year, int(m.group(1)), int(m.group(2)))
    except ValueError:
        return None


def _parse_title(
    card: Any,
    *,
    canonical_name_fn,
    vs_matchup: bool,
) -> tuple[str | None, str | None, str | None, str | None]:
    title = card.select_one(".tb-se-title")
    if not title:
        return None, None, None, None
    link = title.find("a")
    matchup_text = link.get_text(" ", strip=True) if link else ""
    href = link.get("href") if link else None
    when = None
    span = title.find("span")
    if span:
        when = span.get_text(" ", strip=True)
    if vs_matchup:
        parts = VS_SPLIT.split(matchup_text, maxsplit=1)
        if len(parts) != 2:
            return None, None, when, href
        away_raw, home_raw = parts[0].strip(), parts[1].strip()
    else:
        if "@" not in matchup_text:
            return None, None, when, href
        away_raw, home_raw = [p.strip() for p in matchup_text.split("@", 1)]
    return canonical_name_fn(away_raw), canonical_name_fn(home_raw), when, href


def _event_id_from_href(href: str | None) -> str | None:
    if not href:
        return None
    m = re.search(r"/event/(\d+)", href)
    return m.group(1) if m else None


def _side_payload(
    *,
    selection: str,
    public_bet_pct: int | None,
    handle_bet_pct: int | None,
    live: float | int | None,
    live_odds: float | int | None = None,
) -> dict[str, Any]:
    row: dict[str, Any] = {
        "selection": selection,
        "public_bet_pct": public_bet_pct,
        "handle_bet_pct": handle_bet_pct,
        "live": live,
    }
    if live_odds is not None:
        row["live_odds"] = live_odds
    return row


def _assign_team_rows(
    rows: list[dict[str, Any]],
    away_name: str,
    home_name: str,
    *,
    canonical_name_fn,
    names_match_fn=None,
) -> tuple[dict[str, Any] | None, dict[str, Any] | None]:
    away_row = home_row = None
    away_l, home_l = away_name.lower(), home_name.lower()
    for row in rows:
        label = (row.get("label") or "").lower()
        resolved = (canonical_name_fn(row.get("label") or "") or "").lower()
        if names_match_fn is not None:
            if names_match_fn(row.get("label") or "", away_name):
                away_row = row
            elif names_match_fn(row.get("label") or "", home_name):
                home_row = row
            continue
        if away_l in label or resolved == away_l:
            away_row = row
        elif home_l in label or resolved == home_l:
            home_row = row
    return away_row, home_row


def parse_game(
    card: Any,
    matchups: list[dict[str, Any]],
    *,
    league: str = "WNBA",
    canonical_name_fn=wnba_canonical_name,
    canonical_abbr_fn=wnba_canonical_abbr,
    names_match_fn=None,
    day: date | None = None,
) -> dict[str, Any] | None:
    vs_matchup = league == "UFC"
    away_name, home_name, when, href = _parse_title(
        card, canonical_name_fn=canonical_name_fn, vs_matchup=vs_matchup
    )
    if not away_name or not home_name:
        return None
    if vs_matchup and day is not None:
        card_day = _parse_when_date(when, day.year)
        if card_day is not None and card_day != day:
            return None
    away_abbr = canonical_abbr_fn(away_name) or away_name
    home_abbr = canonical_abbr_fn(home_name) or home_name

    markets: dict[str, list[dict[str, Any]]] = {}
    for block in card.select(".tb-market-wrap > div"):
        head = block.select_one(".tb-se-head")
        if not head:
            continue
        market_name = head.get_text(" ", strip=True).split()[0].lower()
        sides = []
        for row in block.select(".tb-sodd"):
            parsed = _row_side(row)
            if parsed:
                sides.append(parsed)
        markets[market_name] = sides

    ml_rows = markets.get("moneyline") or []
    sp_rows = markets.get("spread") or []
    tot_rows = markets.get("total") or []

    ml_away, ml_home = _assign_team_rows(
        ml_rows, away_name, home_name, canonical_name_fn=canonical_name_fn, names_match_fn=names_match_fn
    )
    sp_away, sp_home = _assign_team_rows(
        sp_rows, away_name, home_name, canonical_name_fn=canonical_name_fn, names_match_fn=names_match_fn
    )

    def _ou(rows: list[dict[str, Any]], key: str) -> dict[str, Any] | None:
        for row in rows:
            if key in (row.get("label") or "").lower():
                return row
        return None

    tot_over = _ou(tot_rows, "over")
    tot_under = _ou(tot_rows, "under")

    moneyline = {
        "away": _side_payload(
            selection=away_abbr,
            public_bet_pct=(ml_away or {}).get("public_bet_pct"),
            handle_bet_pct=(ml_away or {}).get("handle_bet_pct"),
            live=(ml_away or {}).get("odds"),
        )
        if ml_away
        else None,
        "home": _side_payload(
            selection=home_abbr,
            public_bet_pct=(ml_home or {}).get("public_bet_pct"),
            handle_bet_pct=(ml_home or {}).get("handle_bet_pct"),
            live=(ml_home or {}).get("odds"),
        )
        if ml_home
        else None,
    }
    spread = {
        "away": _side_payload(
            selection=away_abbr,
            public_bet_pct=(sp_away or {}).get("public_bet_pct"),
            handle_bet_pct=(sp_away or {}).get("handle_bet_pct"),
            live=(sp_away or {}).get("embedded_line"),
            live_odds=(sp_away or {}).get("odds"),
        )
        if sp_away
        else None,
        "home": _side_payload(
            selection=home_abbr,
            public_bet_pct=(sp_home or {}).get("public_bet_pct"),
            handle_bet_pct=(sp_home or {}).get("handle_bet_pct"),
            live=(sp_home or {}).get("embedded_line"),
            live_odds=(sp_home or {}).get("odds"),
        )
        if sp_home
        else None,
    }
    total = {
        "over": _side_payload(
            selection="Over",
            public_bet_pct=(tot_over or {}).get("public_bet_pct"),
            handle_bet_pct=(tot_over or {}).get("handle_bet_pct"),
            live=(tot_over or {}).get("embedded_line"),
            live_odds=(tot_over or {}).get("odds"),
        )
        if tot_over
        else None,
        "under": _side_payload(
            selection="Under",
            public_bet_pct=(tot_under or {}).get("public_bet_pct"),
            handle_bet_pct=(tot_under or {}).get("handle_bet_pct"),
            live=(tot_under or {}).get("embedded_line"),
            live_odds=(tot_under or {}).get("odds"),
        )
        if tot_under
        else None,
    }

    matched = None if vs_matchup else wnba_match_matchup(away_name, home_name, matchups)
    matchup = f"{away_name} vs {home_name}" if vs_matchup else f"{away_abbr} @ {home_abbr}"
    game: dict[str, Any] = {
        "matchup": matchup,
        "away_abbr": away_abbr,
        "home_abbr": home_abbr,
        "away": away_name,
        "home": home_name,
        "game_time_local": when,
        "event_id": _event_id_from_href(href),
        "moneyline": moneyline,
        "spread": spread,
        "total": total,
    }
    if vs_matchup and day is not None:
        game["date"] = day.isoformat()
    if matched:
        game["espn_game_id"] = matched.get("espn_game_id")
        if matched.get("game_time"):
            game["game_time_local"] = matched.get("game_time")
    card_day = _parse_when_date(when, (day or datetime.now(PAGE_TZ)).year) if when else None
    if card_day is not None:
        game["date"] = card_day.isoformat()
    elif day is not None:
        game["date"] = day.isoformat()
    return game


def parse_games(
    html: str,
    matchups: list[dict[str, Any]],
    *,
    league: str = "WNBA",
    canonical_name_fn=wnba_canonical_name,
    canonical_abbr_fn=wnba_canonical_abbr,
    names_match_fn=None,
    day: date | None = None,
) -> list[dict[str, Any]]:
    from bs4 import BeautifulSoup

    soup = BeautifulSoup(html, "html.parser")
    games: list[dict[str, Any]] = []
    for card in soup.select(".tb-se"):
        parsed = parse_game(
            card,
            matchups,
            league=league,
            canonical_name_fn=canonical_name_fn,
            canonical_abbr_fn=canonical_abbr_fn,
            names_match_fn=names_match_fn,
            day=day,
        )
        if parsed:
            games.append(parsed)
    return games


def _page_url(url: str, page: int) -> str:
    if page <= 1:
        return url
    joiner = "&" if "?" in url else "?"
    if re.search(r"[?&]tb_page=", url):
        return re.sub(r"([?&]tb_page=)\d+", rf"\g<1>{page}", url)
    return f"{url}{joiner}tb_page={page}"


def _pager_max(html: str) -> int:
    from bs4 import BeautifulSoup

    soup = BeautifulSoup(html, "html.parser")
    pages = [1]
    for link in soup.select(".tb_pagination a[href]"):
        href = link.get("href") or ""
        m = re.search(r"tb_page=(\d+)", href)
        if m:
            pages.append(int(m.group(1)))
    return max(pages)


def scrape(
    day: date | None = None,
    matchups_path: Path = DEFAULT_MATCHUPS,
    url: str | None = None,
    headed: bool = False,
    retries: int = 3,
    league: str = "WNBA",
) -> dict[str, Any]:
    league = (league or "WNBA").upper()
    day = day or datetime.now(PAGE_TZ).date()
    page_url = url or PAGE_URLS.get(league, PAGE_URL)
    if league == "UFC":
        from ufc_fighter_map import canonical_name as ufc_canonical_name
        from ufc_fighter_map import names_match as ufc_names_match

        matchups: list[dict[str, Any]] = []
        canonical_name_fn = ufc_canonical_name
        canonical_abbr_fn = _identity_abbr
        names_match_fn = ufc_names_match
        max_pages = 8
    else:
        matchups = load_matchups(matchups_path)
        canonical_name_fn = wnba_canonical_name
        canonical_abbr_fn = wnba_canonical_abbr
        names_match_fn = None
        max_pages = 1

    parse_kw = {
        "league": league,
        "canonical_name_fn": canonical_name_fn,
        "canonical_abbr_fn": canonical_abbr_fn,
        "names_match_fn": names_match_fn,
        "day": day,
    }

    games: list[dict[str, Any]] = []
    last_html = ""
    for attempt in range(1, retries + 1):
        games = []
        seen_ids: set[str] = set()
        html = fetch_rendered_html(
            page_url,
            wait_selector="#tbsedid",
            headed=headed,
            extra_wait_ms=1500,
            optional_selector=".tb-se",
        )
        last_html = html
        pages_to_fetch = min(_pager_max(html), max_pages)
        for page in range(1, pages_to_fetch + 1):
            if page > 1:
                html = fetch_rendered_html(
                    _page_url(page_url, page),
                    wait_selector="#tbsedid",
                    headed=headed,
                    extra_wait_ms=1500,
                    optional_selector=".tb-se",
                )
                last_html = html
            for game in parse_games(html, matchups, **parse_kw):
                key = str(game.get("event_id") or game.get("matchup") or "")
                if key in seen_ids:
                    continue
                seen_ids.add(key)
                games.append(game)
        if games:
            break
        blocked = "Unable to fetch" in last_html or "403" in last_html
        if not blocked and "No events match" in last_html:
            break
        if attempt < retries:
            print(f"DraftKings splits empty (attempt {attempt}/{retries}); retrying…")
            time.sleep(2)
    if not games and ("Unable to fetch" in last_html or "403" in last_html):
        print("Warning: DraftKings returned no events (403 / empty table)")
    return {
        "source": "dknetwork.draftkings.com",
        "source_page": page_url,
        "date": day.isoformat(),
        "league": league,
        "scraped_at": datetime.now(timezone.utc).isoformat(),
        "game_count": len(games),
        "games": games,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description="Scrape DraftKings betting splits")
    parser.add_argument("--league", default="WNBA", choices=["WNBA", "UFC"])
    parser.add_argument("--matchups", type=Path, default=DEFAULT_MATCHUPS)
    parser.add_argument("--out", type=Path, default=None)
    parser.add_argument("--url", default=None)
    parser.add_argument("--headed", action="store_true")
    args = parser.parse_args()

    result = scrape(
        matchups_path=args.matchups,
        url=args.url,
        headed=args.headed,
        league=args.league,
    )
    out = args.out or (DEFAULT_UFC_OUT if args.league == "UFC" else DEFAULT_OUT)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(result, indent=2) + "\n", encoding="utf-8")
    print(f"Wrote {result['game_count']} {args.league} games → {out}")


if __name__ == "__main__":
    main()
