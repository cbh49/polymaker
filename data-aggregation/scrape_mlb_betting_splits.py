#!/usr/bin/env python3
"""
Aggregate MLB public betting splits from:
  - PlayerProps.ai
  - SportsBettingDime
  - VSiN
  - EV Analytics (timestamped line-history charts; no public/handle %)

Writes a single combined JSON. PlayerProps fields are the defaults; other
sources are prefixed on each side:
  public_bet_pct / handle_bet_pct / open / live / diff / sharp_money
  sbd_public_bet_pct / sbd_handle_bet_pct / sbd_line / ...
  vsin_public_bet_pct / vsin_handle_bet_pct / vsin_line
  eva_line / eva_odds / eva_open / eva_history / eva_win_prob_pct
  covers_odds                              (Covers prediction markets)
  polymarket                               (Gamma share price + American odds + poll history)

If EV Analytics or Covers is already on the next Eastern slate, unmerged
games are kept under eva_line_movement / covers_prediction_markets.

Usage:
  python scrape_mlb_betting_splits.py
  python scrape_mlb_betting_splits.py --date 2026-08-12
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import date, datetime
from pathlib import Path
from typing import Any, Callable
from zoneinfo import ZoneInfo

from scrape_covers_odds import merge_covers_into_game
from scrape_covers_odds import scrape as scrape_covers
from scrape_eva_splits import merge_eva_into_game
from scrape_eva_splits import scrape as scrape_eva
from scrape_playerprops_splits import scrape as scrape_playerprops
from scrape_polymarket_odds import load_previous_games, merge_polymarket_into_game
from scrape_polymarket_odds import scrape as scrape_polymarket
from scrape_sbd_splits import merge_sbd_into_game
from scrape_sbd_splits import scrape as scrape_sbd
from scrape_vsin_splits import merge_vsin_into_game
from scrape_vsin_splits import scrape as scrape_vsin
from slate_alignment import game_slate_date as _game_slate_date
from slate_alignment import native_dates as _native_dates
from slate_alignment import same_slate as _same_slate

from mlb_team_map import canonical_abbr as _mlb_abbr

SCRIPT_DIR = Path(__file__).resolve().parent
DEFAULT_OUT = SCRIPT_DIR / "output" / "mlb_betting_splits.json"
PAGE_TZ = ZoneInfo("America/Los_Angeles")


def _index_games(games: list[dict[str, Any]]) -> tuple[dict[str, dict[str, Any]], dict[tuple[str, str], dict[str, Any]]]:
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
    away = _mlb_abbr(str(game.get("away_abbr") or game.get("away") or ""))
    home = _mlb_abbr(str(game.get("home_abbr") or game.get("home") or ""))
    if not away or not home:
        return None
    for other in by_matchup.values():
        o_away = _mlb_abbr(str(other.get("away_abbr") or other.get("away") or ""))
        o_home = _mlb_abbr(str(other.get("home_abbr") or other.get("home") or ""))
        if o_away == away and o_home == home:
            return other
    return None


def _on_slate(game: dict[str, Any], day: date) -> bool:
    game_day = _game_slate_date(game)
    return game_day is None or game_day == day


def _scrape_or_empty(name: str, fn: Callable[[], dict[str, Any]]) -> dict[str, Any]:
    try:
        result = fn()
        if isinstance(result, dict):
            return result
    except Exception as exc:  # noqa: BLE001
        print(f"Warning: {name} scrape failed: {exc}", file=sys.stderr)
    return {"games": [], "game_count": 0, "source": name}


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Scrape + merge MLB betting splits from PlayerProps, SBD, VSiN, and EV Analytics"
    )
    parser.add_argument(
        "--date",
        default=None,
        help="Slate date YYYY-MM-DD in America/Los_Angeles (default: today Pacific)",
    )
    parser.add_argument("--out", type=Path, default=DEFAULT_OUT)
    args = parser.parse_args()

    day = date.fromisoformat(args.date) if args.date else datetime.now(PAGE_TZ).date()

    pp = _scrape_or_empty("PlayerProps", lambda: scrape_playerprops(day=day))
    sbd = _scrape_or_empty("SportsBettingDime", lambda: scrape_sbd(day=day))
    vsin = _scrape_or_empty("VSiN", lambda: scrape_vsin(day=day))
    eva = _scrape_or_empty("EV Analytics", lambda: scrape_eva())
    covers = _scrape_or_empty("Covers", lambda: scrape_covers(league="MLB"))
    pp_native = _native_dates(pp.get("games") or [])
    sbd_native = _native_dates(sbd.get("games") or [])
    vsin_native = _native_dates(vsin.get("games") or [])
    pp_count = len(pp.get("games") or [])

    sbd_by_matchup, sbd_by_teams = _index_games(sbd.get("games") or [])
    vsin_by_matchup, vsin_by_teams = _index_games(vsin.get("games") or [])
    eva_by_matchup, eva_by_teams = _index_games(eva.get("games") or [])
    covers_by_matchup, covers_by_teams = _index_games(covers.get("games") or [])

    sbd_merged = 0
    vsin_merged = 0
    eva_merged_ids: set[str] = set()
    covers_merged_ids: set[str] = set()
    for game in pp.get("games") or []:
        sbd_game = _find_game(game, sbd_by_matchup, sbd_by_teams)
        if sbd_game and _same_slate(sbd_game, game, day):
            merge_sbd_into_game(game, sbd_game)
            sbd_merged += 1
        vsin_game = _find_game(game, vsin_by_matchup, vsin_by_teams)
        if vsin_game and _same_slate(vsin_game, game, day):
            merge_vsin_into_game(game, vsin_game)
            vsin_merged += 1
        eva_game = _find_game(game, eva_by_matchup, eva_by_teams)
        if eva_game and _same_slate(eva_game, game, day):
            merge_eva_into_game(game, eva_game)
            eva_merged_ids.add(str(eva_game.get("eva_game_id") or eva_game.get("matchup")))
        covers_game = _find_game(game, covers_by_matchup, covers_by_teams)
        if covers_game and _same_slate(covers_game, game, day):
            merge_covers_into_game(game, covers_game)
            covers_merged_ids.add(str(covers_game.get("covers_game_id") or covers_game.get("matchup")))

    pp_matchups = {g.get("matchup") for g in pp.get("games") or []}
    pp_teams = {
        (str(g.get("away") or "").lower(), str(g.get("home") or "").lower())
        for g in pp.get("games") or []
        if g.get("away") and g.get("home")
    }
    extras_by_matchup: dict[str, dict[str, Any]] = {}

    def _already_present(game: dict[str, Any]) -> bool:
        if game.get("matchup") in pp_matchups:
            return True
        key = (str(game.get("away") or "").lower(), str(game.get("home") or "").lower())
        return bool(key[0] and key[1] and key in pp_teams)

    for sbd_game in sbd.get("games") or []:
        key = sbd_game.get("matchup")
        if not key or _already_present(sbd_game) or not _on_slate(sbd_game, day):
            continue
        extras_by_matchup[key] = dict(sbd_game)
    for vsin_game in vsin.get("games") or []:
        key = vsin_game.get("matchup")
        if not key or _already_present(vsin_game) or not _on_slate(vsin_game, day):
            continue
        if key in extras_by_matchup:
            merge_vsin_into_game(extras_by_matchup[key], vsin_game)
        else:
            extras_by_matchup[key] = dict(vsin_game)
    for extra in extras_by_matchup.values():
        eva_game = _find_game(extra, eva_by_matchup, eva_by_teams)
        if eva_game and _same_slate(eva_game, extra, day):
            merge_eva_into_game(extra, eva_game)
            eva_merged_ids.add(str(eva_game.get("eva_game_id") or eva_game.get("matchup")))
        covers_game = _find_game(extra, covers_by_matchup, covers_by_teams)
        if covers_game and _same_slate(covers_game, extra, day):
            merge_covers_into_game(extra, covers_game)
            covers_merged_ids.add(str(covers_game.get("covers_game_id") or covers_game.get("matchup")))
    for eva_game in eva.get("games") or []:
        key = eva_game.get("matchup")
        if not key or _already_present(eva_game):
            continue
        if _game_slate_date(eva_game) != day:
            continue
        if key in extras_by_matchup:
            merge_eva_into_game(extras_by_matchup[key], eva_game)
        else:
            extras_by_matchup[key] = dict(eva_game)
        eva_merged_ids.add(str(eva_game.get("eva_game_id") or key))
    for covers_game in covers.get("games") or []:
        key = covers_game.get("matchup")
        if not key or _already_present(covers_game):
            continue
        if _game_slate_date(covers_game) != day:
            continue
        if key in extras_by_matchup:
            merge_covers_into_game(extras_by_matchup[key], covers_game)
        else:
            extras_by_matchup[key] = dict(covers_game)
        covers_merged_ids.add(str(covers_game.get("covers_game_id") or key))
    extras = list(extras_by_matchup.values())

    if extras:
        pp.setdefault("games", []).extend(extras)
        pp["games"].sort(key=lambda g: g.get("game_time_utc") or g.get("matchup") or "")
        pp["game_count"] = len(pp["games"])

    eva_unmerged = [
        g
        for g in (eva.get("games") or [])
        if str(g.get("eva_game_id") or g.get("matchup")) not in eva_merged_ids
    ]
    if eva_unmerged:
        pp["eva_line_movement"] = {
            "source": eva.get("source"),
            "source_page": eva.get("source_page"),
            "api": eva.get("api"),
            "date": eva.get("date"),
            "slate_dates": eva.get("slate_dates"),
            "scraped_at": eva.get("scraped_at"),
            "game_count": len(eva_unmerged),
            "note": (
                "EV Analytics board did not match this slate date; "
                "timestamped histories are kept here instead of merging onto games."
            ),
            "games": eva_unmerged,
        }
    covers_unmerged = [
        g
        for g in (covers.get("games") or [])
        if str(g.get("covers_game_id") or g.get("matchup")) not in covers_merged_ids
    ]
    if covers_unmerged:
        pp["covers_prediction_markets"] = {
            "source": covers.get("source"),
            "source_page": covers.get("source_page"),
            "date": covers.get("date"),
            "books": covers.get("books"),
            "scraped_at": covers.get("scraped_at"),
            "game_count": len(covers_unmerged),
            "note": (
                "Covers Today (ET) board did not match this slate date; "
                "prediction-market odds are kept here instead of merging onto games."
            ),
            "games": covers_unmerged,
        }

    pp["sources"] = {
        "playerprops": {
            "source": pp.get("source"),
            "source_page": pp.get("source_page"),
            "api": pp.get("api"),
            "date": pp.get("date"),
            "native_dates": pp_native,
            "game_count": pp_count,
        },
        "sportsbettingdime": {
            "source": sbd.get("source"),
            "source_page": sbd.get("source_page"),
            "api": sbd.get("api"),
            "date": sbd.get("date"),
            "native_dates": sbd_native,
            "game_count": sbd.get("game_count"),
            "merged_into_playerprops_games": sbd_merged,
        },
        "vsin": {
            "source": vsin.get("source"),
            "source_page": vsin.get("source_page"),
            "date": vsin.get("date"),
            "native_dates": vsin_native,
            "game_count": vsin.get("game_count"),
            "merged_into_playerprops_games": vsin_merged,
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
    }

    prev_by_matchup = load_previous_games(args.out)
    poly = _scrape_or_empty(
        "Polymarket",
        lambda: scrape_polymarket(league="MLB", games=pp.get("games") or [], day=day),
    )
    poly_by_matchup, poly_by_teams = _index_games(poly.get("games") or [])
    poly_merged = 0
    for game in pp.get("games") or []:
        poly_game = _find_game(game, poly_by_matchup, poly_by_teams)
        if not poly_game:
            continue
        merge_polymarket_into_game(game, poly_game, prev_by_matchup.get(game.get("matchup") or ""))
        poly_merged += 1
    pp["sources"]["polymarket"] = {
        "source": poly.get("source"),
        "api": poly.get("api"),
        "date": poly.get("date"),
        "game_count": poly.get("game_count"),
        "merged_into_games": poly_merged,
    }
    pp["source"] = (
        "playerprops.ai + sportsbettingdime.com + data.vsin.com + "
        "evanalytics.com + covers.com + polymarket"
    )
    pp["league"] = "MLB"
    pp["date"] = day.isoformat()
    pp["scraped_at"] = datetime.now(PAGE_TZ).astimezone().isoformat()
    pp["game_count"] = len(pp.get("games") or [])

    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(pp, indent=2) + "\n", encoding="utf-8")
    print(
        f"Wrote {pp['game_count']} games "
        f"(SBD merged={sbd_merged}, VSiN merged={vsin_merged}, "
        f"EVA merged={len(eva_merged_ids)}, Covers merged={len(covers_merged_ids)}, "
        f"Polymarket merged={poly_merged}, extras={len(extras)}) → {args.out}"
    )


if __name__ == "__main__":
    main()
