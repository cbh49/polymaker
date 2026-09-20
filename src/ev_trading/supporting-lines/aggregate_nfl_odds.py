"""Join RotoWire, Kalshi, and Polymarket NFL odds onto each game and player market.

Sportsbooks stay listed individually. Kalshi and Polymarket contribute one main line
per market — the alternate closest to the sportsbook consensus number.

Usage:
  python aggregate_nfl_odds.py
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

SCRIPT_DIR = Path(__file__).resolve().parent
_SRC = SCRIPT_DIR.parents[1]
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

from ev_trading.nfl_odds import (  # noqa: E402
    CANONICAL_PROP_TYPES,
    ROTOWIRE_PROP_TO_CANONICAL,
    YARDAGE_PROP_TYPES,
    american_to_implied_prob,
    canonical_abbr,
    game_key,
    is_dst_name,
    kalshi_mid,
    kalshi_spread_home_line,
    kalshi_ticker_team,
    kalshi_total_line,
    kickoff_ms,
    match_team_side,
    median_line,
    normalize_player_name,
    parse_kalshi_sub_title,
    pick_closest,
    pick_closest_to_even,
    player_initial_key,
    team_name,
)

DEFAULT_GAME_ODDS = SCRIPT_DIR / "nfl_game_odds.json"
DEFAULT_PLAYER_PROPS = SCRIPT_DIR / "nfl_player_props.json"
DEFAULT_KALSHI = SCRIPT_DIR.parent / "venues" / "kalshi" / "nfl_week_markets.json"
DEFAULT_POLYMARKET = SCRIPT_DIR.parents[2] / "data-aggregation" / "output" / "polymarket_nfl_odds.json"
DEFAULT_OUT = SCRIPT_DIR / "nfl_aggregated_odds.json"


def _load_json(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def _as_float(value: Any) -> float | None:
    if value is None or value == "":
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _round_prob(value: float | None) -> float | None:
    if value is None:
        return None
    return round(float(value), 4)


def _drop_none(row: dict[str, Any]) -> dict[str, Any]:
    return {key: val for key, val in row.items() if val is not None}


def _american_side(odds: Any, *, line: Any = None) -> dict[str, Any] | None:
    implied = american_to_implied_prob(odds)
    if odds is None and line is None:
        return None
    return _drop_none(
        {
            "line": _as_float(line),
            "odds": odds,
            "implied_prob": _round_prob(implied),
        }
    )


def _book_ou(entry: dict[str, Any]) -> dict[str, Any]:
    over = entry.get("overOdds")
    under = entry.get("underOdds")
    odds = entry.get("odds")
    return _drop_none(
        {
            "line": _as_float(entry.get("line")),
            "over_odds": over,
            "under_odds": under,
            "over_implied_prob": _round_prob(american_to_implied_prob(over)),
            "under_implied_prob": _round_prob(american_to_implied_prob(under)),
            "odds": odds if over is None else None,
            "implied_prob": _round_prob(american_to_implied_prob(odds)) if over is None else None,
        }
    )


def _kalshi_price(market: dict[str, Any], extra: dict[str, Any] | None = None) -> dict[str, Any]:
    bid = market.get("yes_bid")
    ask = market.get("yes_ask")
    row = {
        "ticker": market.get("ticker"),
        "yes_sub_title": market.get("yes_sub_title") or None,
        "yes_bid": bid,
        "yes_ask": ask,
        "no_bid": market.get("no_bid"),
        "no_ask": market.get("no_ask"),
        "implied_prob": _round_prob(kalshi_mid(bid, ask)),
        "volume": market.get("volume"),
    }
    if extra:
        row.update(extra)
    return _drop_none(row)


def _poly_game_side(snap: dict[str, Any] | None) -> dict[str, Any] | None:
    if not isinstance(snap, dict):
        return None
    pct = _as_float(snap.get("implied_prob_pct"))
    implied = pct / 100.0 if pct is not None else None
    return _drop_none(
        {
            "line": snap.get("line"),
            "implied_prob": _round_prob(implied),
            "bid": _round_prob(_as_float(snap.get("bid"))),
            "ask": _round_prob(_as_float(snap.get("ask"))),
            "liquidity": snap.get("liquidity"),
            "volume_24hr": snap.get("volume_24hr"),
            "market_id": snap.get("market_id"),
        }
    )


def _poly_mid_from_side(side: dict[str, Any] | None) -> float | None:
    if not isinstance(side, dict):
        return None
    pct = _as_float(side.get("implied_prob_pct"))
    if pct is not None:
        return pct / 100.0
    return american_to_implied_prob(side.get("line"))


def load_rotowire_games(payload: dict[str, Any]) -> dict[tuple[str, str], dict[str, Any]]:
    out: dict[tuple[str, str], dict[str, Any]] = {}
    for period in payload.get("periods") or []:
        if period.get("period") != "game":
            continue
        for game in period.get("games") or []:
            away = game.get("away") or {}
            home = game.get("home") or {}
            key = game_key(str(away.get("abbr") or ""), str(home.get("abbr") or ""))
            if key[0] and key[1]:
                out[key] = game
    return out


def load_rotowire_props(
    payload: dict[str, Any],
) -> dict[str, list[dict[str, Any]]]:
    """gameID → list of canonical player-prop rows with books."""
    by_game: dict[str, list[dict[str, Any]]] = {}
    for market in payload.get("markets") or []:
        prop = str(market.get("prop") or "")
        canonical = ROTOWIRE_PROP_TO_CANONICAL.get(prop)
        if canonical is None:
            continue
        for player in market.get("players") or []:
            if not isinstance(player, dict):
                continue
            game_id = str(player.get("gameID") or "")
            if not game_id:
                continue
            books_raw = player.get("books") or {}
            books = {
                name: _book_ou(entry)
                for name, entry in books_raw.items()
                if isinstance(entry, dict)
            }
            books = {name: row for name, row in books.items() if row}
            by_game.setdefault(game_id, []).append(
                {
                    "player": player.get("name") or "",
                    "playerID": player.get("playerID"),
                    "team": player.get("team"),
                    "opponent": player.get("opponent"),
                    "type": canonical,
                    "books": books,
                }
            )
    return by_game


def load_kalshi_games(
    payload: dict[str, Any],
    slate_keys: set[tuple[str, str]] | None = None,
) -> tuple[dict[tuple[str, str], dict[str, Any]], int]:
    out: dict[tuple[str, str], dict[str, Any]] = {}
    skipped = 0
    for game in payload.get("games") or []:
        parsed = parse_kalshi_sub_title(str(game.get("sub_title") or ""))
        if parsed is None:
            skipped += 1
            continue
        key = game_key(*parsed)
        if slate_keys is not None and key not in slate_keys:
            skipped += 1
            continue
        out[key] = game
    return out, skipped


def load_polymarket_games(
    payload: dict[str, Any],
) -> dict[tuple[str, str], dict[str, Any]]:
    out: dict[tuple[str, str], dict[str, Any]] = {}
    for game in payload.get("games") or []:
        key = game_key(str(game.get("away_abbr") or ""), str(game.get("home_abbr") or ""))
        if key[0] and key[1]:
            out[key] = game
    return out


def _line_delta(alt_line: float | None, consensus: float | None) -> float | None:
    if alt_line is None or consensus is None:
        return None
    return round(alt_line - consensus, 1)


def _select_alt(
    alts: list[dict[str, Any]],
    consensus: float | None,
    *,
    line_key: str,
    mid_of,
) -> dict[str, Any] | None:
    numbered = [alt for alt in alts if alt.get(line_key) is not None]
    if consensus is not None and numbered:
        picked = pick_closest(numbered, consensus, line_key=line_key)
        return dict(picked) if picked is not None else None
    if numbered:
        picked = pick_closest_to_even(numbered, mid_of=mid_of)
        return dict(picked) if picked is not None else numbered[0]
    return alts[0] if alts else None


def _build_moneyline(
    rw: dict[str, Any] | None,
    kalshi: dict[str, Any] | None,
    poly: dict[str, Any] | None,
    *,
    away_abbr: str,
    home_abbr: str,
    away_name: str,
    home_name: str,
) -> dict[str, Any] | None:
    books: dict[str, Any] = {}
    for name, entry in ((rw or {}).get("books") or {}).items():
        if not isinstance(entry, dict):
            continue
        ml = entry.get("moneyline")
        if not isinstance(ml, dict):
            continue
        away = _american_side(ml.get("away"))
        home = _american_side(ml.get("home"))
        if away or home:
            books[name] = _drop_none({"away": away, "home": home})

    kalshi_ml: dict[str, Any] = {}
    for market in (kalshi or {}).get("game_lines") or []:
        if market.get("type") != "moneyline":
            continue
        side = match_team_side(
            str(market.get("yes_sub_title") or ""),
            away_abbr=away_abbr,
            home_abbr=home_abbr,
            away_name=away_name,
            home_name=home_name,
            team_abbr=market.get("team_abbr") or kalshi_ticker_team(str(market.get("ticker") or "")),
        )
        if side:
            kalshi_ml[side] = _kalshi_price(market)

    poly_ml_raw = (poly or {}).get("moneyline") or {}
    poly_ml = _drop_none(
        {
            "away": _poly_game_side(poly_ml_raw.get("away") if isinstance(poly_ml_raw, dict) else None),
            "home": _poly_game_side(poly_ml_raw.get("home") if isinstance(poly_ml_raw, dict) else None),
        }
    )

    row = _drop_none(
        {
            "books": books or None,
            "kalshi": kalshi_ml or None,
            "polymarket": poly_ml or None,
        }
    )
    return row or None


def _build_spread(
    rw: dict[str, Any] | None,
    kalshi: dict[str, Any] | None,
    poly: dict[str, Any] | None,
    *,
    away_abbr: str,
    home_abbr: str,
    away_name: str,
    home_name: str,
) -> dict[str, Any] | None:
    books: dict[str, Any] = {}
    home_lines: list[float] = []
    for name, entry in ((rw or {}).get("books") or {}).items():
        if not isinstance(entry, dict):
            continue
        spread = entry.get("spread")
        if not isinstance(spread, dict):
            continue
        away_side = spread.get("away") if isinstance(spread.get("away"), dict) else {}
        home_side = spread.get("home") if isinstance(spread.get("home"), dict) else {}
        away = _american_side(away_side.get("odds"), line=away_side.get("line"))
        home = _american_side(home_side.get("odds"), line=home_side.get("line"))
        if away or home:
            books[name] = _drop_none({"away": away, "home": home})
        home_line = _as_float(home_side.get("line"))
        if home_line is not None:
            home_lines.append(home_line)
    consensus = median_line(home_lines)

    kalshi_alts: list[dict[str, Any]] = []
    for market in (kalshi or {}).get("game_lines") or []:
        if market.get("type") != "spread":
            continue
        home_line = kalshi_spread_home_line(
            market,
            away_abbr=away_abbr,
            home_abbr=home_abbr,
            away_name=away_name,
            home_name=home_name,
        )
        if home_line is None:
            continue
        kalshi_alts.append({**market, "home_line": home_line})
    kalshi_picked = _select_alt(
        kalshi_alts,
        consensus,
        line_key="home_line",
        mid_of=lambda m: kalshi_mid(m.get("yes_bid"), m.get("yes_ask")),
    )
    kalshi_row = None
    if kalshi_picked is not None:
        home_line = _as_float(kalshi_picked.get("home_line"))
        kalshi_row = _kalshi_price(
            kalshi_picked,
            extra={
                "home_line": home_line,
                "line": home_line,
                "line_delta": _line_delta(home_line, consensus),
                "team": kalshi_picked.get("team"),
                "team_abbr": kalshi_picked.get("team_abbr"),
            },
        )

    poly_row = None
    alts = list((poly or {}).get("spread_alts") or [])
    if alts:
        picked = _select_alt(
            alts,
            consensus,
            line_key="home_line",
            mid_of=lambda m: _poly_mid_from_side(m.get("home")),
        )
        if picked is not None:
            home_line = _as_float(picked.get("home_line"))
            poly_row = _drop_none(
                {
                    "home_line": home_line,
                    "points": picked.get("points"),
                    "line_delta": _line_delta(home_line, consensus),
                    "away": _poly_game_side(picked.get("away")),
                    "home": _poly_game_side(picked.get("home")),
                    "market_id": picked.get("market_id"),
                    "liquidity": picked.get("liquidity"),
                }
            )
    else:
        spread = (poly or {}).get("spread") or {}
        if isinstance(spread, dict) and (spread.get("away") or spread.get("home")):
            home_line = _as_float(spread.get("home_line"))
            poly_row = _drop_none(
                {
                    "home_line": home_line,
                    "points": spread.get("points"),
                    "line_delta": _line_delta(home_line, consensus),
                    "away": _poly_game_side(spread.get("away")),
                    "home": _poly_game_side(spread.get("home")),
                }
            )

    row = _drop_none(
        {
            "consensus_line": consensus,
            "books": books or None,
            "kalshi": kalshi_row,
            "polymarket": poly_row,
        }
    )
    return row or None


def _build_total(
    rw: dict[str, Any] | None,
    kalshi: dict[str, Any] | None,
    poly: dict[str, Any] | None,
) -> dict[str, Any] | None:
    books: dict[str, Any] = {}
    lines: list[float] = []
    for name, entry in ((rw or {}).get("books") or {}).items():
        if not isinstance(entry, dict):
            continue
        total = entry.get("overUnder")
        if not isinstance(total, dict):
            continue
        row = _book_ou(total)
        if row:
            books[name] = row
        line = _as_float(total.get("line"))
        if line is not None:
            lines.append(line)
    consensus = median_line(lines)

    kalshi_alts: list[dict[str, Any]] = []
    for market in (kalshi or {}).get("game_lines") or []:
        if market.get("type") != "total":
            continue
        line = kalshi_total_line(market)
        if line is None:
            continue
        kalshi_alts.append({**market, "line": line})
    kalshi_picked = _select_alt(
        kalshi_alts,
        consensus,
        line_key="line",
        mid_of=lambda m: kalshi_mid(m.get("yes_bid"), m.get("yes_ask")),
    )
    kalshi_row = None
    if kalshi_picked is not None:
        line = _as_float(kalshi_picked.get("line"))
        kalshi_row = _kalshi_price(
            kalshi_picked,
            extra={"line": line, "line_delta": _line_delta(line, consensus)},
        )

    poly_row = None
    alts = list((poly or {}).get("total_alts") or [])
    if alts:
        picked = _select_alt(
            alts,
            consensus,
            line_key="points",
            mid_of=lambda m: _poly_mid_from_side(m.get("over")),
        )
        if picked is not None:
            points = _as_float(picked.get("points"))
            poly_row = _drop_none(
                {
                    "line": points,
                    "points": points,
                    "line_delta": _line_delta(points, consensus),
                    "over": _poly_game_side(picked.get("over")),
                    "under": _poly_game_side(picked.get("under")),
                    "market_id": picked.get("market_id"),
                    "liquidity": picked.get("liquidity"),
                }
            )
    else:
        total = (poly or {}).get("total") or {}
        if isinstance(total, dict) and (total.get("over") or total.get("under")):
            points = _as_float(total.get("points"))
            poly_row = _drop_none(
                {
                    "line": points,
                    "points": points,
                    "line_delta": _line_delta(points, consensus),
                    "over": _poly_game_side(total.get("over")),
                    "under": _poly_game_side(total.get("under")),
                }
            )

    row = _drop_none(
        {
            "consensus_line": consensus,
            "books": books or None,
            "kalshi": kalshi_row,
            "polymarket": poly_row,
        }
    )
    return row or None


def _player_lookup(rows: list[dict[str, Any]]) -> tuple[dict[str, dict[str, Any]], dict[tuple[str, str], list[str]]]:
    by_norm: dict[str, dict[str, Any]] = {}
    by_initial: dict[tuple[str, str], list[str]] = {}
    for row in rows:
        name = str(row.get("player") or "")
        if is_dst_name(name):
            continue
        key = normalize_player_name(name)
        if not key:
            continue
        by_norm.setdefault(key, row)
        initial = player_initial_key(name)
        if initial is not None:
            by_initial.setdefault(initial, [])
            if key not in by_initial[initial]:
                by_initial[initial].append(key)
    return by_norm, by_initial


def _resolve_player(
    name: str,
    by_norm: dict[str, dict[str, Any]],
    by_initial: dict[tuple[str, str], list[str]],
) -> str | None:
    if is_dst_name(name):
        return None
    key = normalize_player_name(name)
    if key in by_norm:
        return key
    initial = player_initial_key(name)
    if initial is None:
        return key or None
    matches = by_initial.get(initial) or []
    if len(matches) == 1:
        return matches[0]
    return key or None


def _prop_books_consensus(books: dict[str, Any]) -> float | None:
    return median_line(
        [row["line"] for row in books.values() if isinstance(row, dict) and row.get("line") is not None]
    )


def _kalshi_prop_quote(market: dict[str, Any], consensus: float | None) -> dict[str, Any]:
    line = _as_float(market.get("line"))
    return _kalshi_price(
        market,
        extra={"line": line, "line_delta": _line_delta(line, consensus)},
    )


def _poly_prop_quote(market: dict[str, Any], consensus: float | None) -> dict[str, Any]:
    line = _as_float(market.get("line"))
    over = _as_float(market.get("over"))
    return _drop_none(
        {
            "line": line,
            "line_delta": _line_delta(line, consensus),
            "over": over,
            "under": _as_float(market.get("under")),
            "over_bid": _round_prob(_as_float(market.get("over_bid"))),
            "over_ask": _round_prob(_as_float(market.get("over_ask"))),
            "under_bid": _round_prob(_as_float(market.get("under_bid"))),
            "under_ask": _round_prob(_as_float(market.get("under_ask"))),
            "implied_prob": _round_prob(over),
            "volume": market.get("volume"),
            "liquidity": market.get("liquidity"),
            "market_id": market.get("market_id"),
            "slug": market.get("slug"),
            "yes_sub_title": market.get("yes_sub_title") or None,
        }
    )


def _build_player_props(
    rw_props: list[dict[str, Any]],
    kalshi: dict[str, Any] | None,
    poly: dict[str, Any] | None,
) -> tuple[list[dict[str, Any]], dict[str, int]]:
    grouped: dict[tuple[str, str], dict[str, Any]] = {}
    by_norm, by_initial = _player_lookup(rw_props)

    def _ensure(norm: str, *, name: str, prop_type: str, meta: dict[str, Any] | None = None) -> dict[str, Any]:
        key = (norm, prop_type)
        row = grouped.get(key)
        if row is None:
            source = meta or {}
            row = {
                "player": source.get("player") or name,
                "playerID": source.get("playerID"),
                "team": source.get("team"),
                "type": prop_type,
                "books": dict(source.get("books") or {}),
            }
            grouped[key] = row
        return row

    for rw_row in rw_props:
        name = str(rw_row.get("player") or "")
        norm = normalize_player_name(name)
        if not norm:
            continue
        _ensure(norm, name=name, prop_type=str(rw_row["type"]), meta=rw_row)

    kalshi_by_key: dict[tuple[str, str], list[dict[str, Any]]] = {}
    for market in (kalshi or {}).get("player_props") or []:
        name = str(market.get("player") or "")
        prop_type = str(market.get("type") or "")
        if prop_type not in CANONICAL_PROP_TYPES:
            continue
        norm = _resolve_player(name, by_norm, by_initial)
        if not norm:
            continue
        kalshi_by_key.setdefault((norm, prop_type), []).append(market)
        meta = by_norm.get(norm)
        _ensure(norm, name=name, prop_type=prop_type, meta=meta)

    poly_by_key: dict[tuple[str, str], list[dict[str, Any]]] = {}
    for market in (poly or {}).get("player_props") or []:
        name = str(market.get("player") or "")
        prop_type = str(market.get("type") or "")
        if prop_type not in CANONICAL_PROP_TYPES:
            continue
        if is_dst_name(name):
            continue
        norm = _resolve_player(name, by_norm, by_initial)
        if not norm:
            continue
        poly_by_key.setdefault((norm, prop_type), []).append(market)
        meta = by_norm.get(norm)
        _ensure(norm, name=name, prop_type=prop_type, meta=meta)

    stats = {"with_books": 0, "kalshi": 0, "polymarket": 0}
    out: list[dict[str, Any]] = []
    for (norm, prop_type), row in sorted(grouped.items(), key=lambda item: (item[0][0], item[0][1])):
        books = row.get("books") or {}
        consensus = _prop_books_consensus(books)
        kalshi_alts = kalshi_by_key.get((norm, prop_type), [])
        poly_alts = poly_by_key.get((norm, prop_type), [])

        kalshi_picked = _select_alt(
            kalshi_alts,
            consensus if prop_type in YARDAGE_PROP_TYPES else None,
            line_key="line",
            mid_of=lambda m: kalshi_mid(m.get("yes_bid"), m.get("yes_ask")),
        )
        poly_picked = _select_alt(
            poly_alts,
            consensus if prop_type in YARDAGE_PROP_TYPES else None,
            line_key="line",
            mid_of=lambda m: _as_float(m.get("over")),
        )

        built = _drop_none(
            {
                "player": row.get("player"),
                "playerID": row.get("playerID"),
                "team": row.get("team"),
                "type": prop_type,
                "consensus_line": consensus,
                "books": books or None,
                "kalshi": _kalshi_prop_quote(kalshi_picked, consensus) if kalshi_picked else None,
                "polymarket": _poly_prop_quote(poly_picked, consensus) if poly_picked else None,
            }
        )
        out.append(built)
        if books:
            stats["with_books"] += 1
        if kalshi_picked:
            stats["kalshi"] += 1
        if poly_picked:
            stats["polymarket"] += 1
    return out, stats


def _team_fields(rw: dict[str, Any] | None, poly: dict[str, Any] | None, abbr: str, side: str) -> dict[str, Any]:
    if rw:
        blob = rw.get(side) or {}
        return {
            "team": blob.get("team") or team_name(abbr),
            "abbr": canonical_abbr(str(blob.get("abbr") or abbr)),
            "nickname": blob.get("nickname"),
        }
    return {
        "team": (poly or {}).get(side) or team_name(abbr),
        "abbr": canonical_abbr(abbr),
    }


def build_game(
    key: tuple[str, str],
    *,
    rw: dict[str, Any] | None,
    kalshi: dict[str, Any] | None,
    poly: dict[str, Any] | None,
    rw_props: list[dict[str, Any]],
) -> dict[str, Any]:
    away_abbr, home_abbr = key
    away = _team_fields(rw, poly, away_abbr, "away")
    home = _team_fields(rw, poly, home_abbr, "home")
    away_name = str(away.get("team") or "")
    home_name = str(home.get("team") or "")
    date = None
    start_time_ms = None
    if rw and rw.get("gameDate"):
        date = str(rw["gameDate"])[:10]
        start_time_ms = kickoff_ms(rw.get("gameDate"))
    elif poly and poly.get("date"):
        date = str(poly["date"])[:10]

    markets = _drop_none(
        {
            "moneyline": _build_moneyline(
                rw,
                kalshi,
                poly,
                away_abbr=away_abbr,
                home_abbr=home_abbr,
                away_name=away_name,
                home_name=home_name,
            ),
            "spread": _build_spread(
                rw,
                kalshi,
                poly,
                away_abbr=away_abbr,
                home_abbr=home_abbr,
                away_name=away_name,
                home_name=home_name,
            ),
            "total": _build_total(rw, kalshi, poly),
        }
    )
    player_props, prop_stats = _build_player_props(rw_props, kalshi, poly)
    matchup = f"{away['abbr']} @ {home['abbr']}"
    if poly and poly.get("matchup"):
        matchup = str(poly["matchup"])
    return {
        "gameID": (rw or {}).get("gameID"),
        "matchup": matchup,
        "date": date,
        "start_time_ms": start_time_ms,
        "away": _drop_none(away),
        "home": _drop_none(home),
        "markets": markets,
        "player_props": player_props,
        "_prop_stats": prop_stats,
        "kalshi_event_ticker": (kalshi or {}).get("event_ticker"),
        "polymarket_event_slug": (poly or {}).get("polymarket_event_slug"),
    }


def aggregate_nfl_slate(
    game_odds: dict[str, Any],
    player_props: dict[str, Any],
    kalshi: dict[str, Any],
    polymarket: dict[str, Any],
) -> dict[str, Any]:
    rw_games = load_rotowire_games(game_odds)
    rw_props = load_rotowire_props(player_props)
    kalshi_games, kalshi_skipped = load_kalshi_games(kalshi, slate_keys=set(rw_games) or None)
    poly_games = load_polymarket_games(polymarket)

    keys = list(rw_games)
    if not keys:
        keys = list(dict.fromkeys([*poly_games, *kalshi_games]))

    games: list[dict[str, Any]] = []
    prop_stats = {"with_books": 0, "kalshi": 0, "polymarket": 0}
    for key in keys:
        rw = rw_games.get(key)
        game_id = str((rw or {}).get("gameID") or "")
        built = build_game(
            key,
            rw=rw,
            kalshi=kalshi_games.get(key),
            poly=poly_games.get(key),
            rw_props=rw_props.get(game_id, []),
        )
        stats = built.pop("_prop_stats")
        for name, count in stats.items():
            prop_stats[name] = prop_stats.get(name, 0) + count
        games.append(_drop_none(built))

    return {
        "generated_at": datetime.now(UTC).isoformat(),
        "game_count": len(games),
        "kalshi_games_skipped": kalshi_skipped,
        "prop_stats": prop_stats,
        "sources": {
            "rotowire_games": game_odds.get("source"),
            "rotowire_props": player_props.get("source"),
            "kalshi_generated_at": kalshi.get("generated_at"),
            "polymarket": polymarket.get("source"),
        },
        "games": games,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="Aggregate NFL sportsbook + Kalshi + Polymarket odds")
    parser.add_argument("--game-odds", type=Path, default=DEFAULT_GAME_ODDS)
    parser.add_argument("--player-props", type=Path, default=DEFAULT_PLAYER_PROPS)
    parser.add_argument("--kalshi", type=Path, default=DEFAULT_KALSHI)
    parser.add_argument("--polymarket", type=Path, default=DEFAULT_POLYMARKET)
    parser.add_argument("--out", type=Path, default=DEFAULT_OUT)
    args = parser.parse_args()

    result = aggregate_nfl_slate(
        _load_json(args.game_odds),
        _load_json(args.player_props),
        _load_json(args.kalshi),
        _load_json(args.polymarket),
    )
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(result, indent=2) + "\n", encoding="utf-8")
    stats = result["prop_stats"]
    print(
        f"Joined {result['game_count']} games → {args.out}  |  "
        f"props books={stats['with_books']} kalshi={stats['kalshi']} "
        f"polymarket={stats['polymarket']}  |  "
        f"kalshi skipped (off-slate)={result['kalshi_games_skipped']}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
