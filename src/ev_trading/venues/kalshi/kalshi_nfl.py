"""Pull this week's NFL game lines and player props from the Kalshi public API.

Writes `nfl_week_markets.json` next to this file and prints one summary line per game.

Public market-data endpoints do not require auth. Signed RSA-PSS headers are only
attached if a request comes back 401.
"""

from __future__ import annotations

import json
import re
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal

from ev_trading.venues.kalshi.api import (  # noqa: F401
    BASE_URL,
    EVENTS_PAGE_LIMIT,
    MARKETS_PAGE_LIMIT,
    KalshiClient,
)

SCRIPT_DIR = Path(__file__).resolve().parent
OUTPUT_PATH = SCRIPT_DIR / "nfl_week_markets.json"

GameLineType = Literal["moneyline", "spread", "total"]
PlayerPropType = Literal[
    "anytime_td",
    "first_td",
    "2plus_td",
    "passing_yards",
    "receiving_yards",
    "rushing_yards",
    "rushing_receiving_yards",
]

# Weekly game series. KXNFLWINS is a season market and is skipped by default.
GAME_LINE_SERIES: dict[str, GameLineType] = {
    "KXNFLGAME": "moneyline",
    "KXNFLSPREAD": "spread",
    "KXNFLTOTAL": "total",
}
PLAYER_PROP_SERIES: dict[str, PlayerPropType] = {
    "KXNFLANYTD": "anytime_td",
    "KXNFLFIRSTTD": "first_td",
    "KXNFL2TD": "2plus_td",
    "KXNFLPASSYDS": "passing_yards",
    "KXNFLRECYDS": "receiving_yards",
    "KXNFLRSHYDS": "rushing_yards",
    "KXNFLRRYDS": "rushing_receiving_yards",
}
NFL_SERIES_TICKERS: tuple[str, ...] = tuple(GAME_LINE_SERIES) + tuple(PLAYER_PROP_SERIES)


def _as_float(value: Any) -> float | None:
    if value is None or value == "":
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _first_float(market: dict[str, Any], *keys: str) -> float | None:
    for key in keys:
        if key in market and market[key] is not None:
            return _as_float(market[key])
    return None


def _game_key(event_ticker: str, series_ticker: str) -> str:
    """Strip the series prefix so GAME/SPREAD/TOTAL/prop events for one game merge.

    KXNFLGAME-26SEP13ATLPIT and KXNFLFIRSTTD-26SEP13ATLPIT share key 26SEP13ATLPIT.
    """
    prefix = f"{series_ticker}-"
    if event_ticker.startswith(prefix):
        return event_ticker[len(prefix) :]
    _, _, rest = event_ticker.partition("-")
    return rest or event_ticker


def _clean_title(title: str) -> str:
    for suffix in (
        ": Spread",
        ": Total Points",
        ": 1st Touchdown",
        ": Anytime Touchdown Scorer",
        ": Two or More Touchdowns Scorer",
        ": Anytime Touchdown",
        ": First Touchdown",
        ": Passing Yards",
        ": Receiving Yards",
        ": Rushing Yards",
        ": Rushing + Receiving Yards",
    ):
        if title.endswith(suffix):
            return title[: -len(suffix)]
    return title



def _event_markets(client: KalshiClient, event: dict[str, Any]) -> list[dict[str, Any]]:
    nested = event.get("markets")
    if isinstance(nested, list) and nested:
        return [m for m in nested if isinstance(m, dict)]
    event_ticker = str(event.get("event_ticker") or "")
    if not event_ticker:
        return []
    return client.list_event_markets(event_ticker)


_SPREAD_SUB_RE = re.compile(
    r"^(?P<team>.+?)\s+wins by over\s+(?P<line>\d+(?:\.\d+)?)\s+points",
    re.I,
)
_TOTAL_SUB_RE = re.compile(
    r"^Over\s+(?P<line>\d+(?:\.\d+)?)\s+points",
    re.I,
)
_TICKER_TEAM_RE = re.compile(r"-([A-Z]{2,3})(?:\d+)?$")


def parse_spread_subtitle(yes_sub_title: str) -> tuple[str | None, float | None]:
    """'Pittsburgh wins by over 3.5 points' → ('Pittsburgh', 3.5)."""
    match = _SPREAD_SUB_RE.match((yes_sub_title or "").strip())
    if not match:
        return None, None
    return match.group("team").strip(), float(match.group("line"))


def parse_total_subtitle(yes_sub_title: str) -> float | None:
    """'Over 44.5 points scored' → 44.5."""
    match = _TOTAL_SUB_RE.match((yes_sub_title or "").strip())
    if not match:
        return None
    return float(match.group("line"))


def ticker_team_abbr(ticker: str) -> str | None:
    """Trailing team token: KXNFLGAME-...-PIT / KXNFLSPREAD-...-PIT4 → PIT."""
    match = _TICKER_TEAM_RE.search(ticker or "")
    if not match:
        return None
    return match.group(1)


def _game_line_record(market: dict[str, Any], line_type: GameLineType) -> dict[str, Any]:
    yes_sub = market.get("yes_sub_title") or ""
    record: dict[str, Any] = {
        "ticker": market.get("ticker"),
        "type": line_type,
        "yes_sub_title": yes_sub,
        "yes_bid": _first_float(market, "yes_bid_dollars", "yes_bid"),
        "yes_ask": _first_float(market, "yes_ask_dollars", "yes_ask"),
        "no_bid": _first_float(market, "no_bid_dollars", "no_bid"),
        "no_ask": _first_float(market, "no_ask_dollars", "no_ask"),
        "volume": _first_float(market, "volume_fp", "volume"),
    }
    strike = _as_float(market.get("floor_strike"))
    ticker = str(market.get("ticker") or "")
    if line_type == "spread":
        team, parsed_line = parse_spread_subtitle(yes_sub)
        record["line"] = strike if strike is not None else parsed_line
        if team:
            record["team"] = team
        team_abbr = ticker_team_abbr(ticker)
        if team_abbr:
            record["team_abbr"] = team_abbr
    elif line_type == "total":
        parsed_line = parse_total_subtitle(yes_sub)
        record["line"] = strike if strike is not None else parsed_line
    elif line_type == "moneyline":
        team_abbr = ticker_team_abbr(ticker)
        if team_abbr:
            record["team_abbr"] = team_abbr
    return record


def _player_from_subtitle(yes_sub_title: str) -> str:
    """'Aaron Rodgers: 125+' -> 'Aaron Rodgers'; TD props are already just the name."""
    if ": " in yes_sub_title:
        return yes_sub_title.split(": ", 1)[0].strip()
    return yes_sub_title.strip()


def _player_prop_record(market: dict[str, Any], prop_type: PlayerPropType) -> dict[str, Any]:
    yes_sub = str(market.get("yes_sub_title") or "")
    return {
        "ticker": market.get("ticker"),
        "type": prop_type,
        "player": _player_from_subtitle(yes_sub),
        "line": _as_float(market.get("floor_strike")),
        "yes_sub_title": yes_sub,
        "yes_bid": _first_float(market, "yes_bid_dollars", "yes_bid"),
        "yes_ask": _first_float(market, "yes_ask_dollars", "yes_ask"),
        "volume": _first_float(market, "volume_fp", "volume"),
    }


def _midpoint(market: dict[str, Any]) -> float | None:
    bid = market.get("yes_bid")
    ask = market.get("yes_ask")
    if bid is None and ask is None:
        return None
    if bid is None:
        return ask
    if ask is None:
        return bid
    return (bid + ask) / 2.0


def _closest_to_even(markets: list[dict[str, Any]]) -> dict[str, Any] | None:
    """Pick the line whose YES midpoint is nearest 0.50 (the 'main' spread/total)."""
    best: dict[str, Any] | None = None
    best_dist: float | None = None
    for market in markets:
        mid = _midpoint(market)
        if mid is None:
            continue
        dist = abs(mid - 0.5)
        if best_dist is None or dist < best_dist:
            best = market
            best_dist = dist
    return best


def _fmt_px(value: float | None) -> str:
    if value is None:
        return "—"
    return f"{value:.2f}"


def _summarize_game(game: dict[str, Any]) -> str:
    lines: list[dict[str, Any]] = game.get("game_lines") or []
    props: list[dict[str, Any]] = game.get("player_props") or []
    moneylines = [m for m in lines if m.get("type") == "moneyline"]
    spreads = [m for m in lines if m.get("type") == "spread"]
    totals = [m for m in lines if m.get("type") == "total"]

    if moneylines:
        ml_bits = [
            f"{m.get('yes_sub_title') or m.get('ticker')} {_fmt_px(m.get('yes_bid'))}"
            for m in moneylines
        ]
        ml_text = " / ".join(ml_bits)
    else:
        ml_text = "n/a"

    spread = _closest_to_even(spreads)
    if spread:
        label = spread.get("yes_sub_title") or spread.get("ticker")
        spread_text = f"{label} {_fmt_px(spread.get('yes_bid'))}/{_fmt_px(spread.get('yes_ask'))}"
    else:
        spread_text = "n/a"

    total = _closest_to_even(totals)
    if total:
        label = total.get("yes_sub_title") or total.get("ticker")
        total_text = f"{label} {_fmt_px(total.get('yes_bid'))}/{_fmt_px(total.get('yes_ask'))}"
    else:
        total_text = "n/a"

    title = game.get("title") or game.get("event_ticker")
    sub = game.get("sub_title")
    heading = f"{title} ({sub})" if sub else str(title)
    if props:
        type_counts: dict[str, int] = {}
        for prop in props:
            key = str(prop.get("type") or "unknown")
            type_counts[key] = type_counts.get(key, 0) + 1
        breakdown = ", ".join(f"{name} {n}" for name, n in sorted(type_counts.items()))
        props_text = f"{len(props)} ({breakdown})"
    else:
        props_text = "0"
    return (
        f"{heading}  |  ML {ml_text}  |  spread {spread_text}  |  "
        f"total {total_text}  |  props {props_text}"
    )


def fetch_nfl_week(client: KalshiClient) -> list[dict[str, Any]]:
    """Pull open events for each NFL series and group markets by game."""
    games: dict[str, dict[str, Any]] = {}

    for series_ticker in NFL_SERIES_TICKERS:
        series = client.get_series(series_ticker)
        if series is None:
            print(f"skip {series_ticker}: series not found", file=sys.stderr)
            continue

        events = client.list_open_events(series_ticker)
        if not events:
            print(f"skip {series_ticker}: no open events this week", file=sys.stderr)
            continue

        print(
            f"{series_ticker}: {len(events)} open event(s) ({series.get('title') or series_ticker})",
            file=sys.stderr,
        )

        is_game_line = series_ticker in GAME_LINE_SERIES
        line_type = GAME_LINE_SERIES.get(series_ticker)
        prop_type = PLAYER_PROP_SERIES.get(series_ticker)

        for event in events:
            event_ticker = str(event.get("event_ticker") or "")
            if not event_ticker:
                continue
            key = _game_key(event_ticker, series_ticker)
            game = games.setdefault(
                key,
                {
                    "event_ticker": event_ticker,
                    "title": _clean_title(str(event.get("title") or event_ticker)),
                    "sub_title": event.get("sub_title") or "",
                    "close_time": None,
                    "game_lines": [],
                    "player_props": [],
                },
            )

            # Prefer the moneyline event ticker / title as the canonical game identity.
            if series_ticker == "KXNFLGAME":
                game["event_ticker"] = event_ticker
                game["title"] = _clean_title(str(event.get("title") or game["title"]))
                if event.get("sub_title"):
                    game["sub_title"] = event["sub_title"]

            markets = _event_markets(client, event)
            close_times: list[str] = []
            if event.get("close_time"):
                close_times.append(str(event["close_time"]))

            for market in markets:
                close_time = market.get("close_time")
                if close_time:
                    close_times.append(str(close_time))
                if is_game_line and line_type is not None:
                    game["game_lines"].append(_game_line_record(market, line_type))
                elif prop_type is not None:
                    game["player_props"].append(_player_prop_record(market, prop_type))

            if close_times:
                earliest = min(close_times)
                existing = game.get("close_time")
                if existing is None or earliest < str(existing):
                    game["close_time"] = earliest

    def sort_key(game: dict[str, Any]) -> tuple[str, str]:
        return (str(game.get("close_time") or ""), str(game.get("title") or ""))

    def prop_sort_key(prop: dict[str, Any]) -> tuple[str, str, float]:
        line = prop.get("line")
        return (
            str(prop.get("type") or ""),
            str(prop.get("player") or ""),
            float(line) if line is not None else -1.0,
        )

    ordered = sorted(games.values(), key=sort_key)
    for game in ordered:
        game["player_props"] = sorted(game.get("player_props") or [], key=prop_sort_key)
    return ordered


def main() -> int:
    client = KalshiClient.from_env()
    try:
        games = fetch_nfl_week(client)
    finally:
        client.close()

    payload = {
        "generated_at": datetime.now(UTC).isoformat(),
        "games": games,
    }
    OUTPUT_PATH.write_text(json.dumps(payload, indent=2) + "\n")

    print(f"\nWrote {len(games)} game(s) to {OUTPUT_PATH}")
    if not games:
        print("No open NFL games found.")
        return 0
    print()
    for game in games:
        print(_summarize_game(game))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
