#!/usr/bin/env python3
"""
Identify daily "sharp money" plays from combined betting splits.

Input schema (per game.moneyline.away/home, game.spread.away/home, or
game.total.over/under) — confirmed against mlb/wnba/nba/nhl/ufc/ncaaf/nfl_betting_splits.json:

  primary  public_bet_pct / handle_bet_pct
           (MLB: PlayerProps.ai; WNBA/NBA/NHL/UFC/NCAAF/NFL: DraftKings Network)
  sbd      sbd_public_bet_pct / sbd_handle_bet_pct   (MLB + NBA + NHL + NCAAF + NFL)
  prices   eva_open / eva_line first when EVA detected a move, else
           TheSpread open / live. Polymarket history is the moneyline
           price and, for spread/total, only the move direction — its
           American price is never the point line.
           (never splice EVA open against DK live)
  juice    open_odds / live_odds  (spread/total vig; not a substitute for the number)
  ML fair  live American odds, else EVA (no-vig)

Moneyline is the default market. Spread open/live are point-spread numbers,
so RLM uses the number first and falls back to juice implied-prob movement.
Totals use over/under: a rising total confirms Over, a falling total Under.
A negative total, or a spread at +/-100 or beyond, is an American price
that leaked into the line field and is ignored.
WNBA/UFC score DraftKings alone. MLB/NBA/NHL/NCAAF/NFL need PlayerProps or DraftKings
plus SportsBettingDime, and ScoresAndOdds when SAO_VOTES is on. If the primary
feed is null on every game, those leagues require SBD and SAO to agree.
Pinnacle is skipped. RLM source order:
EV Analytics (chart history), then TheSpread open→live, then Polymarket.
SBD is not scraped for WNBA/UFC. Covers is not scraped for UFC;
Polymarket implied prob is used as the exchange fair in that case.
VSiN republished the same DraftKings splits and is no longer scraped.

Qualified plays are then enriched (never filtered) with an exchange_confirmation
block: median de-vig fair from covers_odds books, edge vs sportsbook fair,
Polymarket history RLM, and a liquidity flag. Tier A can upgrade to A+.

Usage:
  python find_sharp_money.py
  python find_sharp_money.py --input output/mlb_betting_splits.json
  python find_sharp_money.py --input output/wnba_betting_splits.json \\
      --out output/wnba_sharp_money.json --csv output/wnba_sharp_money.csv \\
      --market both
  python find_sharp_money.py --input output/ufc_betting_splits.json \\
      --out output/ufc_sharp_money.json --csv output/ufc_sharp_money.csv
  python find_sharp_money.py --input output/ncaaf_betting_splits.json \\
      --out output/ncaaf_sharp_money.json --csv output/ncaaf_sharp_money.csv \\
      --market all
  python find_sharp_money.py --input output/nfl_betting_splits.json \\
      --out output/nfl_sharp_money.json --csv output/nfl_sharp_money.csv \\
      --market all
  python find_sharp_money.py --input output/nba_betting_splits.json \\
      --out output/nba_sharp_money.json --csv output/nba_sharp_money.csv \\
      --market all
  python find_sharp_money.py --input output/nhl_betting_splits.json \\
      --out output/nhl_sharp_money.json --csv output/nhl_sharp_money.csv \\
      --market all
"""

from __future__ import annotations

import argparse
import json
import sys
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from statistics import median
from typing import Any, Literal
from zoneinfo import ZoneInfo

import pandas as pd

SCRIPT_DIR = Path(__file__).resolve().parent
DEFAULT_INPUT = SCRIPT_DIR / "output" / "mlb_betting_splits.json"
DEFAULT_JSON_OUT = SCRIPT_DIR / "output" / "mlb_sharp_money.json"
DEFAULT_CSV_OUT = SCRIPT_DIR / "output" / "mlb_sharp_money.csv"
PAGE_TZ = ZoneInfo("America/Los_Angeles")

Market = Literal["moneyline", "spread", "total"]

# --- Tunable constants (change these; do not hardcode in logic) ---------------

W_PRIMARY = 1.0
W_SBD = 0.75
SAO_VOTES = True
SAO_WEIGHT = 0.75
# "average" divides the weighted sum by the weights of sources that voted,
# so a third source does not inflate the gap past the threshold of 15.
# "sum" is the previous composite.
COMPOSITE_MODE = "average"

TIER_A_THRESHOLD = 15.0
TIER_B_THRESHOLD = 15.0

# Spread/total only. A move of exactly 1 point still qualifies. Moneylines
# stay on implied-probability RLM and are not point-capped.
MAX_POINT_LINE_MOVE = 1.0

# Qualified moneyline outside this no-vig band is bought as that team's spread.
# At or under 0.48, or above 0.60 (the sharp max_ask). Exactly 0.60 stays a
# moneyline. The published row stays a moneyline so the card still shows the ML splits.
# Below the league floor, neither the moneyline nor the spread is traded.
ML_TO_SPREAD_MAX_FAIR = 0.48
ML_TO_SPREAD_HIGH_FAIR = 0.60
# Strictly below this no-vig fair, skip both markets. Leagues not listed still
# buy the spread down through the 0.48 cutoff. CFB uses the NCAAF floor.
ML_SKIP_BELOW_FAIR: dict[str, float] = {
    "NBA": 0.25,
    "NCAAF": 0.20,
    "NFL": 0.20,
}

# Exchange confirmation (enrichment only; never filters plays).
EXCHANGE_RLM_MIN_PP = 1.0
LOW_LIQUIDITY_THRESHOLD = 10_000.0
TIER_A_PLUS_EDGE_PCT = 1.5

# If any source with a reading votes the opposite side, discard the game.
# Neutral/missing sources are not dissent. With two sources, both must agree.
REQUIRE_UNANIMOUS_DIRECTION = True

# Moneyline underdog American odds at/beyond this are flagged as lower-volume.
LOW_PROB_DOG_ODDS_THRESHOLD = 200.0

# Primary RLM line-movement source order (first complete source wins).
RLM_SOURCE_PRIORITY: tuple[str, ...] = ("eva", "thespread", "polymarket")

SOURCE_WEIGHTS: dict[str, float] = {
    "primary": W_PRIMARY,
    "sbd": W_SBD,
    "sao": SAO_WEIGHT,
}
MLB_SOURCES = ("primary", "sbd")
SAO_LEAGUES = frozenset({"MLB", "NBA", "NHL", "NCAAF", "NFL"})
SINGLE_SOURCE_LEAGUES = frozenset({"WNBA", "UFC"})
WNBA_SOURCES = ("primary",)
PUBLIC_PCT_FIELDS = {
    "primary": "public_bet_pct",
    "sbd": "sbd_public_bet_pct",
    "sao": "sao_public_bet_pct",
}
SIDES = ("away", "home")
TOTAL_SIDES = ("over", "under")
ALL_SIDES = SIDES + TOTAL_SIDES
Side = Literal["away", "home", "over", "under"]
SourceName = Literal["primary", "sbd", "sao"]
Tier = Literal["A+", "A", "B"]


def _league_key(league: str | None) -> str:
    key = str(league or "").strip().upper()
    if key == "CFB":
        return "NCAAF"
    return key


def sources_for_league(league: str | None, *, sao_votes: bool | None = None) -> tuple[str, ...]:
    if _league_key(league) in SINGLE_SOURCE_LEAGUES:
        return WNBA_SOURCES
    votes = SAO_VOTES if sao_votes is None else sao_votes
    if votes and _league_key(league) in SAO_LEAGUES:
        return MLB_SOURCES + ("sao",)
    return MLB_SOURCES


def primary_down_note(league: str | None) -> str:
    if _league_key(league) == "MLB":
        return "PlayerProps splits down: SBD + SAO"
    return "DK splits down: SBD + SAO"


def primary_source_label(league: str | None) -> str:
    if _league_key(league) in {"WNBA", "NBA", "NHL", "UFC", "NCAAF", "NFL"}:
        return "draftkings"
    return "playerprops"


# --- Odds helpers -------------------------------------------------------------


def american_to_implied_prob(odds: float) -> float:
    """Convert American odds to implied win probability (vig included).

    Plus odds:  100 / (odds + 100)
    Minus odds: |odds| / (|odds| + 100)
    """
    if odds >= 0:
        return 100.0 / (odds + 100.0)
    return abs(odds) / (abs(odds) + 100.0)


def no_vig_fair_probs(odds_a: float, odds_b: float) -> tuple[float, float]:
    """Standard two-way no-vig (multiplicative) de-vig of American odds.

    Returns (fair_prob_a, fair_prob_b) that sum to 1.0.
    """
    implied_a = american_to_implied_prob(odds_a)
    implied_b = american_to_implied_prob(odds_b)
    total = implied_a + implied_b
    if total <= 0:
        raise ValueError(f"invalid implied probabilities: {implied_a}, {implied_b}")
    return implied_a / total, implied_b / total


def american_to_fair_odds(odds_a: float, odds_b: float) -> tuple[float, float]:
    """No-vig fair American odds for both sides of a two-way market."""
    p_a, p_b = no_vig_fair_probs(odds_a, odds_b)
    return implied_prob_to_american(p_a), implied_prob_to_american(p_b)


def implied_prob_to_american(prob: float) -> float:
    """Convert a fair probability in (0, 1) to American odds."""
    if prob <= 0 or prob >= 1:
        raise ValueError(f"probability must be in (0, 1), got {prob}")
    if prob >= 0.5:
        return round(-100.0 * prob / (1.0 - prob), 2)
    return round(100.0 * (1.0 - prob) / prob, 2)


def _as_float(value: Any) -> float | None:
    if value is None or value == "":
        return None
    try:
        out = float(value)
    except (TypeError, ValueError):
        return None
    if out != out:  # NaN
        return None
    return out


def is_point_line(value: Any, market: str) -> bool:
    """True when `value` is a spread or total, not an American price.

    Totals are positive. Spreads in these leagues stay inside +/-100;
    American odds are integers at or beyond 100, which is what shows up
    when a contract price is stored in the line field.
    """
    num = _as_float(value)
    if num is None:
        return False
    kind = (market or "").strip().lower()
    if kind == "total":
        return num > 0
    if kind == "spread":
        return abs(num) < 100
    return True


def _as_point(value: Any, market: str) -> float | None:
    num = _as_float(value)
    if not is_point_line(num, market):
        return None
    return num


# --- Step 1: per-source gaps --------------------------------------------------


@dataclass(frozen=True)
class SourceGaps:
    """Raw handle − public gap for one side, per source. None = missing data."""

    primary: float | None
    vsin: float | None
    sbd: float | None
    sao: float | None = None

    def as_dict(self) -> dict[str, float | None]:
        return {"primary": self.primary, "vsin": self.vsin, "sbd": self.sbd, "sao": self.sao}


def compute_gaps(side: dict[str, Any] | None) -> SourceGaps:
    """Compute handle_pct − public_pct for each of the three sources.

    Gaps are left as raw percentage-point differences (not binarized).
    """
    if not isinstance(side, dict):
        return SourceGaps(primary=None, vsin=None, sbd=None)

    def _gap(handle_key: str, public_key: str) -> float | None:
        handle = _as_float(side.get(handle_key))
        public = _as_float(side.get(public_key))
        if handle is None or public is None:
            return None
        return handle - public

    return SourceGaps(
        primary=_gap("handle_bet_pct", "public_bet_pct"),
        vsin=_gap("vsin_handle_bet_pct", "vsin_public_bet_pct"),
        sbd=_gap("sbd_handle_bet_pct", "sbd_public_bet_pct"),
        sao=_gap("sao_handle_bet_pct", "sao_public_bet_pct"),
    )


# --- Step 2: direction agreement ---------------------------------------------


@dataclass(frozen=True)
class Agreement:
    """Majority-side result after filtering source direction votes."""

    side: Side | None
    agreeing_sources: tuple[str, ...]
    conflict: bool
    sbd_override: bool = False
    sbd_dissent_gap: float | None = None

    @property
    def n_agreeing(self) -> int:
        return len(self.agreeing_sources)


def _source_favors(away_gaps: SourceGaps, home_gaps: SourceGaps, source: str) -> Side | None:
    """Return the side a source's gap favors (positive gap), or None if flat/missing."""
    away_gap = away_gaps.as_dict()[source]
    home_gap = home_gaps.as_dict()[source]
    away_pos = away_gap is not None and away_gap > 0
    home_pos = home_gap is not None and home_gap > 0
    if away_pos and not home_pos:
        return "away"
    if home_pos and not away_pos:
        return "home"
    if away_pos and home_pos:
        # Non-complementary splits; pick the larger raw gap.
        assert away_gap is not None and home_gap is not None
        return "away" if away_gap >= home_gap else "home"
    return None


def check_agreement(
    away_gaps: SourceGaps,
    home_gaps: SourceGaps,
    sources: tuple[str, ...] = MLB_SOURCES,
    min_agree: int | None = None,
) -> Agreement:
    """Count sources with a positive gap on each side.

    A single-source league (WNBA/UFC) qualifies on that one vote. Every other
    league needs at least two agreeing sources. Opposite votes discard the game.
    """
    votes: dict[Side, list[str]] = {"away": [], "home": []}
    for source in sources:
        favored = _source_favors(away_gaps, home_gaps, source)
        if favored is not None:
            votes[favored].append(source)

    away_n = len(votes["away"])
    home_n = len(votes["home"])
    conflict = away_n > 0 and home_n > 0
    if min_agree is None:
        min_agree = 1 if len(sources) <= 1 else 2

    if conflict and REQUIRE_UNANIMOUS_DIRECTION:
        return Agreement(side=None, agreeing_sources=(), conflict=True)

    if away_n >= min_agree and away_n >= home_n:
        return Agreement(side="away", agreeing_sources=tuple(votes["away"]), conflict=conflict)
    if home_n >= min_agree:
        return Agreement(side="home", agreeing_sources=tuple(votes["home"]), conflict=conflict)
    return Agreement(side=None, agreeing_sources=(), conflict=conflict)


# --- Step 3: composite gap ----------------------------------------------------


def compute_composite(
    side_gaps: SourceGaps,
    agreeing_sources: tuple[str, ...],
    mode: str | None = None,
) -> float:
    """Weighted gap of sources that agree in direction.

    Missing gaps among the agreeing set are skipped (not treated as 0).
    "sum" is weight * gap added together. "average" divides that sum by the
    weights of the sources that had a gap, so the threshold stays 15.
    """
    chosen = COMPOSITE_MODE if mode is None else mode
    gaps = side_gaps.as_dict()
    total = 0.0
    weight = 0.0
    for source in agreeing_sources:
        gap = gaps.get(source)
        if gap is None:
            continue
        source_weight = SOURCE_WEIGHTS[source]
        total += source_weight * gap
        weight += source_weight
    if chosen == "average":
        if weight <= 0:
            return 0.0
        return total / weight
    return total


# --- Step 4: reverse line movement -------------------------------------------


@dataclass(frozen=True)
class RLMResult:
    public_favors: Side | None
    line_moved_toward: Side | None
    rlm: bool
    rlm_confirmed: bool
    rlm_source_used: str | None = None
    rlm_source_conflict: bool = False
    eva_line_moved_toward: Side | None = None
    polymarket_line_moved_toward: Side | None = None
    polymarket_low_liquidity: bool | None = None
    polymarket_liquidity: float | None = None
    open_px: float | None = None
    live_px: float | None = None


def _implied_prob_delta(open_odds: Any, live_odds: Any) -> float | None:
    """live implied prob − open implied prob. Positive = price shortened."""
    opened = _as_float(open_odds)
    live = _as_float(live_odds)
    if opened is None or live is None:
        return None
    return american_to_implied_prob(live) - american_to_implied_prob(opened)


def check_rlm(
    away: dict[str, Any],
    home: dict[str, Any],
    sharp_side: Side,
    market: Market = "moneyline",
    public_field: str = "public_bet_pct",
) -> RLMResult:
    """Detect reverse line movement and whether it confirms `sharp_side`.

    public_favors      = side with higher primary public_bet_pct
    line_moved_toward  = side the market moved toward
    rlm                = line moved against the public side
    rlm_confirmed      = rlm and the move is toward the sharp-money side

    Source order (RLM_SOURCE_PRIORITY): EVA open→live when the chart
    actually moved, then TheSpread open→live (or juice), then Polymarket
    history first→last (skipped as primary when liquidity is below
    LOW_LIQUIDITY_THRESHOLD). Sources are never mixed into one pair.
    """
    away_pub = _as_float(away.get(public_field))
    home_pub = _as_float(home.get(public_field))
    public_favors: Side | None = None
    if away_pub is not None and home_pub is not None:
        if away_pub > home_pub:
            public_favors = "away"
        elif home_pub > away_pub:
            public_favors = "home"

    ts_complete = _thespread_pair_complete(away, home, market)
    ts_move, ts_open, ts_live = _thespread_move(away, home, market, sharp_side)
    poly_move, poly_open, poly_live, poly_liq, poly_low, poly_ready = _polymarket_move(
        away, home, sharp_side
    )
    eva_move, eva_open, eva_live = _eva_move(away, home, market, sharp_side)
    eva_available = eva_open is not None or eva_live is not None or eva_move is not None

    line_moved_toward: Side | None = None
    source_used: str | None = None
    open_px: float | None = None
    live_px: float | None = None
    conflict = False
    poly_usable = poly_ready and poly_low is False

    if eva_move is not None:
        source_used = "eva"
        line_moved_toward = eva_move
        open_px, live_px = eva_open, eva_live
        conflict = (ts_move is not None and ts_move != eva_move) or (
            poly_move is not None and poly_move != eva_move
        )
    elif ts_complete:
        source_used = "thespread"
        line_moved_toward = ts_move
        open_px, live_px = ts_open, ts_live
        conflict = poly_move is not None and ts_move is not None and poly_move != ts_move
    elif poly_usable:
        source_used = "polymarket"
        line_moved_toward = poly_move
        if market == "moneyline":
            open_px, live_px = poly_open, poly_live
    elif eva_available:
        source_used = "eva"
        line_moved_toward = eva_move
        open_px, live_px = eva_open, eva_live
    elif poly_ready:
        source_used = "polymarket"
        line_moved_toward = poly_move
        if market == "moneyline":
            open_px, live_px = poly_open, poly_live

    rlm = (
        public_favors is not None
        and line_moved_toward is not None
        and line_moved_toward != public_favors
    )
    rlm_confirmed = bool(rlm and line_moved_toward == sharp_side)
    return RLMResult(
        public_favors=public_favors,
        line_moved_toward=line_moved_toward,
        rlm=rlm,
        rlm_confirmed=rlm_confirmed,
        rlm_source_used=source_used,
        rlm_source_conflict=conflict,
        eva_line_moved_toward=eva_move,
        polymarket_line_moved_toward=poly_move,
        polymarket_low_liquidity=poly_low,
        polymarket_liquidity=poly_liq,
        open_px=open_px,
        live_px=live_px,
    )


def _thespread_open_live(side: dict[str, Any]) -> tuple[float | None, float | None]:
    return _as_float(side.get("open")), _as_float(side.get("live"))


def _eva_open_live(side: dict[str, Any]) -> tuple[float | None, float | None]:
    return _as_float(side.get("eva_open")), _as_float(side.get("eva_line"))


def _book_point_pair(side: dict[str, Any], market: Market) -> tuple[float | None, float | None]:
    """TheSpread open/live when both are point lines, else the EVA pair.

    A lone DK/TheSpread number is kept when neither book has a full pair,
    so the bet can still say 41.5 after the price history was unusable.
    American prices stuffed into open/live are dropped, not drawn as the line.
    """
    opened, live = _thespread_open_live(side)
    opened = _as_point(opened, market)
    live = _as_point(live, market)
    if opened is not None and live is not None:
        return opened, live
    eva_open, eva_live = _eva_open_live(side)
    eva_open = _as_point(eva_open, market)
    eva_live = _as_point(eva_live, market)
    if eva_open is not None and eva_live is not None:
        return eva_open, eva_live
    if opened is not None or live is not None:
        return opened, live
    return eva_open, eva_live


def _point_pair_fn(market: Market, raw_fn):
    def pair(side: dict[str, Any]) -> tuple[float | None, float | None]:
        opened, live = raw_fn(side)
        return _as_point(opened, market), _as_point(live, market)

    return pair


def _thespread_pair_complete(away: dict[str, Any], home: dict[str, Any], market: Market) -> bool:
    """True when TheSpread itself supplied an open→live (or juice) pair."""
    for side in (away, home):
        opened, live = _thespread_open_live(side)
        if market == "moneyline":
            if opened is not None and live is not None:
                return True
        elif _as_point(opened, market) is not None and _as_point(live, market) is not None:
            return True
        if market != "moneyline":
            if _as_float(side.get("open_odds")) is not None and _as_float(side.get("live_odds")) is not None:
                return True
    return False


def _thespread_move(
    away: dict[str, Any],
    home: dict[str, Any],
    market: Market,
    sharp_side: Side,
) -> tuple[Side | None, float | None, float | None]:
    pair_fn = _thespread_open_live if market == "moneyline" else _point_pair_fn(market, _thespread_open_live)
    if market == "spread":
        moved = _spread_number_moved_toward(away, home, pair_fn)
        if moved is None:
            moved = _juice_moved_toward(away, home)
    elif market == "total":
        moved = _total_number_moved_toward(away, home, pair_fn)
        if moved is None:
            moved = _juice_moved_toward(away, home)
    else:
        moved = _american_odds_moved_toward(away, home, _thespread_open_live)
    sharp = away if sharp_side == "away" else home
    opened, live = pair_fn(sharp)
    return moved, opened, live


def _eva_move(
    away: dict[str, Any],
    home: dict[str, Any],
    market: Market,
    sharp_side: Side,
) -> tuple[Side | None, float | None, float | None]:
    pair_fn = _eva_open_live if market == "moneyline" else _point_pair_fn(market, _eva_open_live)
    if market == "spread":
        moved = _spread_number_moved_toward(away, home, pair_fn)
    elif market == "total":
        moved = _total_number_moved_toward(away, home, pair_fn)
    else:
        moved = _american_odds_moved_toward(away, home, _eva_open_live)
    sharp = away if sharp_side == "away" else home
    opened, live = pair_fn(sharp)
    return moved, opened, live


def _poly_history_pair(poly: dict[str, Any] | None) -> tuple[dict[str, Any] | None, dict[str, Any] | None]:
    if not isinstance(poly, dict):
        return None, None
    history = poly.get("history")
    if not isinstance(history, list) or len(history) < 2:
        return None, None
    first = history[0] if isinstance(history[0], dict) else None
    last = history[-1] if isinstance(history[-1], dict) else None
    return first, last


def _poly_prob_delta(poly: dict[str, Any] | None) -> float | None:
    first, last = _poly_history_pair(poly)
    if first is None or last is None:
        return None
    opened = _as_float(first.get("implied_prob_pct"))
    latest = _as_float(last.get("implied_prob_pct"))
    if opened is None or latest is None:
        return None
    return latest - opened


def _polymarket_moved_toward(away: dict[str, Any], home: dict[str, Any]) -> Side | None:
    """Implied-prob first→last on each side's polymarket.history (all markets)."""
    away_poly = away.get("polymarket") if isinstance(away.get("polymarket"), dict) else None
    home_poly = home.get("polymarket") if isinstance(home.get("polymarket"), dict) else None
    away_delta = _poly_prob_delta(away_poly)
    home_delta = _poly_prob_delta(home_poly)
    if away_delta is not None and home_delta is not None:
        if away_delta > home_delta:
            return "away"
        if home_delta > away_delta:
            return "home"
        return None
    if away_delta is not None and away_delta != 0:
        return "away" if away_delta > 0 else "home"
    if home_delta is not None and home_delta != 0:
        return "home" if home_delta > 0 else "away"
    return None


def _polymarket_move(
    away: dict[str, Any],
    home: dict[str, Any],
    sharp_side: Side,
) -> tuple[Side | None, float | None, float | None, float | None, bool | None, bool]:
    away_poly = away.get("polymarket") if isinstance(away.get("polymarket"), dict) else None
    home_poly = home.get("polymarket") if isinstance(home.get("polymarket"), dict) else None
    moved = _polymarket_moved_toward(away, home)
    liqs = [
        v
        for v in (
            _as_float((away_poly or {}).get("liquidity")),
            _as_float((home_poly or {}).get("liquidity")),
        )
        if v is not None
    ]
    liq = max(liqs) if liqs else None
    low: bool | None = None if liq is None else liq < LOW_LIQUIDITY_THRESHOLD
    sharp_poly = away_poly if sharp_side == "away" else home_poly
    first, last = _poly_history_pair(sharp_poly)
    if first is None or last is None:
        alt = away_poly if sharp_side == "home" else home_poly
        first, last = _poly_history_pair(alt)
    opened = _as_float(first.get("line")) if first else None
    live = _as_float(last.get("line")) if last else None
    ready = first is not None and last is not None
    return moved, opened, live, liq, low, ready


def _open_live_number(side: dict[str, Any]) -> tuple[float | None, float | None]:
    """Display helper: TheSpread pair, else EVA. Does not splice across books."""
    opened, live = _thespread_open_live(side)
    if opened is not None and live is not None:
        return opened, live
    return _eva_open_live(side)


def _spread_number_moved_toward(
    away: dict[str, Any],
    home: dict[str, Any],
    pair_fn=_thespread_open_live,
) -> Side | None:
    """Side whose spread decreased (became more negative / less plus)."""
    votes: list[Side] = []
    away_open, away_live = pair_fn(away)
    home_open, home_live = pair_fn(home)
    if away_open is not None and away_live is not None and away_live != away_open:
        votes.append("home" if away_live > away_open else "away")
    if home_open is not None and home_live is not None and home_live != home_open:
        votes.append("away" if home_live > home_open else "home")
    if not votes:
        return None
    if all(v == votes[0] for v in votes):
        return votes[0]
    return votes[0]


def _total_number_moved_toward(
    over: dict[str, Any],
    under: dict[str, Any],
    pair_fn=_thespread_open_live,
) -> Side | None:
    """Rising total → Over (away slot); falling total → Under (home slot)."""
    over_open, over_live = pair_fn(over)
    if over_open is not None and over_live is not None and over_live != over_open:
        return "away" if over_live > over_open else "home"
    under_open, under_live = pair_fn(under)
    if under_open is not None and under_live is not None and under_live != under_open:
        return "away" if under_live > under_open else "home"
    return None


def _juice_moved_toward(away: dict[str, Any], home: dict[str, Any]) -> Side | None:
    return _implied_move_toward(
        away.get("open_odds"),
        away.get("live_odds"),
        home.get("open_odds"),
        home.get("live_odds"),
    )


def _american_odds_moved_toward(
    away: dict[str, Any],
    home: dict[str, Any],
    pair_fn=_thespread_open_live,
) -> Side | None:
    away_open, away_live = pair_fn(away)
    home_open, home_live = pair_fn(home)
    return _implied_move_toward(away_open, away_live, home_open, home_live)


def _implied_move_toward(
    away_open: Any,
    away_live: Any,
    home_open: Any,
    home_live: Any,
) -> Side | None:
    away_delta = _implied_prob_delta(away_open, away_live)
    home_delta = _implied_prob_delta(home_open, home_live)
    if away_delta is not None and home_delta is not None:
        if away_delta > home_delta:
            return "away"
        if home_delta > away_delta:
            return "home"
        return None
    if away_delta is not None and away_delta != 0:
        return "away" if away_delta > 0 else "home"
    if home_delta is not None and home_delta != 0:
        return "home" if home_delta > 0 else "away"
    return None


# --- Step 5: tier assignment --------------------------------------------------


def assign_tier(
    n_agreeing: int,
    composite_gap: float,
    rlm_confirmed: bool,
    n_sources: int = 3,
    sbd_override: bool = False,
) -> Tier | None:
    """Return 'A' or 'B', or None if the play should not be output.

    Tier A = every active source agrees and the composite clears the threshold.
    WNBA/UFC have one handle/public source (DraftKings), so that vote is Tier A.
    Tier B = all-but-one on a slate with at least three sources.
    """
    if not rlm_confirmed or sbd_override:
        return None
    if n_agreeing == n_sources and n_sources >= 1 and composite_gap >= TIER_A_THRESHOLD:
        return "A"
    # Tier B ("all but one agree") stays defined below for a 3+ source slate.
    # REQUIRE_UNANIMOUS_DIRECTION already drops any opposing vote, and a source
    # with no gap is not a dissenting vote, so B cannot mean "two against one".
    # Leave it dormant instead of qualifying on a silent third source.
    if REQUIRE_UNANIMOUS_DIRECTION:
        return None
    if (
        n_sources >= 3
        and n_agreeing == n_sources - 1
        and n_agreeing >= 2
        and composite_gap >= TIER_B_THRESHOLD
    ):
        return "B"
    return None


def agreement_tier(n_agreeing: int, n_sources: int) -> Tier | None:
    """Tier the agreement count would earn, ignoring RLM, gap, and SBD override."""
    if n_agreeing == n_sources and n_sources >= 1:
        return "A"
    if n_sources >= 3 and n_agreeing == n_sources - 1 and n_agreeing >= 2:
        return "B"
    return None


def _gap_rejects(n_agreeing: int, composite_gap: float, n_sources: int) -> bool:
    if agreement_tier(n_agreeing, n_sources) is None:
        return True
    return composite_gap < TIER_B_THRESHOLD


def point_move_rejects(market: Market, line_move: float | None) -> bool:
    """True when a spread or total moved more than MAX_POINT_LINE_MOVE points."""
    if market not in {"spread", "total"}:
        return False
    if line_move is None:
        return False
    return abs(line_move) > MAX_POINT_LINE_MOVE


def rejection_reasons(
    *,
    rlm_confirmed: bool,
    n_agreeing: int,
    composite_gap: float,
    n_sources: int,
    sbd_override: bool,
    market: Market,
    line_move: float | None,
) -> list[str]:
    """Every failed gate, so a later review can tell them apart."""
    reasons: list[str] = []
    if not rlm_confirmed:
        reasons.append("rlm")
    if _gap_rejects(n_agreeing, composite_gap, n_sources):
        reasons.append("gap")
    if sbd_override:
        reasons.append("sbd_override")
    if point_move_rejects(market, line_move):
        reasons.append("line_move")
    return reasons


# --- Step 6: output row -------------------------------------------------------


def _label_side(side: str | None, market: Market) -> str | None:
    if side is None:
        return None
    if market != "total":
        return side
    if side == "away":
        return "over"
    if side == "home":
        return "under"
    return side


def _fair_prob_for_side(
    away: dict[str, Any], home: dict[str, Any], side: Side, market: Market = "moneyline"
) -> float | None:
    """De-vig live American odds. Spread/total use juice; ML uses live, then EVA."""
    if market in {"spread", "total"}:
        odds_away = _as_float(away.get("live_odds"))
        odds_home = _as_float(home.get("live_odds"))
        if odds_away is None or odds_home is None:
            odds_away = _as_float(away.get("open_odds"))
            odds_home = _as_float(home.get("open_odds"))
        if odds_away is None or odds_home is None:
            odds_away = _as_float(away.get("eva_odds"))
            odds_home = _as_float(home.get("eva_odds"))
        if odds_away is None or odds_home is None:
            return None
    else:
        live_away = _as_float(away.get("live"))
        live_home = _as_float(home.get("live"))
        if live_away is None:
            live_away = _as_float(away.get("eva_line"))
        if live_home is None:
            live_home = _as_float(home.get("eva_line"))
        if live_away is None or live_home is None:
            return None
        odds_away, odds_home = live_away, live_home
    try:
        p_away, p_home = no_vig_fair_probs(odds_away, odds_home)
    except ValueError:
        return None
    return p_away if side == "away" else p_home


def _ml_american_odds(side_data: dict[str, Any]) -> float | None:
    for key in ("live", "eva_line", "sbd_line"):
        odds = _as_float(side_data.get(key))
        if odds is not None:
            return odds
    return None


def _spread_composite_on_side(
    game: dict[str, Any],
    logical_side: Side,
    sources: tuple[str, ...],
) -> float | None:
    """Weighted spread-market gap on the same team side, ignoring tiering/RLM."""
    block = game.get("spread")
    if not isinstance(block, dict):
        return None
    away = block.get("away")
    home = block.get("home")
    if not isinstance(away, dict) or not isinstance(home, dict):
        return None
    gaps = compute_gaps(away if logical_side == "away" else home)
    present = tuple(s for s in sources if gaps.as_dict().get(s) is not None)
    if not present:
        return None
    return compute_composite(gaps, present)


def _team_name(game: dict[str, Any], which: Literal["away", "home"]) -> str:
    name = str(game.get(which) or "").strip()
    if name:
        return name
    abbr = str(game.get(f"{which}_abbr") or "").strip()
    return abbr


def _side_display_name(
    game: dict[str, Any],
    labeled: str | None,
    market: Market,
    fallback: str | None = None,
) -> str:
    if labeled in {"over", "under"}:
        return labeled.capitalize()
    if labeled == "away":
        return _team_name(game, "away") or (fallback or "Away")
    if labeled == "home":
        return _team_name(game, "home") or (fallback or "Home")
    return str(fallback or labeled or "").strip()


def _format_american(odds: float | None) -> str | None:
    val = _as_float(odds)
    if val is None:
        return None
    n = int(round(val))
    return f"+{n}" if n > 0 else str(n)


def _format_number_line(value: float | None, *, signed: bool) -> str | None:
    val = _as_float(value)
    if val is None:
        return None
    if val == int(val):
        n = int(val)
        if signed and n > 0:
            return f"+{n}"
        return str(n)
    text = f"{val:g}"
    if signed and val > 0 and not text.startswith(("+", "-")):
        return f"+{text}"
    return text


def format_play_label(
    *,
    name: str,
    market: Market,
    line: float | None,
    odds: float | None = None,
) -> str:
    """Human play string: 'Purdue +3', 'APP +210', 'Under 49.5'."""
    label = (name or "").strip() or "—"
    if market == "moneyline":
        formatted = _format_american(odds if odds is not None else line)
        return f"{label} {formatted}" if formatted else label
    if not is_point_line(line, market):
        line = None
    formatted = _format_number_line(line, signed=(market == "spread"))
    return f"{label} {formatted}" if formatted else label


def compute_model_confidence(play: dict[str, Any]) -> int:
    """Stable 70–100 UI score from signal quality (not implied_fair_prob)."""
    score = 78 if play.get("tier") == "A+" else 70
    gap = _as_float(play.get("composite_gap")) or 0.0
    score += min(12, int(gap / 10.0))
    n_agree = play.get("n_sources_agreeing") or 0
    try:
        n_agree = int(n_agree)
    except (TypeError, ValueError):
        n_agree = 0
    # Three agreeing votes. SAO counts only when it actually voted; a missing
    # SAO gap is not in agreeing_sources, so it does not earn this point.
    agreeing = play.get("agreeing_sources") or []
    if n_agree >= 3 and isinstance(agreeing, list) and "sao" in agreeing:
        score += 4
    conf = play.get("exchange_confirmation")
    conf = conf if isinstance(conf, dict) else {}
    edge = _as_float(conf.get("exchange_edge_pct"))
    if edge is not None and edge >= TIER_A_PLUS_EDGE_PCT:
        score += 4
    if conf.get("exchange_rlm_confirmed") is True:
        score += 3
    low_liq = conf.get("low_liquidity")
    if low_liq is False or play.get("polymarket_low_liquidity") is False:
        score += 2
    if play.get("rlm_source_conflict"):
        score -= 4
    if play.get("low_volume_dog_flag"):
        score -= 8
    if play.get("ml_spread_divergence"):
        score -= 6
    if low_liq is True or play.get("polymarket_low_liquidity") is True:
        score -= 4
    return max(70, min(100, int(score)))


def _ml_confidence_fields(
    game: dict[str, Any],
    side_data: dict[str, Any],
    logical: Side,
    composite_gap: float,
    sources: tuple[str, ...],
) -> dict[str, Any]:
    """Flags for noisy moneyline underdogs. Never filters the play."""
    odds = _ml_american_odds(side_data)
    low_vol = bool(odds is not None and odds >= LOW_PROB_DOG_ODDS_THRESHOLD)
    spread_composite = _spread_composite_on_side(game, logical, sources)
    spread_rounded = None if spread_composite is None else round(spread_composite, 4)
    diverges = False
    if low_vol:
        if spread_composite is None or spread_composite <= 0:
            diverges = True
        elif spread_composite < TIER_B_THRESHOLD:
            diverges = True
    notes: list[str] = []
    if low_vol:
        notes.append(f"ML dog beyond +{int(LOW_PROB_DOG_ODDS_THRESHOLD)} odds")
    if diverges:
        spread_txt = "missing" if spread_rounded is None else str(spread_rounded)
        notes.append(
            f"spread composite on same side is only {spread_txt} vs ML composite of {round(composite_gap, 2)}"
        )
    return {
        "low_volume_dog_flag": low_vol,
        "ml_spread_divergence": diverges,
        "spread_composite_gap": spread_rounded,
        "confidence_note": "; ".join(notes) if notes else None,
    }


def build_output(
    game: dict[str, Any],
    side: Side,
    side_data: dict[str, Any],
    away: dict[str, Any],
    home: dict[str, Any],
    away_gaps: SourceGaps,
    home_gaps: SourceGaps,
    agreement: Agreement,
    composite_gap: float,
    rlm: RLMResult,
    tier: Tier | None,
    market: Market,
    sources: tuple[str, ...] = MLB_SOURCES,
    public_field: str = "public_bet_pct",
) -> dict[str, Any]:
    """Assemble one qualifying play as a JSON-serializable dict."""
    logical: Side = "away" if side in {"away", "over"} else "home"
    gaps = (away_gaps if logical == "away" else home_gaps).as_dict()
    labeled = _label_side(logical, market) or side
    abbr = side_data.get("selection") or game.get(f"{labeled}_abbr") or game.get(f"{side}_abbr")
    fair = _fair_prob_for_side(away, home, logical, market)
    if market == "moneyline" and (rlm.open_px is not None or rlm.live_px is not None):
        open_px, live_px = rlm.open_px, rlm.live_px
    elif market == "moneyline":
        open_px, live_px = _open_live_number(side_data)
    else:
        rlm_open = _as_point(rlm.open_px, market)
        rlm_live = _as_point(rlm.live_px, market)
        if rlm_open is not None or rlm_live is not None:
            open_px, live_px = rlm_open, rlm_live
        else:
            open_px, live_px = _book_point_pair(side_data, market)
    away_team = _team_name(game, "away")
    home_team = _team_name(game, "home")
    if away_team and home_team:
        matchup_display = f"{away_team} @ {home_team}"
    else:
        matchup_display = game.get("matchup")
    play_name = _side_display_name(game, labeled, market, fallback=str(abbr) if abbr else None)
    if market == "moneyline":
        play_line = _as_float(live_px) if live_px is not None else _ml_american_odds(side_data)
        play_odds = play_line
    else:
        play_line = live_px if live_px is not None else open_px
        play_odds = _as_float(side_data.get("live_odds"))
        if play_odds is None:
            play_odds = _as_float(side_data.get("open_odds"))
        if play_odds is None:
            play_odds = _as_float(side_data.get("eva_odds"))
    open_num = _as_float(open_px)
    live_num = _as_float(live_px)
    line_move = None if open_num is None or live_num is None else round(live_num - open_num, 4)
    pub_labeled = _label_side(rlm.public_favors, market)
    pub_data = away if rlm.public_favors == "away" else home if rlm.public_favors == "home" else None
    row: dict[str, Any] = {
        "matchup": game.get("matchup"),
        "matchup_display": matchup_display,
        "away_team": away_team or None,
        "home_team": home_team or None,
        "game_time_utc": game.get("game_time_utc"),
        "game_time_local": game.get("game_time_local"),
        "date": game.get("date"),
        "event_id": game.get("event_id"),
        "market": market,
        "side": abbr,
        "home_away": labeled,
        "play_label": format_play_label(name=play_name, market=market, line=play_line, odds=play_odds),
        "play_line": play_line,
        "play_odds": play_odds,
        "line_move": line_move,
        "public_bet_pct": _as_float(side_data.get("public_bet_pct")),
        "handle_bet_pct": _as_float(side_data.get("handle_bet_pct")),
        "public_favors_bet_pct": None if pub_data is None else _as_float(pub_data.get(public_field)),
        "vsin_public_bet_pct": _as_float(side_data.get("vsin_public_bet_pct")),
        "vsin_handle_bet_pct": _as_float(side_data.get("vsin_handle_bet_pct")),
        "sbd_public_bet_pct": _as_float(side_data.get("sbd_public_bet_pct")),
        "sbd_handle_bet_pct": _as_float(side_data.get("sbd_handle_bet_pct")),
        "tier": tier,
        "composite_gap": round(composite_gap, 4),
        "primary_gap": gaps["primary"],
        "vsin_gap": gaps["vsin"],
    }
    if "sbd" in sources:
        row["sbd_gap"] = gaps["sbd"]
        row["sbd_override"] = agreement.sbd_override
        row["sbd_dissent_gap"] = agreement.sbd_dissent_gap
    row.update(
        {
            "n_sources_agreeing": agreement.n_agreeing,
            "agreeing_sources": list(agreement.agreeing_sources),
            "rlm_confirmed": rlm.rlm_confirmed,
            "rlm_source_used": rlm.rlm_source_used,
            "rlm_source_conflict": rlm.rlm_source_conflict,
            "public_favors": pub_labeled,
            "public_favors_name": _side_display_name(game, pub_labeled, market),
            "line_moved_toward": _label_side(rlm.line_moved_toward, market),
            "eva_line_moved_toward": _label_side(rlm.eva_line_moved_toward, market),
            "polymarket_line_moved_toward": _label_side(rlm.polymarket_line_moved_toward, market),
            "polymarket_rlm_liquidity": rlm.polymarket_liquidity,
            "polymarket_low_liquidity": rlm.polymarket_low_liquidity,
            "open": open_px,
            "live": live_px,
            "open_odds": side_data.get("open_odds"),
            "live_odds": side_data.get("live_odds"),
            "implied_fair_prob": None if fair is None else round(fair, 6),
        }
    )
    if market == "moneyline":
        row.update(_ml_confidence_fields(game, side_data, logical, composite_gap, sources))
    else:
        row["low_volume_dog_flag"] = None
        row["ml_spread_divergence"] = None
        row["spread_composite_gap"] = None
        row["confidence_note"] = None
    return row


# --- Step 7: exchange confirmation (enrichment only) --------------------------


def _null_exchange_confirmation() -> dict[str, Any]:
    return {
        "exchange_fair_prob": None,
        "exchange_edge_pct": None,
        "exchange_rlm_confirmed": None,
        "polymarket_liquidity": None,
        "polymarket_volume_24hr": None,
        "low_liquidity": None,
        "books_used": [],
    }


def _covers_book_fair_prob(
    book: dict[str, Any], side: Side, market: Market = "moneyline"
) -> float | None:
    """De-vig one book's two-way pair; return fair prob for `side`."""
    block = book.get(market)
    if not isinstance(block, dict):
        return None
    left_key, right_key = ("over", "under") if market == "total" else ("away", "home")
    left = block.get(left_key) if isinstance(block.get(left_key), dict) else {}
    right = block.get(right_key) if isinstance(block.get(right_key), dict) else {}
    if market in {"spread", "total"}:
        odds_left = _as_float(left.get("odds"))
        odds_right = _as_float(right.get("odds"))
    else:
        odds_left = _as_float(left.get("line"))
        odds_right = _as_float(right.get("line"))
    if odds_left is None or odds_right is None:
        return None
    try:
        p_left, p_right = no_vig_fair_probs(odds_left, odds_right)
    except ValueError:
        return None
    return p_left if side in {"away", "over"} else p_right


def _polymarket_block(
    game: dict[str, Any], home_away: Side, market: Market = "moneyline"
) -> dict[str, Any] | None:
    """Embedded polymarket object under the play's market/side, not covers_odds."""
    block = game.get(market)
    if not isinstance(block, dict):
        return None
    side_data = block.get(home_away)
    if not isinstance(side_data, dict):
        return None
    poly = side_data.get("polymarket")
    return poly if isinstance(poly, dict) else None


def _exchange_rlm_confirmed(poly: dict[str, Any] | None) -> bool | None:
    """True if last implied_prob_pct − first is > 1pp on the sharp side's history."""
    if poly is None:
        return None
    history = poly.get("history")
    if not isinstance(history, list) or len(history) < 2:
        return None
    first = history[0] if isinstance(history[0], dict) else None
    last = history[-1] if isinstance(history[-1], dict) else None
    if first is None or last is None:
        return None
    opened = _as_float(first.get("implied_prob_pct"))
    latest = _as_float(last.get("implied_prob_pct"))
    if opened is None or latest is None:
        return None
    return (latest - opened) > EXCHANGE_RLM_MIN_PP


def enrich_with_exchange_data(play: dict[str, Any], game_data: dict[str, Any]) -> dict[str, Any]:
    """Attach exchange_confirmation and optionally upgrade Tier A → A+.

    Never drops a play. Missing covers_odds / polymarket → null fields.
    """
    covers = game_data.get("covers_odds")
    covers_ok = isinstance(covers, dict) and bool(covers)
    home_away = play.get("home_away")
    market: Market = play.get("market") if play.get("market") in {"moneyline", "spread", "total"} else "moneyline"
    side: Side | None = home_away if home_away in ALL_SIDES else None
    poly = _polymarket_block(game_data, side, market) if side is not None else None

    if not covers_ok and poly is None:
        play["exchange_confirmation"] = _null_exchange_confirmation()
        play["model_confidence"] = compute_model_confidence(play)
        return play

    books_used: list[str] = []
    fairs: list[float] = []
    if covers_ok:
        for slug, book in covers.items():
            if not isinstance(book, dict):
                continue
            books_used.append(str(book.get("book") or slug))
            if side is None:
                continue
            fair = _covers_book_fair_prob(book, side, market)
            if fair is not None:
                fairs.append(fair)

    exchange_fair = float(median(fairs)) if fairs else None
    if exchange_fair is None and poly is not None:
        poly_prob = _as_float(poly.get("implied_prob_pct"))
        if poly_prob is not None:
            exchange_fair = poly_prob / 100.0
            if "polymarket" not in books_used:
                books_used.append("polymarket")
    implied = _as_float(play.get("implied_fair_prob"))
    edge: float | None = None
    if exchange_fair is not None and implied is not None:
        # Percentage points vs sportsbook fair prob (0–1).
        edge = round((exchange_fair - implied) * 100.0, 4)

    liq = _as_float(poly.get("liquidity")) if poly else None
    vol = _as_float(poly.get("volume_24hr")) if poly else None
    low_liq: bool | None = None if liq is None else liq < LOW_LIQUIDITY_THRESHOLD
    rlm = _exchange_rlm_confirmed(poly)

    play["exchange_confirmation"] = {
        "exchange_fair_prob": None if exchange_fair is None else round(exchange_fair, 6),
        "exchange_edge_pct": edge,
        "exchange_rlm_confirmed": rlm,
        "polymarket_liquidity": liq,
        "polymarket_volume_24hr": vol,
        "low_liquidity": low_liq,
        "books_used": books_used,
    }

    if (
        play.get("tier") == "A"
        and edge is not None
        and edge >= TIER_A_PLUS_EDGE_PCT
        and rlm is True
        and low_liq is False
    ):
        play["tier"] = "A+"

    play["model_confidence"] = compute_model_confidence(play)
    return play


def _team_spread_quote(
    game: dict[str, Any], home_away: str
) -> tuple[float, float | None, str] | None:
    """Point spread and juice for one team. None when the number is missing."""
    if home_away not in {"away", "home"}:
        return None
    block = game.get("spread")
    if not isinstance(block, dict):
        return None
    side = block.get(home_away)
    if not isinstance(side, dict):
        return None
    opened, live = _book_point_pair(side, "spread")
    line = live if live is not None else opened
    if line is None:
        return None
    odds = _as_float(side.get("live_odds"))
    if odds is None:
        odds = _as_float(side.get("open_odds"))
    if odds is None:
        odds = _as_float(side.get("eva_odds"))
    return line, odds, home_away


def ml_skip_below_fair(league: str | None) -> float | None:
    """No-vig fair below which a qualified moneyline is not traded at all."""
    return ML_SKIP_BELOW_FAIR.get(_league_key(league))


def _attach_ml_trade_target(
    play: dict[str, Any],
    game: dict[str, Any],
    league: str | None = None,
) -> None:
    """Buy the team spread when a qualified ML fair is outside the tradable band.

    At or under 0.48, or above 0.60 (the sharp max_ask). Exactly 0.60 stays
    a moneyline. NBA under 0.25, and NCAAF/NFL under 0.20, skip both markets.
    Card fields (market, play_label, open, live) stay the moneyline.
    """
    if play.get("market") != "moneyline" or play.get("qualified") is not True:
        return
    fair = _as_float(play.get("implied_fair_prob"))
    if fair is None or ML_TO_SPREAD_MAX_FAIR < fair <= ML_TO_SPREAD_HIGH_FAIR:
        return
    resolved = league if league else game.get("league")
    floor = ml_skip_below_fair(str(resolved) if resolved else None)
    if floor is not None and fair <= ML_TO_SPREAD_MAX_FAIR and fair < floor:
        play["trade_market"] = "skip"
        return
    quote = _team_spread_quote(game, str(play.get("home_away") or ""))
    if quote is None:
        play["trade_market"] = "skip"
        return
    line, odds, home_away = quote
    play["trade_market"] = "spread"
    play["trade_line"] = line
    play["trade_odds"] = odds
    play["trade_home_away"] = home_away


def _source_has_gap(away_gaps: SourceGaps, home_gaps: SourceGaps, source: str) -> bool:
    gaps = away_gaps.as_dict()
    home = home_gaps.as_dict()
    return gaps.get(source) is not None or home.get(source) is not None


def _public_source_for(
    away: dict[str, Any],
    home: dict[str, Any],
    *,
    primary_down: bool,
) -> str:
    if not primary_down:
        return "primary"
    for source in ("sbd", "sao"):
        field = PUBLIC_PCT_FIELDS[source]
        if _as_float(away.get(field)) is not None or _as_float(home.get(field)) is not None:
            return source
    return "sbd"


def _slate_primary_unavailable(games: list[dict[str, Any]]) -> bool:
    """True only when every game on the slate is missing primary bet and handle %."""
    if not games:
        return False
    for game in games:
        for market in ("moneyline", "spread", "total"):
            block = game.get(market)
            if not isinstance(block, dict):
                continue
            for side in block.values():
                if not isinstance(side, dict):
                    continue
                if side.get("public_bet_pct") is not None or side.get("handle_bet_pct") is not None:
                    return False
    return True


def process_game(
    game: dict[str, Any],
    market: Market = "moneyline",
    sources: tuple[str, ...] = MLB_SOURCES,
    *,
    primary_down: bool = False,
    down_note: str | None = None,
    league: str | None = None,
) -> dict[str, Any] | None:
    """Run steps 1–5, then exchange enrichment.

    Returns a row when the sources agree, including plays the gates drop.
    Those rows have qualified false and are not posted or traded.
    """
    block = game.get(market)
    if not isinstance(block, dict):
        return None
    if market == "total":
        away = block.get("over")
        home = block.get("under")
    else:
        away = block.get("away")
        home = block.get("home")
    if not isinstance(away, dict) or not isinstance(home, dict):
        return None

    away_gaps = compute_gaps(away)
    home_gaps = compute_gaps(home)
    present = tuple(s for s in sources if _source_has_gap(away_gaps, home_gaps, s))
    floor = 1 if len(sources) <= 1 else 2
    if primary_down:
        if "sbd" not in present or "sao" not in present:
            return None
        present = tuple(s for s in ("sbd", "sao") if s in present)
        floor = 2
    agreement = check_agreement(away_gaps, home_gaps, sources=present, min_agree=floor)
    if agreement.side is None:
        return None

    side = _label_side(agreement.side, market) or agreement.side
    side_data = away if agreement.side == "away" else home
    side_gaps = away_gaps if agreement.side == "away" else home_gaps
    composite_gap = compute_composite(side_gaps, agreement.agreeing_sources)
    public_source = _public_source_for(away, home, primary_down=primary_down)
    public_field = PUBLIC_PCT_FIELDS.get(public_source, "public_bet_pct")
    rlm = check_rlm(away, home, agreement.side, market=market, public_field=public_field)
    n_sources = len(present)
    tier = assign_tier(
        agreement.n_agreeing,
        composite_gap,
        rlm.rlm_confirmed,
        n_sources=n_sources,
        sbd_override=agreement.sbd_override,
    )
    play = build_output(
        game=game,
        side=side,
        side_data=side_data,
        away=away,
        home=home,
        away_gaps=away_gaps,
        home_gaps=home_gaps,
        agreement=agreement,
        composite_gap=composite_gap,
        rlm=rlm,
        tier=tier,
        market=market,
        sources=sources,
        public_field=public_field,
    )
    reasons = rejection_reasons(
        rlm_confirmed=rlm.rlm_confirmed,
        n_agreeing=agreement.n_agreeing,
        composite_gap=composite_gap,
        n_sources=n_sources,
        sbd_override=agreement.sbd_override,
        market=market,
        line_move=_as_float(play.get("line_move")),
    )
    # A large point move is recorded, not a veto. The review gate still
    # trades the play when it matches another profitable system.
    qualified = tier is not None
    play["qualified"] = qualified
    play["reject_reasons"] = reasons
    play["would_be_tier"] = agreement_tier(agreement.n_agreeing, n_sources)
    if not qualified:
        play["tier"] = None
    _attach_ml_trade_target(play, game, league=league or game.get("league"))
    play = enrich_with_exchange_data(play, game)
    play["sao_gap"] = side_gaps.as_dict().get("sao")
    play["sources_voted"] = list(agreement.agreeing_sources)
    play["primary_status"] = "unavailable" if primary_down else "available"
    play["public_side_source"] = public_source
    if primary_down and down_note:
        play["primary_down_note"] = down_note
    return play


def _market_source_present(block: Any, public_key: str, handle_key: str) -> bool:
    if not isinstance(block, dict):
        return False
    for side in block.values():
        if not isinstance(side, dict):
            continue
        if side.get(public_key) is not None or side.get(handle_key) is not None:
            return True
    return False


def _correlation_rows(payload: dict[str, Any], markets: tuple[Market, ...]) -> list[dict[str, Any]]:
    """One row per game/market where both SBD and SAO published a number."""
    league = payload.get("league")
    rows: list[dict[str, Any]] = []
    for game in payload.get("games") or []:
        if not isinstance(game, dict):
            continue
        for market in markets:
            block = game.get(market)
            if not _market_source_present(block, "sbd_public_bet_pct", "sbd_handle_bet_pct"):
                continue
            if not _market_source_present(block, "sao_public_bet_pct", "sao_handle_bet_pct"):
                continue
            left_key = "over" if market == "total" else "away"
            side = block.get(left_key) if isinstance(block, dict) else None
            side = side if isinstance(side, dict) else {}
            gaps = compute_gaps(side)
            rows.append(
                {
                    "league": league,
                    "date": game.get("date") or payload.get("date"),
                    "event": game.get("event_id") or game.get("matchup"),
                    "market": market,
                    "sbd_gap": gaps.sbd,
                    "sao_gap": gaps.sao,
                    "sbd_public": _as_float(side.get("sbd_public_bet_pct")),
                    "sao_public": _as_float(side.get("sao_public_bet_pct")),
                }
            )
    return rows


def correlation_log_path() -> Path:
    """Same parent search as the sharp candidate log."""
    for parent in SCRIPT_DIR.parents:
        if (parent / "json-data").is_dir():
            return parent / "json-data" / "source_correlation_log.jsonl"
    return SCRIPT_DIR / "output" / "source_correlation_log.jsonl"


def append_correlation_log(rows: list[dict[str, Any]]) -> None:
    if not rows:
        return
    path = correlation_log_path()
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as handle:
            for row in rows:
                handle.write(json.dumps(_json_ready(row)) + "\n")
    except OSError as exc:
        print(f"Warning: source correlation log failed: {exc}", file=sys.stderr)


def process_slate(
    payload: dict[str, Any],
    markets: tuple[Market, ...] = ("moneyline",),
    sources: tuple[str, ...] | None = None,
    *,
    log_correlation: bool = True,
) -> pd.DataFrame:
    """Process every game and return plays sorted by tier (A+ / A / B), then gap desc."""
    active = sources or sources_for_league(payload.get("league"))
    games = [g for g in (payload.get("games") or []) if isinstance(g, dict)]
    primary_down = (
        "primary" in active
        and "sao" in active
        and _slate_primary_unavailable(games)
    )
    vote_sources = tuple(s for s in ("sbd", "sao") if s in active) if primary_down else active
    note = primary_down_note(payload.get("league")) if primary_down else None
    if log_correlation:
        append_correlation_log(_correlation_rows(payload, markets))
    plays: list[dict[str, Any]] = []
    for game in games:
        for market in markets:
            play = process_game(
                game,
                market=market,
                sources=vote_sources,
                primary_down=primary_down,
                down_note=note,
                league=payload.get("league"),
            )
            if play is not None:
                plays.append(play)

    frame = pd.DataFrame(plays)
    if frame.empty:
        return frame
    ranked = frame.assign(
        _tier_rank=frame["tier"].map({"A+": 0, "A": 1, "B": 2}).fillna(3)
    )
    ranked = ranked.sort_values(["_tier_rank", "composite_gap"], ascending=[True, False])
    return ranked.drop(columns=["_tier_rank"]).reset_index(drop=True)


def config_snapshot(
    markets: tuple[Market, ...],
    sources: tuple[str, ...],
    league: str | None,
    *,
    primary_status: str = "available",
    public_side_source: str | None = None,
) -> dict[str, Any]:
    cfg: dict[str, Any] = {
        "market": list(markets) if len(markets) > 1 else markets[0],
        "sources": list(sources),
        "primary_source": primary_source_label(league),
        "primary_status": primary_status,
        "composite_mode": COMPOSITE_MODE,
        "sao_votes": SAO_VOTES,
        "w_primary": W_PRIMARY,
    }
    if "sbd" in sources:
        cfg["w_sbd"] = W_SBD
    if "sao" in sources:
        cfg["w_sao"] = SAO_WEIGHT
    if primary_status == "unavailable":
        cfg["public_side_source"] = public_side_source or "sbd"
        cfg["primary_down_note"] = primary_down_note(league)
    cfg["tier_a_threshold"] = TIER_A_THRESHOLD
    cfg["tier_b_threshold"] = TIER_B_THRESHOLD
    cfg["require_unanimous_direction"] = REQUIRE_UNANIMOUS_DIRECTION
    cfg["low_prob_dog_odds_threshold"] = LOW_PROB_DOG_ODDS_THRESHOLD
    cfg["rlm_source_priority"] = list(RLM_SOURCE_PRIORITY)
    cfg["exchange_rlm_min_pp"] = EXCHANGE_RLM_MIN_PP
    cfg["low_liquidity_threshold"] = LOW_LIQUIDITY_THRESHOLD
    cfg["tier_a_plus_edge_pct"] = TIER_A_PLUS_EDGE_PCT
    cfg["max_point_line_move"] = MAX_POINT_LINE_MOVE
    cfg["ml_to_spread_max_fair"] = ML_TO_SPREAD_MAX_FAIR
    cfg["ml_to_spread_high_fair"] = ML_TO_SPREAD_HIGH_FAIR
    cfg["ml_skip_below_fair"] = dict(ML_SKIP_BELOW_FAIR)
    return cfg


def _slate_public_source(games: list[dict[str, Any]]) -> str:
    for game in games:
        for market in ("moneyline", "spread", "total"):
            if _market_source_present(game.get(market), "sbd_public_bet_pct", "sbd_handle_bet_pct"):
                return "sbd"
    return "sao"


def _slate_config(payload: dict[str, Any], markets: tuple[Market, ...]) -> dict[str, Any]:
    league = payload.get("league")
    sources = sources_for_league(league)
    games = [g for g in (payload.get("games") or []) if isinstance(g, dict)]
    primary_down = "primary" in sources and "sao" in sources and _slate_primary_unavailable(games)
    return config_snapshot(
        markets,
        sources,
        league,
        primary_status="unavailable" if primary_down else "available",
        public_side_source=_slate_public_source(games) if primary_down else None,
    )


def slate_to_json(
    payload: dict[str, Any],
    frame: pd.DataFrame,
    source_file: Path,
    markets: tuple[Market, ...],
) -> dict[str, Any]:
    plays = frame.to_dict(orient="records") if not frame.empty else []
    qualified_n = sum(1 for play in plays if play.get("qualified") is True)
    return {
        "source_file": str(source_file),
        "date": payload.get("date"),
        "league": payload.get("league", "MLB"),
        "generated_at": datetime.now(PAGE_TZ).astimezone().isoformat(),
        "game_count": payload.get("game_count") or len(payload.get("games") or []),
        "play_count": qualified_n,
        "candidate_count": len(plays),
        "config": _slate_config(payload, markets),
        "plays": plays,
    }


def _qualified_frame(frame: pd.DataFrame) -> pd.DataFrame:
    if frame.empty or "qualified" not in frame.columns:
        return frame
    mask = frame["qualified"].map(lambda value: value is True)
    return frame.loc[mask].reset_index(drop=True)


def print_summary(frame: pd.DataFrame) -> None:
    qualified = _qualified_frame(frame)
    rejected = 0 if frame.empty else len(frame) - len(qualified)
    if qualified.empty:
        print("No Tier A/B sharp-money plays.")
    else:
        _print_play_table(qualified)
    if rejected:
        print(f"{rejected} rejected candidate(s) logged (not posted).")


def _print_play_table(frame: pd.DataFrame) -> None:
    display_cols = [
        "tier",
        "market",
        "matchup",
        "play_label",
        "side",
        "model_confidence",
        "composite_gap",
        "primary_gap",
        "vsin_gap",
        "sbd_gap",
        "rlm_confirmed",
        "rlm_source_used",
        "open",
        "live",
        "implied_fair_prob",
    ]
    cols = [c for c in display_cols if c in frame.columns]
    print(frame[cols].to_string(index=False))
    print()
    n_ap = int((frame["tier"] == "A+").sum())
    n_a = int((frame["tier"] == "A").sum())
    n_b = int((frame["tier"] == "B").sum())
    print(f"{len(frame)} play(s)  |  A+={n_ap}  A={n_a}  B={n_b}")


def _frame_for_csv(frame: pd.DataFrame) -> pd.DataFrame:
    """Expand exchange_confirmation into columns so CSV has the same fields as JSON."""
    if frame.empty:
        return frame
    out = frame
    if "agreeing_sources" in out.columns:
        out = out.copy()
        out["agreeing_sources"] = out["agreeing_sources"].apply(
            lambda v: ",".join(v) if isinstance(v, list) else v
        )
    if "sources_voted" in out.columns:
        out = out.copy()
        out["sources_voted"] = out["sources_voted"].apply(
            lambda v: ",".join(v) if isinstance(v, list) else v
        )
    if "exchange_confirmation" not in out.columns:
        return out
    conf = pd.json_normalize(out["exchange_confirmation"].tolist())
    if "books_used" in conf.columns:
        conf["books_used"] = conf["books_used"].apply(
            lambda v: ",".join(v) if isinstance(v, list) else v
        )
    return pd.concat(
        [out.drop(columns=["exchange_confirmation"]).reset_index(drop=True), conf],
        axis=1,
    )


def _markets_from_arg(value: str) -> tuple[Market, ...]:
    if value == "all":
        return ("moneyline", "spread", "total")
    if value == "both":
        return ("moneyline", "spread")
    if value in {"moneyline", "spread", "total"}:
        return (value,)  # type: ignore[return-value]
    raise ValueError(f"unsupported market: {value}")


def _resolve_cli_path(path: Path) -> Path:
    """Keep `output/...` relative to this script, not the caller's cwd.

    scrape_* writes under data-aggregation/output/. Running from trading-bot
    with `--input output/ncaaf_betting_splits.json` used to miss that file.
    """
    if path.is_absolute():
        return path
    if path.parts and path.parts[0] == "output":
        return SCRIPT_DIR / path
    return path


def candidate_log_path() -> Path:
    """Persistent log. The daily sharp JSON is overwritten on the next run."""
    for parent in SCRIPT_DIR.parents:
        if (parent / "json-data").is_dir():
            return parent / "json-data" / "sharp_candidate_log.jsonl"
    return SCRIPT_DIR / "output" / "sharp_candidate_log.jsonl"


def _json_ready(value: Any) -> Any:
    if isinstance(value, float) and value != value:
        return None
    if isinstance(value, dict):
        return {str(key): _json_ready(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_json_ready(item) for item in value]
    item = getattr(value, "item", None)
    if callable(item):
        try:
            return _json_ready(item())
        except (TypeError, ValueError):
            return str(value)
    return value


def _candidate_key(row: dict[str, Any]) -> str:
    return "|".join(
        [
            str(row.get("league") or ""),
            str(row.get("slate_date") or row.get("date") or ""),
            str(row.get("event_id") or row.get("matchup") or ""),
            str(row.get("market") or ""),
            str(row.get("home_away") or row.get("side") or ""),
        ]
    )


def append_candidate_log(output: dict[str, Any], path: Path | None = None) -> int:
    """Write each agreed play once. A re-run replaces the same key."""
    dest = path or candidate_log_path()
    league = output.get("league")
    slate_date = output.get("date")
    incoming: list[dict[str, Any]] = []
    for play in output.get("plays") or []:
        if not isinstance(play, dict):
            continue
        row = _json_ready(play)
        if not isinstance(row, dict):
            continue
        row["league"] = league or row.get("league")
        row["slate_date"] = slate_date or row.get("date")
        incoming.append(row)
    existing: list[dict[str, Any]] = []
    if dest.is_file():
        for line in dest.read_text(encoding="utf-8").splitlines():
            text = line.strip()
            if not text:
                continue
            try:
                parsed = json.loads(text)
            except json.JSONDecodeError:
                continue
            if isinstance(parsed, dict):
                existing.append(parsed)
    by_key = {_candidate_key(row): row for row in existing}
    for row in incoming:
        by_key[_candidate_key(row)] = row
    ordered: list[str] = []
    seen: set[str] = set()
    for row in existing + incoming:
        key = _candidate_key(row)
        if key in seen:
            continue
        seen.add(key)
        ordered.append(key)
    dest.parent.mkdir(parents=True, exist_ok=True)
    body = "".join(
        json.dumps(by_key[key], ensure_ascii=False) + "\n" for key in ordered
    )
    dest.write_text(body, encoding="utf-8")
    return len(incoming)


def main() -> None:
    parser = argparse.ArgumentParser(description="Find sharp-money plays from combined betting splits")
    parser.add_argument("--input", type=Path, default=DEFAULT_INPUT)
    parser.add_argument("--out", type=Path, default=DEFAULT_JSON_OUT)
    parser.add_argument("--csv", type=Path, default=DEFAULT_CSV_OUT)
    parser.add_argument(
        "--market",
        default="moneyline",
        choices=["moneyline", "spread", "total", "both", "all"],
        help="Market to evaluate (NBA/NCAAF/NFL should use 'all'; WNBA 'both' or 'spread')",
    )
    parser.add_argument("--no-discord", action="store_true", help="Skip Discord A/A+ alerts")
    parser.add_argument("--discord-dry-run", action="store_true", help="Print Discord payloads, do not POST")
    parser.add_argument("--discord-force", action="store_true", help="Ignore the sent-play cache")
    parser.add_argument("--no-x", action="store_true", help="Skip X (Twitter) sharp-money tweets")
    parser.add_argument("--x-dry-run", action="store_true", help="Print X payloads, do not tweet")
    parser.add_argument("--no-convex", action="store_true", help="Skip posting plays to Convex")
    parser.add_argument("--convex-dry-run", action="store_true", help="Print Convex payloads, do not POST")
    args = parser.parse_args()
    args.input = _resolve_cli_path(args.input)
    args.out = _resolve_cli_path(args.out)
    args.csv = _resolve_cli_path(args.csv)

    payload = json.loads(args.input.read_text(encoding="utf-8"))
    markets = _markets_from_arg(args.market)
    frame = process_slate(payload, markets=markets)
    output = slate_to_json(payload, frame, args.input, markets)
    logged = append_candidate_log(output)

    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(output, indent=2) + "\n", encoding="utf-8")
    csv_frame = _frame_for_csv(frame)
    csv_frame.to_csv(args.csv, index=False)

    print_summary(frame)
    print(f"JSON → {args.out}")
    print(f"CSV  → {args.csv}")
    print(f"Candidates → {candidate_log_path()} ({logged})")
    if not args.no_discord:
        from discord_sharp_alerts import post_sharp_alerts

        post_sharp_alerts(output, dry_run=args.discord_dry_run, force=args.discord_force)
    if not args.no_x:
        from sharp_tweets import post_sharp_tweets

        post_sharp_tweets(output, dry_run=args.x_dry_run)
    if not args.no_convex:
        from convex_sharp_alerts import post_sharp_plays

        post_sharp_plays(output, dry_run=args.convex_dry_run)


if __name__ == "__main__":
    main()
