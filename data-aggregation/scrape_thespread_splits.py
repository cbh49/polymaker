#!/usr/bin/env python3
"""
Scrape public-betting line movement from TheSpread.com (WNBA, UFC, or NCAAF).

SportsBettingDime does not publish WNBA/UFC splits; this is the stand-in
for scrape_sbd_splits.py, used for open → current line movement (RLM).
NCAAF uses both: SBD for handle/public % and TheSpread for RLM.

Sources:
  WNBA:  https://www.thespread.com/wnba-public-betting-chart/
  UFC:   https://www.thespread.com/mma-odds/
  NCAAF: https://www.thespread.com/ncaa-college-football-public-betting-chart/

WNBA: Cloudflare-protected public-betting chart. Market-average pie
percentages are burned into a SportsInsights GIF; we keep open/current
spread + juice.

UFC: MMA odds board with live moneyline/spread/total plus an OPEN row
of moneyline prices (spread/total open is usually blank). That open →
live moneyline pair is what find_sharp_money needs for UFC RLM.

Usage:
  python scrape_thespread_splits.py
  python scrape_thespread_splits.py --league UFC --out output/thespread_ufc_betting_splits.json
"""

from __future__ import annotations

import argparse
import json
import re
import sys
import threading
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, TypeVar
from zoneinfo import ZoneInfo

import requests

from scrape_browser import DEFAULT_USER_AGENT, fetch_rendered_html
from wnba_team_map import (
    DEFAULT_MATCHUPS,
    canonical_abbr,
    canonical_name,
    load_matchups,
    match_matchup,
)

SCRIPT_DIR = Path(__file__).resolve().parent
DEFAULT_OUT = SCRIPT_DIR / "output" / "thespread_wnba_betting_splits.json"
DEFAULT_UFC_OUT = SCRIPT_DIR / "output" / "thespread_ufc_betting_splits.json"
DEFAULT_NCAAF_OUT = SCRIPT_DIR / "output" / "thespread_ncaaf_betting_splits.json"
PAGE_TZ = ZoneInfo("America/Los_Angeles")
PAGE_URLS = {
    "WNBA": "https://www.thespread.com/wnba-public-betting-chart/",
    "UFC": "https://www.thespread.com/mma-odds/",
    "NCAAF": "https://www.thespread.com/ncaa-college-football-public-betting-chart/",
}
PAGE_URL = PAGE_URLS["WNBA"]
# Keep these tight: a dead/Cloudflare-stuck thespread.com used to hang the
# whole NCAAF/WNBA/UFC scrape (requests timeout misses DNS, then Playwright
# waits another 60s). Prefer skipping TheSpread over blocking the pipeline.
HTTP_TIMEOUT = (4.0, 8.0)  # connect, read
HTTP_DEADLINE_S = 12.0
PLAYWRIGHT_TIMEOUT_MS = 12_000
_T = TypeVar("_T")

MINUS_CHARS = str.maketrans({"−": "-", "–": "-", "—": "-"})
LINE_ODDS_RE = re.compile(
    r"([+-]?\d+(?:\.\d+)?)\s+([+-]\d+)"
)


def _clean(text: str) -> str:
    return (text or "").translate(MINUS_CHARS).replace("\xa0", " ").strip()


def parse_number(text: str | None) -> float | int | None:
    if not text:
        return None
    m = re.search(r"([+-]?\d+(?:\.\d+)?)", _clean(text).replace(",", ""))
    if not m:
        return None
    num = float(m.group(1))
    return int(num) if num.is_integer() else num


def parse_line_odds_pair(text: str) -> list[tuple[float | int | None, float | int | None]]:
    """Parse stacked 'Open' / 'Current' cells: '+6.5 -114\\n-6.5 -106'."""
    pairs: list[tuple[float | int | None, float | int | None]] = []
    for match in LINE_ODDS_RE.finditer(_clean(text)):
        line = parse_number(match.group(1))
        odds = parse_number(match.group(2))
        pairs.append((line, odds))
    return pairs


def _cell_strings(cell: Any) -> list[str]:
    if cell is None:
        return []
    header = cell.select_one(".dataheader")
    header_text = header.get_text(strip=True).lower() if header else ""
    return [s for s in cell.stripped_strings if s.strip().lower() != header_text]


def _md_for_day(day: date) -> str:
    return f"{day.month}/{day.day}"


def _day_from_md(md: str, year: int) -> date | None:
    m = re.match(r"^(\d{1,2})/(\d{1,2})$", (md or "").strip())
    if not m:
        return None
    try:
        return date(year, int(m.group(1)), int(m.group(2)))
    except ValueError:
        return None


def parse_datarow(
    row: Any,
    day: date,
    matchups: list[dict[str, Any]],
    *,
    canonical_name_fn=canonical_name,
    canonical_abbr_fn=canonical_abbr,
    match_matchup_fn=match_matchup,
    allowed_mds: set[str] | None = None,
) -> dict[str, Any] | None:
    time_cell = row.select_one(".datacell.time")
    teams_cell = row.select_one(".datacell.teams")
    if not time_cell or not teams_cell:
        return None

    time_parts = _cell_strings(time_cell)
    if not time_parts:
        return None
    row_md = time_parts[0].strip()
    if allowed_mds is not None:
        if row_md not in allowed_mds:
            return None
    elif row_md != _md_for_day(day):
        return None
    tip = time_parts[1].strip() if len(time_parts) > 1 else None

    away_span = teams_cell.find("span", id="tmv")
    home_span = teams_cell.find("span", id="tmh")
    away_raw = away_span.get_text(" ", strip=True) if away_span else None
    home_raw = home_span.get_text(" ", strip=True) if home_span else None
    if not away_raw or not home_raw:
        return None

    away_name = canonical_name_fn(away_raw)
    home_name = canonical_name_fn(home_raw)
    if not away_name or not home_name:
        return None
    away_abbr = canonical_abbr_fn(away_name) or away_name
    home_abbr = canonical_abbr_fn(home_name) or home_name

    teams_text = teams_cell.get_text(" ", strip=True)
    rot_away = rot_home = None
    rots = re.findall(r"(\d{3})\s*-", teams_text)
    if len(rots) >= 2:
        rot_away, rot_home = rots[0], rots[1]

    open_cell = row.select_one(".child-open")
    current_cell = row.select_one(".child-current")
    open_pairs = parse_line_odds_pair(open_cell.get_text("\n", strip=True) if open_cell else "")
    live_pairs = parse_line_odds_pair(
        current_cell.get_text("\n", strip=True) if current_cell else ""
    )

    def _side(
        selection: str,
        open_pair: tuple[float | int | None, float | int | None] | None,
        live_pair: tuple[float | int | None, float | int | None] | None,
    ) -> dict[str, Any]:
        open_line, open_odds = open_pair or (None, None)
        live_line, live_odds = live_pair or (None, None)
        diff = None
        if isinstance(open_line, (int, float)) and isinstance(live_line, (int, float)):
            diff = live_line - open_line
        return {
            "selection": selection,
            "open": open_line,
            "live": live_line,
            "diff": diff,
            "open_odds": open_odds,
            "live_odds": live_odds,
        }

    away_open = open_pairs[0] if len(open_pairs) > 0 else None
    home_open = open_pairs[1] if len(open_pairs) > 1 else None
    away_live = live_pairs[0] if len(live_pairs) > 0 else None
    home_live = live_pairs[1] if len(live_pairs) > 1 else None

    matched = match_matchup_fn(away_name, home_name, matchups)
    game: dict[str, Any] = {
        "matchup": f"{away_abbr} @ {home_abbr}",
        "away_abbr": away_abbr,
        "home_abbr": home_abbr,
        "away": away_name,
        "home": home_name,
        "game_time_local": tip,
        "date": (_day_from_md(row_md, day.year) or day).isoformat(),
        "rotation_away": rot_away,
        "rotation_home": rot_home,
        "spread": {
            "away": _side(away_abbr, away_open, away_live),
            "home": _side(home_abbr, home_open, home_live),
        },
    }

    ml_open_cell = row.select_one(".child-ml-open") or row.select_one(".datacell.ml .child-open")
    ml_live_cell = row.select_one(".child-ml-current") or row.select_one(".datacell.ml .child-current")
    tot_open_cell = row.select_one(".child-total-open") or row.select_one(".datacell.total .child-open")
    tot_live_cell = row.select_one(".child-total-current") or row.select_one(".datacell.total .child-current")
    ml_open_pairs = parse_line_odds_pair(ml_open_cell.get_text("\n", strip=True) if ml_open_cell else "")
    ml_live_pairs = parse_line_odds_pair(ml_live_cell.get_text("\n", strip=True) if ml_live_cell else "")
    tot_open_pairs = parse_line_odds_pair(tot_open_cell.get_text("\n", strip=True) if tot_open_cell else "")
    tot_live_pairs = parse_line_odds_pair(tot_live_cell.get_text("\n", strip=True) if tot_live_cell else "")
    if ml_open_pairs or ml_live_pairs:
        game["moneyline"] = {
            "away": _side(
                away_abbr,
                ml_open_pairs[0] if ml_open_pairs else None,
                ml_live_pairs[0] if ml_live_pairs else None,
            ),
            "home": _side(
                home_abbr,
                ml_open_pairs[1] if len(ml_open_pairs) > 1 else None,
                ml_live_pairs[1] if len(ml_live_pairs) > 1 else None,
            ),
        }
    if tot_open_pairs or tot_live_pairs:
        game["total"] = {
            "over": _side(
                "Over",
                tot_open_pairs[0] if tot_open_pairs else None,
                tot_live_pairs[0] if tot_live_pairs else None,
            ),
            "under": _side(
                "Under",
                tot_open_pairs[1] if len(tot_open_pairs) > 1 else None,
                tot_live_pairs[1] if len(tot_live_pairs) > 1 else None,
            ),
        }
    if matched:
        game["espn_game_id"] = matched.get("espn_game_id")
        if matched.get("game_time"):
            game["game_time_local"] = matched.get("game_time")
    return game


def parse_games(
    html: str,
    day: date,
    matchups: list[dict[str, Any]],
    *,
    canonical_name_fn=canonical_name,
    canonical_abbr_fn=canonical_abbr,
    match_matchup_fn=match_matchup,
    allowed_mds: set[str] | None = None,
) -> list[dict[str, Any]]:
    from bs4 import BeautifulSoup

    soup = BeautifulSoup(html, "html.parser")
    games: list[dict[str, Any]] = []
    for row in soup.select(".datarow"):
        parsed = parse_datarow(
            row,
            day,
            matchups,
            canonical_name_fn=canonical_name_fn,
            canonical_abbr_fn=canonical_abbr_fn,
            match_matchup_fn=match_matchup_fn,
            allowed_mds=allowed_mds,
        )
        if parsed:
            games.append(parsed)
    return games


def _tse_price(cell: Any) -> float | int | None:
    if cell is None:
        return None
    prices = cell.select(".tse-price")
    if not prices:
        return None
    return parse_number(prices[0].get_text(strip=True))


def _tse_prices(cell: Any) -> list[float | int]:
    if cell is None:
        return []
    out: list[float | int] = []
    for el in cell.select(".tse-price"):
        num = parse_number(el.get_text(strip=True))
        if num is not None:
            out.append(num)
    return out


def _tse_hcap(cell: Any) -> float | int | None:
    if cell is None:
        return None
    hcap = cell.select_one(".tse-hcap")
    if not hcap:
        return None
    return parse_number(hcap.get_text(strip=True))


def _parse_mma_mdy(text: str, year_hint: int) -> date | None:
    m = re.search(r"(\d{1,2})/(\d{1,2})/(\d{2})", text or "")
    if not m:
        return None
    month, day_n, yy = int(m.group(1)), int(m.group(2)), int(m.group(3))
    century = (year_hint // 100) * 100
    year = century + yy
    try:
        return date(year, month, day_n)
    except ValueError:
        return None


def _mma_side(
    selection: str,
    *,
    live: float | int | None,
    live_odds: float | int | None = None,
    opened: float | int | None = None,
    open_odds: float | int | None = None,
) -> dict[str, Any]:
    diff = None
    if isinstance(opened, (int, float)) and isinstance(live, (int, float)):
        diff = live - opened
    row: dict[str, Any] = {
        "selection": selection,
        "open": opened,
        "live": live,
        "diff": diff,
    }
    if open_odds is not None:
        row["open_odds"] = open_odds
    if live_odds is not None:
        row["live_odds"] = live_odds
    return row


def parse_mma_game(card: Any, day: date) -> dict[str, Any] | None:
    from ufc_fighter_map import canonical_name

    date_el = card.select_one(".tse-cell-time-date")
    time_el = card.select_one(".tse-cell-time-time")
    row_day = _parse_mma_mdy(date_el.get_text(strip=True) if date_el else "", day.year)
    if row_day is None or row_day != day:
        return None

    away_el = card.select_one(".tse-team-name.tse-away")
    home_el = card.select_one(".tse-team-name.tse-home")
    away_name = canonical_name(away_el.get_text(" ", strip=True) if away_el else "")
    home_name = canonical_name(home_el.get_text(" ", strip=True) if home_el else "")
    if not away_name or not home_name:
        return None

    away_row = card.select_one(".tse-row-away")
    home_row = card.select_one(".tse-row-home")
    open_row = card.select_one(".tse-row-open")
    if away_row is None or home_row is None:
        return None

    away_ml = _tse_price(away_row.select_one(".tse-cell-ml"))
    home_ml = _tse_price(home_row.select_one(".tse-cell-ml"))
    open_mls = _tse_prices(open_row.select_one(".tse-cell-ml") if open_row else None)
    away_open_ml = open_mls[0] if len(open_mls) > 0 else None
    home_open_ml = open_mls[1] if len(open_mls) > 1 else None

    away_sp = away_row.select_one(".tse-cell-spread")
    home_sp = home_row.select_one(".tse-cell-spread")
    tot_away = away_row.select_one(".tse-cell-total")
    tot_home = home_row.select_one(".tse-cell-total")

    game: dict[str, Any] = {
        "matchup": f"{away_name} vs {home_name}",
        "away_abbr": away_name,
        "home_abbr": home_name,
        "away": away_name,
        "home": home_name,
        "date": day.isoformat(),
        "game_time_local": time_el.get_text(strip=True) if time_el else None,
        "thespread_event_id": card.get("data-event-id"),
        "moneyline": {
            "away": _mma_side(away_name, live=away_ml, opened=away_open_ml),
            "home": _mma_side(home_name, live=home_ml, opened=home_open_ml),
        },
        "spread": {
            "away": _mma_side(
                away_name,
                live=_tse_hcap(away_sp),
                live_odds=_tse_price(away_sp),
            ),
            "home": _mma_side(
                home_name,
                live=_tse_hcap(home_sp),
                live_odds=_tse_price(home_sp),
            ),
        },
        "total": {
            "over": _mma_side(
                "Over",
                live=_tse_hcap(tot_away),
                live_odds=_tse_price(tot_away),
            ),
            "under": _mma_side(
                "Under",
                live=_tse_hcap(tot_home),
                live_odds=_tse_price(tot_home),
            ),
        },
    }
    return game


def parse_mma_games(html: str, day: date) -> list[dict[str, Any]]:
    from bs4 import BeautifulSoup

    soup = BeautifulSoup(html, "html.parser")
    games: list[dict[str, Any]] = []
    for card in soup.select(".tse-game"):
        parsed = parse_mma_game(card, day)
        if parsed:
            games.append(parsed)
    return games


def _call_with_deadline(fn: Callable[[], _T], timeout_s: float) -> _T:
    """Run `fn` on a daemon thread so a hung DNS/SSL connect cannot stall us."""
    box: list[_T] = []
    err: list[Exception] = []

    def _run() -> None:
        try:
            box.append(fn())
        except Exception as exc:  # noqa: BLE001
            err.append(exc)

    thread = threading.Thread(target=_run, daemon=True)
    thread.start()
    thread.join(timeout_s)
    if thread.is_alive():
        raise TimeoutError(f"timed out after {timeout_s:.0f}s")
    if err:
        raise err[0]
    if not box:
        raise TimeoutError(f"timed out after {timeout_s:.0f}s")
    return box[0]


def _fetch_static_html(url: str) -> str:
    def _get() -> str:
        with requests.Session() as session:
            session.trust_env = False
            resp = session.get(
                url,
                headers={
                    "User-Agent": DEFAULT_USER_AGENT,
                    "Accept": "text/html,application/xhtml+xml;q=0.9",
                },
                timeout=HTTP_TIMEOUT,
            )
        resp.raise_for_status()
        try:
            return resp.content.decode("utf-8")
        except UnicodeDecodeError:
            return resp.content.decode("latin-1")

    return _call_with_deadline(_get, HTTP_DEADLINE_S)


def _html_has_selector_token(html: str, wait_selector: str) -> bool:
    if not html or not wait_selector:
        return False
    from bs4 import BeautifulSoup

    return bool(BeautifulSoup(html, "html.parser").select(wait_selector))


def _fetch_thespread_html(
    page_url: str,
    wait_selector: str,
    *,
    headed: bool,
) -> str:
    html = ""
    try:
        html = _fetch_static_html(page_url)
    except (requests.RequestException, OSError, TimeoutError, UnicodeDecodeError) as exc:
        print(
            f"Warning: TheSpread HTTP fetch failed ({exc}); skipping",
            file=sys.stderr,
        )
        return ""
    if _html_has_selector_token(html, wait_selector):
        return html
    # Page answered but had no rows (Cloudflare / empty). Browser might help.
    try:
        html = fetch_rendered_html(
            page_url,
            wait_selector=wait_selector,
            headed=headed,
            require_selector=False,
            timeout_ms=PLAYWRIGHT_TIMEOUT_MS,
        )
    except Exception as exc:  # noqa: BLE001
        print(f"Warning: TheSpread browser fetch failed: {exc}", file=sys.stderr)
        return html or ""
    if not _html_has_selector_token(html, wait_selector):
        blocked = "just a moment" in html.lower() or "cf-browser-verification" in html
        why = "Cloudflare challenge" if blocked else "no betting rows"
        print(f"Warning: TheSpread page had {why}; continuing without it", file=sys.stderr)
    return html


def merge_thespread_into_game(game: dict[str, Any], spread_game: dict[str, Any]) -> None:
    """Copy TheSpread open/live prices onto matching sides.

    WNBA / NCAAF: spread open/live + juice (ML / total when the chart has them).
    UFC: moneyline open/live (RLM), plus live spread/total when present.
    Existing dest values are overwritten so open and live stay from the
    same TheSpread snapshot (needed for reverse line movement).
    """
    for market in ("spread", "moneyline", "total"):
        src = spread_game.get(market)
        if not isinstance(src, dict):
            continue
        dst = game.get(market)
        if not isinstance(dst, dict):
            dst = {}
            game[market] = dst
        for side, src_side in src.items():
            if not isinstance(src_side, dict):
                continue
            dst_side = dst.get(side)
            if not isinstance(dst_side, dict):
                dst_side = {"selection": src_side.get("selection")}
                dst[side] = dst_side
            for key in ("open", "live", "diff", "open_odds", "live_odds"):
                if src_side.get(key) is not None:
                    dst_side[key] = src_side[key]
    if spread_game.get("rotation_away"):
        game["rotation_away"] = spread_game["rotation_away"]
    if spread_game.get("rotation_home"):
        game["rotation_home"] = spread_game["rotation_home"]
    if spread_game.get("thespread_event_id"):
        game["thespread_event_id"] = spread_game["thespread_event_id"]


def scrape(
    day: date | None = None,
    matchups_path: Path = DEFAULT_MATCHUPS,
    url: str | None = None,
    headed: bool = False,
    league: str = "WNBA",
) -> dict[str, Any]:
    league = (league or "WNBA").strip().upper()
    if league == "CFB":
        league = "NCAAF"
    day = day or datetime.now(PAGE_TZ).date()
    page_url = url or PAGE_URLS.get(league, PAGE_URL)
    if league == "UFC":
        html = _fetch_thespread_html(page_url, ".tse-game", headed=headed)
        games = parse_mma_games(html, day) if html else []
    elif league == "NCAAF":
        from cfb_team_map import canonical_abbr as cfb_canonical_abbr
        from cfb_team_map import canonical_name as cfb_canonical_name
        from cfb_team_map import match_matchup as cfb_match_matchup

        matchups = load_matchups(matchups_path) if matchups_path and matchups_path.exists() else []
        html = _fetch_thespread_html(page_url, ".datarow", headed=headed)
        allowed_mds = {_md_for_day(day + timedelta(days=offset)) for offset in range(0, 7)}
        games = (
            parse_games(
                html,
                day,
                matchups,
                canonical_name_fn=cfb_canonical_name,
                canonical_abbr_fn=cfb_canonical_abbr,
                match_matchup_fn=cfb_match_matchup,
                allowed_mds=allowed_mds,
            )
            if html
            else []
        )
    else:
        matchups = load_matchups(matchups_path)
        html = _fetch_thespread_html(page_url, ".datarow", headed=headed)
        games = parse_games(html, day, matchups) if html else []
    return {
        "source": "thespread.com",
        "source_page": page_url,
        "date": day.isoformat(),
        "league": league,
        "scraped_at": datetime.now(timezone.utc).isoformat(),
        "game_count": len(games),
        "games": games,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description="Scrape TheSpread public betting / MMA odds")
    parser.add_argument("--league", default="WNBA", choices=["WNBA", "UFC", "NCAAF", "CFB"])
    parser.add_argument(
        "--date",
        default=None,
        help="Slate date YYYY-MM-DD (default: today Pacific).",
    )
    parser.add_argument("--matchups", type=Path, default=DEFAULT_MATCHUPS)
    parser.add_argument("--out", type=Path, default=None)
    parser.add_argument("--url", default=None)
    parser.add_argument("--headed", action="store_true")
    args = parser.parse_args()

    day = date.fromisoformat(args.date) if args.date else datetime.now(PAGE_TZ).date()
    result = scrape(
        day=day,
        matchups_path=args.matchups,
        url=args.url,
        headed=args.headed,
        league=args.league,
    )
    league = (args.league or "WNBA").strip().upper()
    if league == "CFB":
        league = "NCAAF"
    out = args.out or {
        "UFC": DEFAULT_UFC_OUT,
        "NCAAF": DEFAULT_NCAAF_OUT,
    }.get(league, DEFAULT_OUT)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(result, indent=2) + "\n", encoding="utf-8")
    print(f"Wrote {result['game_count']} {args.league} games ({day.isoformat()}) → {out}")


if __name__ == "__main__":
    main()
