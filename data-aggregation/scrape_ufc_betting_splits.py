#!/usr/bin/env python3
"""
Aggregate UFC public betting splits from:
  - DraftKings Network (handle % / bets %; PlayerProps / SBD / EVA / Covers have no UFC)
  - TheSpread.com MMA odds (open → current moneyline for RLM)
  - VSiN
  - Polymarket (Gamma share price + American odds + CLOB history)

Writes a single combined JSON. DraftKings fields are the defaults; other
sources are prefixed on each side:
  public_bet_pct / handle_bet_pct / live
  open / live / diff                      (TheSpread moneyline)
  vsin_public_bet_pct / vsin_handle_bet_pct / vsin_line
  polymarket                              (share price + American odds + history)

Corner order is not stable across books (DK "A vs B" vs VSiN "B vs A"),
so fights are matched as unordered pairs and sides are swapped on merge.

Usage:
  python scrape_ufc_betting_splits.py
  python scrape_ufc_betting_splits.py --date 2026-08-22
"""

from __future__ import annotations

import argparse
import json
from datetime import date, datetime
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from scrape_dk_splits import scrape as scrape_dk
from scrape_polymarket_odds import load_previous_games
from scrape_polymarket_odds import merge_polymarket_into_game
from scrape_polymarket_odds import scrape as scrape_polymarket
from scrape_thespread_splits import merge_thespread_into_game
from scrape_thespread_splits import scrape as scrape_thespread
from scrape_vsin_splits import merge_vsin_into_game
from scrape_vsin_splits import scrape as scrape_vsin
from ufc_fighter_map import align_game_to, pair_key

SCRIPT_DIR = Path(__file__).resolve().parent
DEFAULT_OUT = SCRIPT_DIR / "output" / "ufc_betting_splits.json"
PAGE_TZ = ZoneInfo("America/Los_Angeles")


def _index_games(
    games: list[dict[str, Any]],
) -> dict[tuple[str, str], dict[str, Any]]:
    by_pair: dict[tuple[str, str], dict[str, Any]] = {}
    for game in games:
        key = pair_key(game.get("away"), game.get("home"))
        if key:
            by_pair[key] = game
    return by_pair


def _find_aligned(
    game: dict[str, Any],
    by_pair: dict[tuple[str, str], dict[str, Any]],
) -> dict[str, Any] | None:
    key = pair_key(game.get("away"), game.get("home"))
    if not key:
        return None
    found = by_pair.get(key)
    if not found:
        return None
    return align_game_to(found, game)


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Scrape + merge UFC betting splits from DraftKings, TheSpread, VSiN, and Polymarket"
    )
    parser.add_argument(
        "--date",
        default=None,
        help="Card date YYYY-MM-DD in America/Los_Angeles (default: today Pacific)",
    )
    parser.add_argument("--out", type=Path, default=DEFAULT_OUT)
    parser.add_argument("--headed", action="store_true", help="Show the browser for DK / TheSpread")
    args = parser.parse_args()

    day = date.fromisoformat(args.date) if args.date else datetime.now(PAGE_TZ).date()

    dk = scrape_dk(day=day, headed=args.headed, league="UFC")
    spread = scrape_thespread(day=day, headed=args.headed, league="UFC")
    vsin = scrape_vsin(league="UFC", day=day)

    spread_by_pair = _index_games(spread.get("games") or [])
    vsin_by_pair = _index_games(vsin.get("games") or [])

    spread_merged = 0
    vsin_merged = 0
    for game in dk.get("games") or []:
        spread_game = _find_aligned(game, spread_by_pair)
        if spread_game:
            merge_thespread_into_game(game, spread_game)
            spread_merged += 1
        vsin_game = _find_aligned(game, vsin_by_pair)
        if vsin_game:
            merge_vsin_into_game(game, vsin_game)
            vsin_merged += 1

    dk_pairs = {
        pair_key(g.get("away"), g.get("home"))
        for g in dk.get("games") or []
        if pair_key(g.get("away"), g.get("home"))
    }
    extras_by_pair: dict[tuple[str, str], dict[str, Any]] = {}

    def _already_present(game: dict[str, Any]) -> bool:
        key = pair_key(game.get("away"), game.get("home"))
        return bool(key and key in dk_pairs)

    for spread_game in spread.get("games") or []:
        key = pair_key(spread_game.get("away"), spread_game.get("home"))
        if not key or _already_present(spread_game):
            continue
        extras_by_pair[key] = dict(spread_game)
    for vsin_game in vsin.get("games") or []:
        key = pair_key(vsin_game.get("away"), vsin_game.get("home"))
        if not key or _already_present(vsin_game):
            continue
        if key in extras_by_pair:
            aligned = align_game_to(vsin_game, extras_by_pair[key])
            if aligned:
                merge_vsin_into_game(extras_by_pair[key], aligned)
        else:
            extras_by_pair[key] = dict(vsin_game)
    extras = list(extras_by_pair.values())

    if extras:
        dk.setdefault("games", []).extend(extras)
        dk["games"].sort(
            key=lambda g: g.get("game_time_utc") or g.get("game_time_local") or g.get("matchup") or ""
        )
        dk["game_count"] = len(dk["games"])

    dk["sources"] = {
        "draftkings": {
            "source": dk.get("source"),
            "source_page": dk.get("source_page"),
            "game_count": dk.get("game_count") - len(extras) if extras else dk.get("game_count"),
        },
        "thespread": {
            "source": spread.get("source"),
            "source_page": spread.get("source_page"),
            "game_count": spread.get("game_count"),
            "merged_into_draftkings_games": spread_merged,
        },
        "vsin": {
            "source": vsin.get("source"),
            "source_page": vsin.get("source_page"),
            "game_count": vsin.get("game_count"),
            "merged_into_draftkings_games": vsin_merged,
        },
        "extras_added": len(extras),
    }

    prev_by_matchup = load_previous_games(args.out)
    poly = scrape_polymarket(league="UFC", games=dk.get("games") or [], day=day)
    poly_by_pair = _index_games(poly.get("games") or [])
    poly_merged = 0
    for game in dk.get("games") or []:
        poly_game = _find_aligned(game, poly_by_pair)
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
    dk["source"] = "dknetwork.draftkings.com + thespread.com + data.vsin.com + polymarket"
    dk["league"] = "UFC"
    dk["date"] = day.isoformat()
    dk["scraped_at"] = datetime.now(PAGE_TZ).astimezone().isoformat()

    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(dk, indent=2) + "\n", encoding="utf-8")
    print(
        f"Wrote {dk['game_count']} UFC fights "
        f"(TheSpread merged={spread_merged}, VSiN merged={vsin_merged}, "
        f"Polymarket merged={poly_merged}, extras={len(extras)}) → {args.out}"
    )


if __name__ == "__main__":
    main()
