#!/usr/bin/env python3
"""Scrape ScoresAndOdds consensus bet % and money % for MLB, NBA, NHL, NCAAF, and NFL.

Pages (server-rendered, one request per league):
  https://www.scoresandodds.com/{league}/consensus-picks

Each game card is one market. The first bar is % of bets, the second is % of money.
Best-odds links are ignored. They are the best price across books, not one book's
open-to-live pair.

Fields, prefixed like SportsBettingDime when merged:
  sao_public_bet_pct
  sao_handle_bet_pct
  sao_line
  sao_scraped_at

Totals are stored as over/under. find_sharp_money reads over as the away side
and under as the home side.

Usage:
  python scrape_scoresodds_splits.py --league NFL
  python scrape_scoresodds_splits.py --league NBA --out output/sao_nba_betting_splits.json
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any, Callable
from zoneinfo import ZoneInfo

import requests
from bs4 import BeautifulSoup

SCRIPT_DIR = Path(__file__).resolve().parent
PAGE_TZ = ZoneInfo("America/Los_Angeles")
USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/128.0.0.0 Safari/537.36"
)
PAGE_URLS = {
    "MLB": "https://www.scoresandodds.com/mlb/consensus-picks",
    "NBA": "https://www.scoresandodds.com/nba/consensus-picks",
    "NHL": "https://www.scoresandodds.com/nhl/consensus-picks",
    "NCAAF": "https://www.scoresandodds.com/ncaaf/consensus-picks",
    "NFL": "https://www.scoresandodds.com/nfl/consensus-picks",
}
DEFAULT_OUT = {
    "MLB": SCRIPT_DIR / "output" / "sao_mlb_betting_splits.json",
    "NBA": SCRIPT_DIR / "output" / "sao_nba_betting_splits.json",
    "NHL": SCRIPT_DIR / "output" / "sao_nhl_betting_splits.json",
    "NCAAF": SCRIPT_DIR / "output" / "sao_ncaaf_betting_splits.json",
    "NFL": SCRIPT_DIR / "output" / "sao_nfl_betting_splits.json",
}
_PCT_RE = re.compile(r"(-?\d+(?:\.\d+)?)\s*%")
_LINE_RE = re.compile(r"\((?:[ou])?([+-]?\d+(?:\.\d+)?)\)", re.IGNORECASE)
_ABBR_RE = re.compile(r"^([A-Za-z0-9.]+)")


def _empty(league: str, page_url: str) -> dict[str, Any]:
    return {
        "source": "scoresandodds.com",
        "source_page": page_url,
        "date": datetime.now(PAGE_TZ).date().isoformat(),
        "league": league,
        "scraped_at": datetime.now(timezone.utc).isoformat(),
        "game_count": 0,
        "games": [],
    }


def abbr_map_for(league: str) -> dict[str, tuple[str, str]]:
    """Explicit SAO abbreviation -> (canonical abbr, full name). No fuzzy names."""
    key = league.upper()
    if key == "CFB":
        key = "NCAAF"
    if key == "NFL":
        from nfl_team_map import ABBR_ALIASES, ABBR_TO_NAME
    elif key == "NBA":
        from nba_team_map import ABBR_ALIASES, ABBR_TO_NAME
    elif key == "NHL":
        from nhl_team_map import ABBR_ALIASES, ABBR_TO_NAME
    elif key == "NCAAF":
        from cfb_team_map import ABBR_ALIASES, ABBR_TO_NAME
    elif key == "MLB":
        from mlb_team_map import ABBR_ALIASES, ABBR_TO_NAME
    else:
        return {}

    out: dict[str, tuple[str, str]] = {
        str(abbr).upper(): (str(abbr).upper(), name) for abbr, name in ABBR_TO_NAME.items()
    }
    for alias, canon in ABBR_ALIASES.items():
        canon_u = str(canon).upper()
        if canon_u not in out:
            continue
        out[str(alias).upper()] = out[canon_u]
    return out


def _card_market(card: Any) -> str | None:
    classes = " ".join(card.get("class") or []).lower()
    if "moneyline" in classes:
        return "moneyline"
    if "total" in classes:
        return "total"
    if "spread" in classes or "puckline" in classes or "runline" in classes:
        return "spread"
    return None


def _pct_from_span(span: Any) -> float | None:
    if span is None:
        return None
    text = span.get_text(" ", strip=True)
    match = _PCT_RE.search(text)
    if match:
        return float(match.group(1))
    style = span.get("style") or ""
    width = re.search(r"width:\s*(\d+(?:\.\d+)?)%", style)
    if width and text:
        return float(width.group(1))
    if width and not text:
        return None
    return None


def _complete_pair(
    left: float | None,
    right: float | None,
    *,
    label: str,
) -> tuple[float | None, float | None, str]:
    """Fill one blank side as 100 - other.

    Returns (left, right, status) where status is empty, ok, or invalid.
    """
    if left is None and right is None:
        return None, None, "empty"
    if left is None and right is not None:
        left = 100.0 - right
    elif right is None and left is not None:
        right = 100.0 - left
    if left is None or right is None:
        return None, None, "empty"
    total = left + right
    if total < 98 or total > 102:
        print(
            f"Warning: ScoresAndOdds {label} sums to {total:.1f}, not 98-102; dropping market",
            file=sys.stderr,
            flush=True,
        )
        return None, None, "invalid"
    return left, right, "ok"


def _parse_label(text: str) -> tuple[str, float | None]:
    cleaned = " ".join(text.split())
    line_match = _LINE_RE.search(cleaned)
    line = float(line_match.group(1)) if line_match else None
    abbr_match = _ABBR_RE.match(cleaned)
    token = abbr_match.group(1) if abbr_match else ""
    if token.lower() == "over":
        return "Over", line
    if token.lower() == "under":
        return "Under", line
    return token.upper(), line


def _bar_values(card: Any) -> tuple[tuple[float | None, float | None], tuple[float | None, float | None]]:
    bars = card.select(".trend-graph-percentage")
    bets = (None, None)
    money = (None, None)
    if bars:
        bets = (
            _pct_from_span(bars[0].select_one(".percentage-a")),
            _pct_from_span(bars[0].select_one(".percentage-b")),
        )
    if len(bars) > 1:
        money = (
            _pct_from_span(bars[1].select_one(".percentage-a")),
            _pct_from_span(bars[1].select_one(".percentage-b")),
        )
    return bets, money


def _side_labels(card: Any) -> tuple[tuple[str, float | None], tuple[str, float | None]]:
    strongs = card.select(".trend-graph-sides strong")
    left = _parse_label(strongs[0].get_text(" ", strip=True)) if strongs else ("", None)
    right = _parse_label(strongs[1].get_text(" ", strip=True)) if len(strongs) > 1 else ("", None)
    return left, right


def _side_row(
    selection: str,
    public: float | None,
    handle: float | None,
    line: float | None,
    scraped_at: str,
) -> dict[str, Any] | None:
    if public is None and handle is None and line is None:
        return None
    return {
        "selection": selection,
        "sao_public_bet_pct": None if public is None else int(round(public)),
        "sao_handle_bet_pct": None if handle is None else int(round(handle)),
        "sao_line": line,
        "sao_scraped_at": scraped_at,
    }


def _market_block(card: Any, kind: str, scraped_at: str) -> dict[str, Any] | None:
    left_label, right_label = _side_labels(card)
    bets, money = _bar_values(card)
    label = f"{left_label[0]} / {right_label[0]} {kind}"
    bet_l, bet_r, bet_status = _complete_pair(bets[0], bets[1], label=f"{label} bets")
    money_l, money_r, money_status = _complete_pair(money[0], money[1], label=f"{label} money")
    if bet_status == "invalid" or money_status == "invalid":
        return None
    if kind == "total":
        left_key, right_key = "over", "under"
        left_sel, right_sel = "Over", "Under"
    else:
        left_key, right_key = "away", "home"
        left_sel = left_label[0] or "away"
        right_sel = right_label[0] or "home"
    left = _side_row(left_sel, bet_l, money_l, left_label[1], scraped_at)
    right = _side_row(right_sel, bet_r, money_r, right_label[1], scraped_at)
    if left is None and right is None:
        return None
    return {left_key: left, right_key: right}


def _event_href(card: Any) -> str:
    link = card.select_one("a[href]")
    if link is None:
        return ""
    return str(link.get("href") or "").strip()


def _event_time(card: Any) -> str | None:
    stamp = card.select_one("[data-role='localtime'][data-value]")
    if stamp is None:
        return None
    raw = str(stamp.get("data-value") or "").strip()
    return raw or None


def _resolve_team(
    token: str,
    abbr_map: dict[str, tuple[str, str]],
) -> tuple[str, str] | None:
    if not token or token in {"Over", "Under"}:
        return None
    return abbr_map.get(token.upper())


def parse_games(
    html: str,
    league: str,
    *,
    scraped_at: str | None = None,
    abbr_map: dict[str, tuple[str, str]] | None = None,
) -> list[dict[str, Any]]:
    """Parse one consensus page into games. Unmatched abbreviations are logged and skipped."""
    league_key = "NCAAF" if league.upper() == "CFB" else league.upper()
    stamp = scraped_at or datetime.now(timezone.utc).isoformat()
    mapping = abbr_map if abbr_map is not None else abbr_map_for(league_key)
    soup = BeautifulSoup(html, "html.parser")
    grouped: dict[str, dict[str, Any]] = {}
    order: list[str] = []
    for card in soup.select("div.trend-card.consensus"):
        kind = _card_market(card)
        if kind is None:
            continue
        href = _event_href(card) or f"card-{id(card)}"
        if href not in grouped:
            grouped[href] = {}
            order.append(href)
        grouped[href][kind] = card

    games: list[dict[str, Any]] = []
    for href in order:
        cards = grouped[href]
        identity_card = cards.get("moneyline") or cards.get("spread") or cards.get("total")
        if identity_card is None:
            continue
        left_label, right_label = _side_labels(identity_card)
        away_token, home_token = left_label[0], right_label[0]
        away = _resolve_team(away_token, mapping)
        home = _resolve_team(home_token, mapping)
        if away is None or home is None:
            print(
                f"Warning: ScoresAndOdds unmatched {league_key} {away_token} @ {home_token}",
                file=sys.stderr,
                flush=True,
            )
            continue
        away_abbr, away_name = away
        home_abbr, home_name = home
        markets: dict[str, Any] = {}
        for kind, card in cards.items():
            block = _market_block(card, kind, stamp)
            if block is not None:
                markets[kind] = block
        if not markets:
            continue
        when = _event_time(identity_card)
        game: dict[str, Any] = {
            "matchup": f"{away_abbr} @ {home_abbr}",
            "away_abbr": away_abbr,
            "home_abbr": home_abbr,
            "away": away_name,
            "home": home_name,
            "event_id": href.rstrip("/").split("/")[-1] or None,
            "moneyline": markets.get("moneyline") or {"away": None, "home": None},
            "spread": markets.get("spread") or {"away": None, "home": None},
            "total": markets.get("total") or {"over": None, "under": None},
        }
        if when:
            game["game_time_utc"] = when
            try:
                dt = datetime.fromisoformat(when.replace("Z", "+00:00"))
                game["date"] = dt.astimezone(PAGE_TZ).date().isoformat()
            except ValueError:
                pass
        games.append(game)
    return games


def fetch_page(url: str, timeout: float = 30.0) -> str:
    headers = {"User-Agent": USER_AGENT, "Accept": "text/html"}
    with requests.Session() as session:
        session.trust_env = False
        resp = session.get(url, headers=headers, timeout=timeout)
    resp.raise_for_status()
    return resp.text


def scrape(
    day: date | None = None,
    league: str = "NFL",
    *,
    url: str | None = None,
    timeout: float = 30.0,
    fetch: Callable[[str, float], str] | None = None,
) -> dict[str, Any]:
    """Fetch one league page. A failed or unreadable page returns no games and does not raise."""
    league_key = (league or "NFL").strip().upper()
    if league_key == "CFB":
        league_key = "NCAAF"
    page_url = url or PAGE_URLS.get(league_key, "")
    if not page_url:
        print(f"Warning: ScoresAndOdds has no page for {league_key}", file=sys.stderr, flush=True)
        return _empty(league_key, page_url)
    getter = fetch or fetch_page
    try:
        html = getter(page_url, timeout)
    except Exception as exc:  # noqa: BLE001
        print(f"Warning: ScoresAndOdds fetch failed ({league_key}): {exc}", file=sys.stderr, flush=True)
        return _empty(league_key, page_url)
    try:
        games = parse_games(html, league_key)
    except Exception as exc:  # noqa: BLE001
        print(f"Warning: ScoresAndOdds parse failed ({league_key}): {exc}", file=sys.stderr, flush=True)
        return _empty(league_key, page_url)
    if day is not None:
        games = [g for g in games if not g.get("date") or g.get("date") >= day.isoformat()]
    return {
        "source": "scoresandodds.com",
        "source_page": page_url,
        "date": (day or datetime.now(PAGE_TZ).date()).isoformat(),
        "league": league_key,
        "scraped_at": datetime.now(timezone.utc).isoformat(),
        "game_count": len(games),
        "games": games,
    }


def merge_sao_into_game(game: dict[str, Any], sao_game: dict[str, Any]) -> None:
    """Copy sao_* fields onto matching side objects. Does not touch line-movement fields."""
    for market in ("moneyline", "spread", "total"):
        dst_market = game.get(market)
        src_market = sao_game.get(market)
        if not isinstance(src_market, dict):
            continue
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
                    if not dst_side.get("selection"):
                        dst_side["selection"] = value
                    continue
                if key.startswith("sao_"):
                    dst_side[key] = value


def _pair(game: dict[str, Any]) -> tuple[str, str]:
    return (
        str(game.get("away_abbr") or "").upper(),
        str(game.get("home_abbr") or "").upper(),
    )


def _match_exact(game: dict[str, Any], pool: list[dict[str, Any]]) -> dict[str, Any] | None:
    matchup = game.get("matchup")
    pair = _pair(game)
    for other in pool:
        if matchup and other.get("matchup") == matchup:
            return other
        other_pair = _pair(other)
        if pair[0] and pair[1] and other_pair == pair:
            return other
    return None


def attach_sao_games(
    games: list[dict[str, Any]],
    sao_games: list[dict[str, Any]],
    *,
    same: Callable[[dict[str, Any], dict[str, Any]], bool],
    on_slate: Callable[[dict[str, Any]], bool],
) -> tuple[int, list[dict[str, Any]]]:
    """Merge SAO onto existing rows by canonical abbr. Unmatched slate games are returned as extras."""
    merged = 0
    used: set[int] = set()
    for game in games:
        found = _match_exact(game, sao_games)
        if found is None or not same(found, game):
            continue
        merge_sao_into_game(game, found)
        merged += 1
        used.add(id(found))
    extras: list[dict[str, Any]] = []
    for cand in sao_games:
        if id(cand) in used or not on_slate(cand):
            continue
        if _match_exact(cand, games) is not None:
            continue
        extras.append(dict(cand))
    return merged, extras


def main() -> None:
    parser = argparse.ArgumentParser(description="Scrape ScoresAndOdds consensus splits")
    parser.add_argument("--league", default="NFL", choices=["MLB", "NBA", "NHL", "NCAAF", "CFB", "NFL"])
    parser.add_argument("--date", default=None, help="Slate date YYYY-MM-DD (Pacific). Keeps games on or after this day.")
    parser.add_argument("--out", type=Path, default=None)
    parser.add_argument("--url", default=None)
    args = parser.parse_args()
    day = date.fromisoformat(args.date) if args.date else datetime.now(PAGE_TZ).date()
    league = "NCAAF" if args.league == "CFB" else args.league
    result = scrape(day=day, league=league, url=args.url)
    out = args.out or DEFAULT_OUT[league]
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(result, indent=2) + "\n", encoding="utf-8")
    print(f"Wrote {result['game_count']} {league} games → {out}")


if __name__ == "__main__":
    main()
