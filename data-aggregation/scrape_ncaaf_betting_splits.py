#!/usr/bin/env python3
"""
Aggregate NCAAF / CFB public betting splits from:
  - DraftKings Network (handle % / bets %; PlayerProps has no CFB)
  - SportsBettingDime (handle % / bets % via ncaafb-odds; skip Pinnacle)
  - TheSpread.com (open → current spread movement for RLM)
  - VSiN
  - EV Analytics (timestamped line-history charts; no public/handle %)
  - Covers prediction markets
  - Polymarket (cfb-YYYY series; moneyline / spread / total)

Writes a single combined JSON. DraftKings fields are the defaults; other
sources are prefixed on each side:
  public_bet_pct / handle_bet_pct / live / live_odds
  sbd_public_bet_pct / sbd_handle_bet_pct / sbd_line
  open / open_odds / diff                 (TheSpread)
  vsin_public_bet_pct / vsin_handle_bet_pct / vsin_line
  eva_line / eva_odds / eva_open / eva_history / eva_win_prob_pct
  covers_odds
  polymarket

Markets kept per game: moneyline, spread, total (over/under).

Usage:
  python scrape_ncaaf_betting_splits.py
  python scrape_ncaaf_betting_splits.py --date 2026-08-29
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import date, datetime
from pathlib import Path
from typing import Any, Callable
from zoneinfo import ZoneInfo

from cfb_team_map import canonical_abbr, names_match
from scrape_covers_odds import merge_covers_into_game
from scrape_covers_odds import scrape as scrape_covers
from scrape_dk_splits import scrape as scrape_dk
from scrape_eva_splits import merge_eva_into_game
from scrape_eva_splits import scrape as scrape_eva
from scrape_polymarket_odds import load_previous_games, merge_polymarket_into_game
from scrape_polymarket_odds import scrape as scrape_polymarket
from scrape_sbd_splits import merge_sbd_into_game
from scrape_sbd_splits import scrape as scrape_sbd
from scrape_thespread_splits import merge_thespread_into_game
from scrape_thespread_splits import scrape as scrape_thespread
from scrape_vsin_splits import merge_vsin_into_game
from scrape_vsin_splits import scrape as scrape_vsin
from slate_alignment import NCAAF_SLATE_WINDOW_DAYS
from slate_alignment import game_slate_date as _game_slate_date
from slate_alignment import in_slate_window
from slate_alignment import native_dates as _native_dates
from slate_alignment import same_slate as _same_slate

SCRIPT_DIR = Path(__file__).resolve().parent
DEFAULT_OUT = SCRIPT_DIR / "output" / "ncaaf_betting_splits.json"
PAGE_TZ = ZoneInfo("America/Los_Angeles")
WINDOW = NCAAF_SLATE_WINDOW_DAYS


def _index_games(
    games: list[dict[str, Any]],
) -> tuple[dict[str, dict[str, Any]], dict[tuple[str, str], dict[str, Any]]]:
    by_matchup = {g["matchup"]: g for g in games if g.get("matchup")}
    by_teams = {
        (str(g.get("away") or "").lower(), str(g.get("home") or "").lower()): g
        for g in games
        if g.get("away") and g.get("home")
    }
    return by_matchup, by_teams


def _find_game(
    game: dict[str, Any],
    by_matchup: dict[str, dict[str, Any]],
    by_teams: dict[tuple[str, str], dict[str, Any]],
) -> dict[str, Any] | None:
    found = by_matchup.get(game.get("matchup"))
    if found is not None:
        return found
    key = (str(game.get("away") or "").lower(), str(game.get("home") or "").lower())
    found = by_teams.get(key)
    if found is not None:
        return found
    away = canonical_abbr(str(game.get("away_abbr") or game.get("away") or ""))
    home = canonical_abbr(str(game.get("home_abbr") or game.get("home") or ""))
    if away and home:
        for other in by_matchup.values():
            o_away = canonical_abbr(str(other.get("away_abbr") or other.get("away") or ""))
            o_home = canonical_abbr(str(other.get("home_abbr") or other.get("home") or ""))
            if o_away == away and o_home == home:
                return other
    for other in by_matchup.values():
        if names_match(str(game.get("away") or ""), str(other.get("away") or "")) and names_match(
            str(game.get("home") or ""), str(other.get("home") or "")
        ):
            return other
    return None


def _on_slate(game: dict[str, Any], day: date) -> bool:
    game_day = _game_slate_date(game)
    return in_slate_window(game_day, day, WINDOW)


def _same(src: dict[str, Any], dest: dict[str, Any], day: date) -> bool:
    return _same_slate(src, dest, day, window_days=WINDOW)


def _scrape_or_empty(name: str, fn: Callable[[], dict[str, Any]]) -> dict[str, Any]:
    print(f"Scraping {name}…", flush=True)
    try:
        result = fn()
        if isinstance(result, dict):
            n = len(result.get("games") or [])
            print(f"  {name}: {n} games", flush=True)
            return result
    except Exception as exc:  # noqa: BLE001
        print(f"Warning: {name} scrape failed: {exc}", file=sys.stderr, flush=True)
    return {"games": [], "game_count": 0, "source": name}


def main() -> None:
    parser = argparse.ArgumentParser(
        description=(
            "Scrape + merge NCAAF betting splits from DraftKings, SBD, TheSpread, "
            "VSiN, EV Analytics, Covers, and Polymarket (no Pinnacle)"
        )
    )
    parser.add_argument(
        "--date",
        default=None,
        help="Slate date YYYY-MM-DD in America/Los_Angeles (default: today Pacific; keeps +6 days)",
    )
    parser.add_argument("--out", type=Path, default=DEFAULT_OUT)
    parser.add_argument("--headed", action="store_true", help="Show the browser for DK / TheSpread")
    args = parser.parse_args()

    day = date.fromisoformat(args.date) if args.date else datetime.now(PAGE_TZ).date()

    dk = _scrape_or_empty(
        "DraftKings", lambda: scrape_dk(day=day, headed=args.headed, league="NCAAF")
    )
    sbd = _scrape_or_empty("SportsBettingDime", lambda: scrape_sbd(day=day, league="NCAAF"))
    spread = _scrape_or_empty(
        "TheSpread", lambda: scrape_thespread(day=day, headed=args.headed, league="NCAAF")
    )
    vsin = _scrape_or_empty("VSiN", lambda: scrape_vsin(league="NCAAF", day=day))
    eva = _scrape_or_empty("EV Analytics", lambda: scrape_eva(league="NCAAF"))
    covers = _scrape_or_empty("Covers", lambda: scrape_covers(league="NCAAF"))
    dk_native = _native_dates(dk.get("games") or [])
    sbd_native = _native_dates(sbd.get("games") or [])
    spread_native = _native_dates(spread.get("games") or [])
    vsin_native = _native_dates(vsin.get("games") or [])
    dk_count = len(dk.get("games") or [])

    sbd_by_matchup, sbd_by_teams = _index_games(sbd.get("games") or [])
    spread_by_matchup, spread_by_teams = _index_games(spread.get("games") or [])
    vsin_by_matchup, vsin_by_teams = _index_games(vsin.get("games") or [])
    eva_by_matchup, eva_by_teams = _index_games(eva.get("games") or [])
    covers_by_matchup, covers_by_teams = _index_games(covers.get("games") or [])

    sbd_merged = 0
    spread_merged = 0
    vsin_merged = 0
    eva_merged_ids: set[str] = set()
    covers_merged_ids: set[str] = set()
    for game in dk.get("games") or []:
        sbd_game = _find_game(game, sbd_by_matchup, sbd_by_teams)
        if sbd_game and _same(sbd_game, game, day):
            merge_sbd_into_game(game, sbd_game)
            sbd_merged += 1
        spread_game = _find_game(game, spread_by_matchup, spread_by_teams)
        if spread_game and _same(spread_game, game, day):
            merge_thespread_into_game(game, spread_game)
            spread_merged += 1
        vsin_game = _find_game(game, vsin_by_matchup, vsin_by_teams)
        if vsin_game and _same(vsin_game, game, day):
            merge_vsin_into_game(game, vsin_game)
            vsin_merged += 1
        eva_game = _find_game(game, eva_by_matchup, eva_by_teams)
        if eva_game:
            merge_eva_into_game(game, eva_game)
            eva_merged_ids.add(str(eva_game.get("eva_game_id") or eva_game.get("matchup")))
        covers_game = _find_game(game, covers_by_matchup, covers_by_teams)
        if covers_game and _same(covers_game, game, day):
            merge_covers_into_game(game, covers_game)
            covers_merged_ids.add(str(covers_game.get("covers_game_id") or covers_game.get("matchup")))

    dk_matchups = {g.get("matchup") for g in dk.get("games") or []}
    dk_by_matchup, dk_by_teams = _index_games(dk.get("games") or [])
    extras_by_matchup: dict[str, dict[str, Any]] = {}

    def _already_present(game: dict[str, Any]) -> bool:
        if game.get("matchup") in dk_matchups:
            return True
        return _find_game(game, dk_by_matchup, dk_by_teams) is not None

    for sbd_game in sbd.get("games") or []:
        key = sbd_game.get("matchup")
        if not key or _already_present(sbd_game) or not _on_slate(sbd_game, day):
            continue
        extras_by_matchup[key] = dict(sbd_game)
    for spread_game in spread.get("games") or []:
        key = spread_game.get("matchup")
        if not key or _already_present(spread_game) or not _on_slate(spread_game, day):
            continue
        found = _find_game(spread_game, extras_by_matchup, {})
        if found:
            merge_thespread_into_game(found, spread_game)
        else:
            extras_by_matchup[key] = dict(spread_game)
    for vsin_game in vsin.get("games") or []:
        key = vsin_game.get("matchup")
        if not key or _already_present(vsin_game) or not _on_slate(vsin_game, day):
            continue
        found = _find_game(vsin_game, extras_by_matchup, {})
        if found:
            merge_vsin_into_game(found, vsin_game)
        else:
            extras_by_matchup[key] = dict(vsin_game)
    for extra in extras_by_matchup.values():
        eva_game = _find_game(extra, eva_by_matchup, eva_by_teams)
        if eva_game:
            merge_eva_into_game(extra, eva_game)
            eva_merged_ids.add(str(eva_game.get("eva_game_id") or eva_game.get("matchup")))
        covers_game = _find_game(extra, covers_by_matchup, covers_by_teams)
        if covers_game and _same(covers_game, extra, day):
            merge_covers_into_game(extra, covers_game)
            covers_merged_ids.add(str(covers_game.get("covers_game_id") or covers_game.get("matchup")))
    for eva_game in eva.get("games") or []:
        key = eva_game.get("matchup")
        if not key or _already_present(eva_game):
            continue
        found = _find_game(eva_game, extras_by_matchup, {})
        if found:
            merge_eva_into_game(found, eva_game)
            eva_merged_ids.add(str(eva_game.get("eva_game_id") or key))
    for covers_game in covers.get("games") or []:
        key = covers_game.get("matchup")
        if not key or _already_present(covers_game):
            continue
        if not _on_slate(covers_game, day):
            continue
        found = _find_game(covers_game, extras_by_matchup, {})
        if found:
            merge_covers_into_game(found, covers_game)
        else:
            extras_by_matchup[key] = dict(covers_game)
        covers_merged_ids.add(str(covers_game.get("covers_game_id") or key))
    extras = list(extras_by_matchup.values())

    if extras:
        dk.setdefault("games", []).extend(extras)
        dk["games"].sort(
            key=lambda g: g.get("game_time_utc") or g.get("game_time_local") or g.get("matchup") or ""
        )
        dk["game_count"] = len(dk["games"])

    eva_unmerged = [
        g
        for g in (eva.get("games") or [])
        if str(g.get("eva_game_id") or g.get("matchup")) not in eva_merged_ids
    ]
    if eva_unmerged:
        dk["eva_line_movement"] = {
            "source": eva.get("source"),
            "source_page": eva.get("source_page"),
            "api": eva.get("api"),
            "date": eva.get("date"),
            "slate_dates": eva.get("slate_dates"),
            "scraped_at": eva.get("scraped_at"),
            "game_count": len(eva_unmerged),
            "note": (
                "EV Analytics games that did not match a DK/SBD/TheSpread/VSiN game "
                "by team; timestamped histories are kept here instead of merging."
            ),
            "games": eva_unmerged,
        }
    covers_unmerged = [
        g
        for g in (covers.get("games") or [])
        if str(g.get("covers_game_id") or g.get("matchup")) not in covers_merged_ids
    ]
    if covers_unmerged:
        dk["covers_prediction_markets"] = {
            "source": covers.get("source"),
            "source_page": covers.get("source_page"),
            "date": covers.get("date"),
            "books": covers.get("books"),
            "scraped_at": covers.get("scraped_at"),
            "game_count": len(covers_unmerged),
            "note": (
                "Covers board did not match this slate window; "
                "prediction-market odds are kept here instead of merging onto games."
            ),
            "games": covers_unmerged,
        }

    dk["sources"] = {
        "draftkings": {
            "source": dk.get("source"),
            "source_page": dk.get("source_page"),
            "date": dk.get("date"),
            "native_dates": dk_native,
            "game_count": dk_count,
        },
        "sportsbettingdime": {
            "source": sbd.get("source"),
            "source_page": sbd.get("source_page"),
            "api": sbd.get("api"),
            "date": sbd.get("date"),
            "native_dates": sbd_native,
            "game_count": sbd.get("game_count"),
            "merged_into_draftkings_games": sbd_merged,
        },
        "thespread": {
            "source": spread.get("source"),
            "source_page": spread.get("source_page"),
            "date": spread.get("date"),
            "native_dates": spread_native,
            "game_count": spread.get("game_count"),
            "merged_into_draftkings_games": spread_merged,
        },
        "vsin": {
            "source": vsin.get("source"),
            "source_page": vsin.get("source_page"),
            "date": vsin.get("date"),
            "native_dates": vsin_native,
            "game_count": vsin.get("game_count"),
            "merged_into_draftkings_games": vsin_merged,
        },
        "evanalytics": {
            "source": eva.get("source"),
            "source_page": eva.get("source_page"),
            "api": eva.get("api"),
            "date": eva.get("date"),
            "slate_dates": eva.get("slate_dates"),
            "game_count": eva.get("game_count"),
            "merged_into_games": len(eva_merged_ids),
            "unmerged_games": len(eva_unmerged),
        },
        "covers": {
            "source": covers.get("source"),
            "source_page": covers.get("source_page"),
            "date": covers.get("date"),
            "books": covers.get("books"),
            "game_count": covers.get("game_count"),
            "merged_into_games": len(covers_merged_ids),
            "unmerged_games": len(covers_unmerged),
        },
        "extras_added": len(extras),
        "pinnacle": {
            "skipped": True,
            "note": "Pinnacle is not published for CFB; sharp findings use DK, SBD, VSiN, TheSpread, EVA, Covers, and Polymarket.",
        },
    }

    prev_by_matchup = load_previous_games(args.out)
    poly = _scrape_or_empty(
        "Polymarket",
        lambda: scrape_polymarket(league="NCAAF", games=dk.get("games") or [], day=day),
    )
    poly_by_matchup, poly_by_teams = _index_games(poly.get("games") or [])
    poly_merged = 0
    for game in dk.get("games") or []:
        poly_game = _find_game(game, poly_by_matchup, poly_by_teams)
        if not poly_game:
            continue
        merge_polymarket_into_game(game, poly_game, prev_by_matchup.get(game.get("matchup") or ""))
        poly_merged += 1
    dk["sources"]["polymarket"] = {
        "source": poly.get("source"),
        "api": poly.get("api"),
        "date": poly.get("date"),
        "game_count": poly.get("game_count"),
        "merged_into_games": poly_merged,
    }
    dk["source"] = (
        "dknetwork.draftkings.com + sportsbettingdime.com + thespread.com + "
        "data.vsin.com + evanalytics.com + covers.com + polymarket"
    )
    dk["league"] = "NCAAF"
    dk["date"] = day.isoformat()
    dk["scraped_at"] = datetime.now(PAGE_TZ).astimezone().isoformat()
    dk["game_count"] = len(dk.get("games") or [])

    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(dk, indent=2) + "\n", encoding="utf-8")
    print(
        f"Wrote {dk['game_count']} NCAAF games "
        f"(SBD merged={sbd_merged}, TheSpread merged={spread_merged}, "
        f"VSiN merged={vsin_merged}, EVA merged={len(eva_merged_ids)}, "
        f"Covers merged={len(covers_merged_ids)}, Polymarket merged={poly_merged}, "
        f"extras={len(extras)}) → {args.out}"
    )


if __name__ == "__main__":
    main()
