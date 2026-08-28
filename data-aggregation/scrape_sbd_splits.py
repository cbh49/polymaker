#!/usr/bin/env python3
"""
Scrape MLB or NCAAF public betting % + handle % from SportsBettingDime.

Source pages:
  MLB:   https://www.sportsbettingdime.com/mlb/public-betting-trends/
  NCAAF: https://www.sportsbettingdime.com/college-football/public-betting-trends/

Data comes from:
  /wp-json/adpt/v1/mlb-odds
  /wp-json/adpt/v1/ncaafb-odds
  (each game includes bettingSplits)

The college-football HTML page may say splits are unavailable while the
ncaafb-odds API still returns them.

Fields are prefixed with sbd_ when merged into the combined splits file:
  sbd_public_bet_pct  <- betsPercentage
  sbd_handle_bet_pct  <- stakePercentage
  sbd_line / sbd_odds / sbd_book  <- displayed book line (no open/live/diff)

Usage:
  python scrape_sbd_splits.py
  python scrape_sbd_splits.py --league NCAAF --out output/sbd_ncaaf_betting_splits.json
"""

from __future__ import annotations

import argparse
import json
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import requests

from mlb_team_map import DEFAULT_ABBREVS, DEFAULT_MATCHUPS, load_abbr_to_name, load_matchups

SCRIPT_DIR = Path(__file__).resolve().parent
DEFAULT_OUT = {
    "MLB": SCRIPT_DIR / "output" / "sbd_betting_splits.json",
    "NCAAF": SCRIPT_DIR / "output" / "sbd_ncaaf_betting_splits.json",
}

PAGE_URLS = {
    "MLB": "https://www.sportsbettingdime.com/mlb/public-betting-trends/",
    "NCAAF": "https://www.sportsbettingdime.com/college-football/public-betting-trends/",
}
API_URLS = {
    "MLB": "https://www.sportsbettingdime.com/wp-json/adpt/v1/mlb-odds",
    "NCAAF": "https://www.sportsbettingdime.com/wp-json/adpt/v1/ncaafb-odds",
}
PAGE_URL = PAGE_URLS["MLB"]
API_URL = API_URLS["MLB"]
# Same book IDs the public-betting page requests.
DEFAULT_BOOKS = (
    "sr:book:17324,"  # BetMGM
    "sr:book:28901,"  # Bet365
    "sr:book:18149,"  # DraftKings
    "sr:book:32219,"  # Caesars / WHNJ
    "sr:book:18186"  # FanDuel
)
PAGE_TZ = ZoneInfo("America/Los_Angeles")

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/128.0.0.0 Safari/537.36"
    ),
    "Accept": "application/json",
    "Referer": PAGE_URLS["MLB"],
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


def load_abbr_to_team(path: Path) -> dict[str, str]:
    mapping = load_abbr_to_name(path)
    mapping.update(ABBR_ALIASES)
    return mapping


def team_name_from_abbr(abbr: str, abbr_map: dict[str, str]) -> str | None:
    return abbr_map.get(abbr.strip().upper())


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


def _round_pct(value: Any) -> int | None:
    if value is None:
        return None
    try:
        return int(round(float(value)))
    except (TypeError, ValueError):
        return None


def _parse_number(value: Any) -> float | int | str | None:
    if value is None or value == "":
        return None
    if isinstance(value, (int, float)):
        return value
    text = str(value).strip().replace("+", "")
    try:
        num = float(text)
        return int(num) if num.is_integer() else num
    except ValueError:
        return str(value).strip()


def _best_book_side(books: list[dict[str, Any]], side: str) -> dict[str, Any] | None:
    """Pick the book marked best for this side; else first book with that side."""
    for book in books:
        side_data = book.get(side)
        if isinstance(side_data, dict) and side_data.get("best"):
            return book
    for book in books:
        if isinstance(book.get(side), dict):
            return book
    return books[0] if books else None


def _side_line_fields(market: str, book: dict[str, Any] | None, side: str) -> dict[str, Any]:
    if not book:
        return {}
    side_data = book.get(side) if isinstance(book.get(side), dict) else {}
    out: dict[str, Any] = {"sbd_book": book.get("name")}
    if market == "moneyline":
        out["sbd_line"] = _parse_number(side_data.get("odds"))
    elif market == "spread":
        out["sbd_line"] = _parse_number(side_data.get("spread"))
        out["sbd_odds"] = _parse_number(side_data.get("odds"))
    else:  # total
        out["sbd_line"] = _parse_number(book.get("total"))
        out["sbd_odds"] = _parse_number(side_data.get("odds"))
    return out


def _split_side(
    split_side: dict[str, Any] | None,
    market: str,
    book: dict[str, Any] | None,
    side: str,
) -> dict[str, Any] | None:
    if not isinstance(split_side, dict):
        return None
    # ncaafb-odds / mlb-odds split sides only publish betsPercentage and
    # stakePercentage — no raw ticket counts or dollar handle to preserve.
    row: dict[str, Any] = {
        "sbd_public_bet_pct": _round_pct(split_side.get("betsPercentage")),
        "sbd_handle_bet_pct": _round_pct(split_side.get("stakePercentage")),
    }
    row.update(_side_line_fields(market, book, side))
    return row


def event_on_day(scheduled: str | None, day: date) -> bool:
    if not scheduled:
        return False
    try:
        dt = datetime.fromisoformat(scheduled.replace("Z", "+00:00"))
    except ValueError:
        return False
    return dt.astimezone(PAGE_TZ).date() == day


def has_usable_splits(splits: dict[str, Any] | None) -> bool:
    if not isinstance(splits, dict):
        return False
    for market in ("moneyline", "spread", "total"):
        block = splits.get(market)
        if not isinstance(block, dict):
            continue
        if block.get("updated") and any(
            _round_pct((block.get(side) or {}).get("betsPercentage")) is not None
            or _round_pct((block.get(side) or {}).get("stakePercentage")) is not None
            for side in ("away", "home", "over", "under")
            if isinstance(block.get(side), dict)
        ):
            return True
        for side in ("away", "home", "over", "under"):
            side_data = block.get(side)
            if not isinstance(side_data, dict):
                continue
            if _round_pct(side_data.get("betsPercentage")) is not None:
                return True
            if _round_pct(side_data.get("stakePercentage")) is not None:
                return True
    return False


def fetch_odds(books: str = DEFAULT_BOOKS, api_url: str = API_URL, referer: str = PAGE_URL) -> dict[str, Any]:
    params = {"books": books, "format": "us"}
    headers = {**HEADERS, "Referer": referer}
    with requests.Session() as session:
        session.trust_env = False
        resp = session.get(api_url, params=params, headers=headers, timeout=30)
    resp.raise_for_status()
    data = resp.json()
    if not isinstance(data, dict):
        raise ValueError("Unexpected odds payload")
    return data


def parse_event(
    event: dict[str, Any],
    abbr_map: dict[str, str],
    matchups: list[dict[str, Any]],
) -> dict[str, Any] | None:
    comps = event.get("competitors")
    if not isinstance(comps, dict):
        return None
    away_comp = comps.get("away") if isinstance(comps.get("away"), dict) else {}
    home_comp = comps.get("home") if isinstance(comps.get("home"), dict) else {}
    away_abbr = (
        away_comp.get("abbr") or away_comp.get("abbreviation") or away_comp.get("alias") or ""
    ).upper()
    home_abbr = (
        home_comp.get("abbr") or home_comp.get("abbreviation") or home_comp.get("alias") or ""
    ).upper()
    if not away_abbr or not home_abbr:
        return None

    splits = event.get("bettingSplits")
    if not has_usable_splits(splits if isinstance(splits, dict) else None):
        return None
    assert isinstance(splits, dict)

    markets = event.get("markets") if isinstance(event.get("markets"), dict) else {}

    def market_books(key: str) -> list[dict[str, Any]]:
        block = markets.get(key)
        if not isinstance(block, dict):
            return []
        books = block.get("books")
        return [b for b in books if isinstance(b, dict)] if isinstance(books, list) else []

    ml_splits = splits.get("moneyline") if isinstance(splits.get("moneyline"), dict) else {}
    sp_splits = splits.get("spread") if isinstance(splits.get("spread"), dict) else {}
    tot_splits = splits.get("total") if isinstance(splits.get("total"), dict) else {}

    ml_books = market_books("moneyline")
    sp_books = market_books("spread")
    tot_books = market_books("total")

    moneyline = {
        "away": _split_side(
            ml_splits.get("away"), "moneyline", _best_book_side(ml_books, "away"), "away"
        ),
        "home": _split_side(
            ml_splits.get("home"), "moneyline", _best_book_side(ml_books, "home"), "home"
        ),
    }
    spread = {
        "away": _split_side(
            sp_splits.get("away"), "spread", _best_book_side(sp_books, "away"), "away"
        ),
        "home": _split_side(
            sp_splits.get("home"), "spread", _best_book_side(sp_books, "home"), "home"
        ),
    }
    total = {
        "over": _split_side(
            tot_splits.get("over"), "total", _best_book_side(tot_books, "over"), "over"
        ),
        "under": _split_side(
            tot_splits.get("under"), "total", _best_book_side(tot_books, "under"), "under"
        ),
    }

    for side_key, abbr in (("away", away_abbr), ("home", home_abbr)):
        if moneyline.get(side_key):
            moneyline[side_key]["selection"] = abbr
        if spread.get(side_key):
            spread[side_key]["selection"] = abbr
    if total.get("over"):
        total["over"]["selection"] = "Over"
    if total.get("under"):
        total["under"]["selection"] = "Under"

    away_name = (
        team_name_from_abbr(away_abbr, abbr_map)
        or away_comp.get("market")
        or away_comp.get("name")
    )
    home_name = (
        team_name_from_abbr(home_abbr, abbr_map)
        or home_comp.get("market")
        or home_comp.get("name")
    )
    matched = match_matchup(away_name, home_name, matchups)

    game: dict[str, Any] = {
        "matchup": f"{away_abbr} @ {home_abbr}",
        "away_abbr": away_abbr,
        "home_abbr": home_abbr,
        "away": away_name,
        "home": home_name,
        "game_time_utc": event.get("scheduled"),
        "event_id": event.get("id"),
        "status": event.get("status"),
        "moneyline": moneyline,
        "spread": spread,
        "total": total,
        "sbd_updated": {
            "moneyline": ml_splits.get("updated"),
            "spread": sp_splits.get("updated"),
            "total": tot_splits.get("updated"),
        },
    }
    if event.get("scheduled"):
        try:
            dt = datetime.fromisoformat(str(event["scheduled"]).replace("Z", "+00:00"))
            game["date"] = dt.astimezone(PAGE_TZ).date().isoformat()
        except ValueError:
            pass
    if matched:
        game["espn_game_id"] = matched.get("espn_game_id")
        game["game_time_local"] = matched.get("game_time")
        game["away_pitcher"] = matched.get("away_pitcher")
        game["home_pitcher"] = matched.get("home_pitcher")
    return game


def scrape(
    day: date | None = None,
    matchups_path: Path = DEFAULT_MATCHUPS,
    abbrevs_path: Path = DEFAULT_ABBREVS,
    books: str = DEFAULT_BOOKS,
    league: str = "MLB",
    day_window: int | None = None,
) -> dict[str, Any]:
    league = (league or "MLB").strip().upper()
    if league == "CFB":
        league = "NCAAF"
    if league not in API_URLS:
        raise ValueError(f"Unsupported SBD league: {league}")
    day = day or datetime.now(PAGE_TZ).date()
    window = 6 if day_window is None and league == "NCAAF" else (day_window or 0)
    allowed = {day + timedelta(days=offset) for offset in range(0, window + 1)}
    page_url = PAGE_URLS[league]
    api_url = API_URLS[league]
    if league == "NCAAF":
        from cfb_team_map import ABBR_ALIASES, ABBR_TO_NAME, canonical_abbr, canonical_name

        abbr_map = dict(ABBR_TO_NAME)
        for alias, canon in ABBR_ALIASES.items():
            abbr_map[alias] = ABBR_TO_NAME[canon]
        matchups: list[dict[str, Any]] = []
    else:
        abbr_map = load_abbr_to_team(abbrevs_path)
        matchups = load_matchups(matchups_path)
    payload = fetch_odds(books=books, api_url=api_url, referer=page_url)

    by_matchup: dict[str, dict[str, Any]] = {}
    for event in payload.get("data") or []:
        if not isinstance(event, dict):
            continue
        scheduled = event.get("scheduled")
        if window:
            if not scheduled:
                continue
            try:
                dt = datetime.fromisoformat(str(scheduled).replace("Z", "+00:00"))
            except ValueError:
                continue
            if dt.astimezone(PAGE_TZ).date() not in allowed:
                continue
        elif not event_on_day(scheduled, day):
            continue
        parsed = parse_event(event, abbr_map, matchups)
        if not parsed:
            continue
        if league == "NCAAF":
            away_name = canonical_name(str(parsed.get("away") or parsed.get("away_abbr") or ""))
            home_name = canonical_name(str(parsed.get("home") or parsed.get("home_abbr") or ""))
            away_abbr = canonical_abbr(str(parsed.get("away_abbr") or away_name or "")) or parsed.get("away_abbr")
            home_abbr = canonical_abbr(str(parsed.get("home_abbr") or home_name or "")) or parsed.get("home_abbr")
            parsed["away"] = away_name or parsed.get("away")
            parsed["home"] = home_name or parsed.get("home")
            parsed["away_abbr"] = away_abbr
            parsed["home_abbr"] = home_abbr
            parsed["matchup"] = f"{away_abbr} @ {home_abbr}"
        key = parsed["matchup"]
        # Prefer the copy that has fresher split timestamps if duplicates exist.
        prev = by_matchup.get(key)
        if prev is None:
            by_matchup[key] = parsed
            continue
        prev_ts = (prev.get("sbd_updated") or {}).get("moneyline") or ""
        new_ts = (parsed.get("sbd_updated") or {}).get("moneyline") or ""
        if new_ts >= prev_ts:
            by_matchup[key] = parsed

    games = sorted(by_matchup.values(), key=lambda g: g.get("game_time_utc") or "")
    return {
        "source": "sportsbettingdime.com",
        "source_page": page_url,
        "api": api_url,
        "date": day.isoformat(),
        "league": league,
        "scraped_at": datetime.now(timezone.utc).isoformat(),
        "game_count": len(games),
        "games": games,
    }


def merge_sbd_into_game(game: dict[str, Any], sbd_game: dict[str, Any]) -> None:
    """Copy sbd_* fields from an SBD game onto matching PlayerProps side objects."""
    for market in ("moneyline", "spread", "total"):
        dst_market = game.get(market)
        src_market = sbd_game.get(market)
        if not isinstance(dst_market, dict) or not isinstance(src_market, dict):
            continue
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
                if key.startswith("sbd_"):
                    dst_side[key] = value
    if sbd_game.get("sbd_updated"):
        game["sbd_updated"] = sbd_game["sbd_updated"]
    if sbd_game.get("event_id") and not game.get("sbd_event_id"):
        game["sbd_event_id"] = sbd_game["event_id"]
    if sbd_game.get("status"):
        game["sbd_status"] = sbd_game["status"]


def main() -> None:
    parser = argparse.ArgumentParser(description="Scrape SportsBettingDime public betting splits")
    parser.add_argument("--league", default="MLB", choices=["MLB", "NCAAF", "CFB"])
    parser.add_argument(
        "--date",
        default=None,
        help="Slate date YYYY-MM-DD in America/Los_Angeles (default: today Pacific)",
    )
    parser.add_argument("--matchups", type=Path, default=DEFAULT_MATCHUPS)
    parser.add_argument("--abbrevs", type=Path, default=DEFAULT_ABBREVS)
    parser.add_argument("--out", type=Path, default=None)
    args = parser.parse_args()

    day = date.fromisoformat(args.date) if args.date else datetime.now(PAGE_TZ).date()
    result = scrape(
        day=day,
        matchups_path=args.matchups,
        abbrevs_path=args.abbrevs,
        league=args.league,
    )
    league = result.get("league") or "MLB"
    out = args.out or DEFAULT_OUT.get(league, DEFAULT_OUT["MLB"])
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(result, indent=2) + "\n", encoding="utf-8")
    print(f"Wrote {result['game_count']} {league} games → {out}")


if __name__ == "__main__":
    main()
