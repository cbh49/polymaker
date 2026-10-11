"""Load sharp-money JSON plays produced by data-aggregation/find_sharp_money.py."""

from __future__ import annotations

import json
import math
from collections.abc import Sequence
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any


@dataclass(frozen=True, slots=True)
class SharpPlay:
    league: str
    matchup: str
    side: str
    market: str
    tier: str
    home_away: str | None
    game_time_utc: str | None
    implied_fair_prob: float | None
    rlm_confirmed: bool
    composite_gap: float | None
    source_path: str
    raw: dict[str, Any]
    low_volume_dog_flag: bool = False


def qualifying_trade_data(play: SharpPlay) -> dict[str, Any]:
    """Finder snapshot that qualified this play, stored on the trade row.

    The raw play is the record of tier, gaps, splits, and reverse-line-move.
    Normalized fields the bot traded on are written over the raw copy.
    """
    raw = play.raw if isinstance(play.raw, dict) else {}
    data = _json_safe(raw)
    if not isinstance(data, dict):
        data = {}
    data["league"] = play.league
    data["market"] = play.market
    signal_market = raw.get("signal_market") if isinstance(raw, dict) else None
    if signal_market:
        data["signal_market"] = signal_market
    data["side"] = play.side
    data["tier"] = play.tier
    data["rlm_confirmed"] = play.rlm_confirmed
    data["low_volume_dog_flag"] = play.low_volume_dog_flag
    if play.home_away is not None:
        data["home_away"] = play.home_away
    if play.composite_gap is not None:
        data["composite_gap"] = play.composite_gap
    if play.implied_fair_prob is not None:
        data["implied_fair_prob"] = play.implied_fair_prob
    if play.game_time_utc is not None:
        data["game_time_utc"] = play.game_time_utc
    return data


def _json_safe(value: Any) -> Any:
    if isinstance(value, bool) or value is None or isinstance(value, str):
        return value
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        return value if math.isfinite(value) else None
    if isinstance(value, dict):
        return {str(key): _json_safe(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_json_safe(item) for item in value]
    return str(value)


def load_sharp_file(path: str | Path) -> list[SharpPlay]:
    """Load plays from an mlb/wnba_sharp_money.json file."""
    p = Path(path)
    if not p.is_file():
        raise FileNotFoundError(f"sharp money file not found: {p}")
    data = json.loads(p.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise ValueError(f"expected object in {p}")
    league = str(data.get("league") or "").strip().upper() or _league_from_name(p.name)
    if league == "CFB":
        league = "NCAAF"
    plays_raw = data.get("plays") or []
    if not isinstance(plays_raw, list):
        raise ValueError(f"plays must be a list in {p}")

    out: list[SharpPlay] = []
    for row in plays_raw:
        if not isinstance(row, dict):
            continue
        matchup = str(row.get("matchup") or "").strip()
        side = str(row.get("side") or "").strip()
        market = str(row.get("market") or "moneyline").strip().lower()
        tier = str(row.get("tier") or "").strip().upper()
        if not matchup or not side:
            continue
        if row.get("qualified") is False:
            continue
        fair = row.get("implied_fair_prob")
        gap = row.get("composite_gap")
        out.append(
            SharpPlay(
                league=league,
                matchup=matchup,
                side=side,
                market=market,
                tier=tier or "B",
                home_away=str(row["home_away"]).strip().lower() if row.get("home_away") else None,
                game_time_utc=str(row["game_time_utc"]) if row.get("game_time_utc") else None,
                implied_fair_prob=float(fair) if fair is not None else None,
                rlm_confirmed=bool(row.get("rlm_confirmed")),
                composite_gap=float(gap) if gap is not None else None,
                source_path=str(p),
                raw=row,
                low_volume_dog_flag=bool(row.get("low_volume_dog_flag")),
            )
        )
    return out


def order_play(play: SharpPlay) -> SharpPlay | None:
    """Play the matcher should buy.

    A moneyline card stays a moneyline in the published JSON. When its fair
    is at or under 0.48 the finder sets trade_market to the team's spread
    (or skip, when that spread does not exist).
    """
    raw = play.raw if isinstance(play.raw, dict) else {}
    trade_market = str(raw.get("trade_market") or "").strip().lower()
    if trade_market == "skip":
        return None
    if trade_market != "spread":
        return play
    try:
        line = float(raw["trade_line"])
    except (KeyError, TypeError, ValueError):
        return None
    home_away = str(raw.get("trade_home_away") or play.home_away or "").strip().lower()
    order_raw = dict(raw)
    order_raw["signal_market"] = play.market
    order_raw["signal_open"] = raw.get("open")
    order_raw["signal_live"] = raw.get("live")
    order_raw["signal_play_line"] = raw.get("play_line")
    order_raw["signal_play_odds"] = raw.get("play_odds")
    order_raw["market"] = "spread"
    order_raw["open"] = line
    order_raw["live"] = line
    order_raw["play_line"] = line
    if raw.get("trade_odds") is not None:
        order_raw["play_odds"] = raw.get("trade_odds")
    if home_away:
        order_raw["home_away"] = home_away
    return replace(
        play,
        market="spread",
        home_away=home_away or play.home_away,
        raw=order_raw,
    )


def plays_for_orders(plays: Sequence[SharpPlay]) -> list[SharpPlay]:
    """Drop skipped moneylines and retarget the rest onto the spread."""
    out: list[SharpPlay] = []
    for play in plays:
        ordered = order_play(play)
        if ordered is not None:
            out.append(ordered)
    return out


def load_sharp_plays(paths: Sequence[str | Path]) -> list[SharpPlay]:
    plays: list[SharpPlay] = []
    for path in paths:
        plays.extend(load_sharp_file(path))
    return plays


def _league_from_name(name: str) -> str:
    lower = name.lower()
    if "wnba" in lower:
        return "WNBA"
    if "ufc" in lower:
        return "UFC"
    if "ncaaf" in lower or "cfb" in lower:
        return "NCAAF"
    if "nfl" in lower:
        return "NFL"
    return "MLB"
