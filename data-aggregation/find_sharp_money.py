#!/usr/bin/env python3
"""
Identify daily "sharp money" plays from combined betting splits.

Input schema (per game.moneyline.away/home, game.spread.away/home, or
game.total.over/under) — confirmed against mlb/wnba/ufc/ncaaf_betting_splits.json:

  primary  public_bet_pct / handle_bet_pct
           (MLB: PlayerProps.ai; WNBA/UFC/NCAAF: DraftKings Network)
  sbd      sbd_public_bet_pct / sbd_handle_bet_pct   (MLB + NCAAF)
  vsin     vsin_public_bet_pct / vsin_handle_bet_pct
  prices   eva_open / eva_line first when EVA detected a move, else
           TheSpread open / live, else Polymarket history
           (never splice EVA open against DK live)
  juice    open_odds / live_odds  (spread/total vig when the number is flat)
  vsin ML  vsin_line    (American odds, used for no-vig fair probability)

Moneyline is the default market. Spread open/live are point-spread numbers,
so RLM uses the number first and falls back to juice implied-prob movement.
Totals use over/under: a rising total confirms Over, a falling total Under.
WNBA/UFC weight DraftKings (primary) + VSiN only. NCAAF is three-source
(DK + VSiN + SBD) like MLB; Pinnacle is skipped. RLM source order:
EV Analytics (chart history), then TheSpread open→live, then Polymarket.
SBD is not scraped for WNBA/UFC. Covers is not scraped for UFC;
Polymarket implied prob is used as the exchange fair in that case.

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
"""

from __future__ import annotations

import argparse
import json
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

W_VSIN = 1.5
W_PRIMARY = 1.0
W_SBD = 0.75

TIER_A_THRESHOLD = 15.0
TIER_B_THRESHOLD = 15.0

# Exchange confirmation (enrichment only; never filters plays).
EXCHANGE_RLM_MIN_PP = 1.0
LOW_LIQUIDITY_THRESHOLD = 10_000.0
TIER_A_PLUS_EDGE_PCT = 1.5

# If any source with a reading votes the opposite side, discard the game.
# Neutral/missing sources are not dissent, so Tier B still fires when exactly
# two sources agree and the third is missing or has a zero gap.
# Softened for SBD-only dissent: see STRONG_SOURCE_GAP_THRESHOLD.
REQUIRE_UNANIMOUS_DIRECTION = True

# Per-source handle−public gap (not composite). When VSiN and DK both clear
# this and agree, SBD voting the other way does not discard the game.
STRONG_SOURCE_GAP_THRESHOLD = 15.0

# Moneyline underdog American odds at/beyond this are flagged as lower-volume.
LOW_PROB_DOG_ODDS_THRESHOLD = 200.0

# Primary RLM line-movement source order (first complete source wins).
RLM_SOURCE_PRIORITY: tuple[str, ...] = ("eva", "thespread", "polymarket")

SOURCE_WEIGHTS: dict[str, float] = {
    "vsin": W_VSIN,
    "primary": W_PRIMARY,
    "sbd": W_SBD,
}
MLB_SOURCES = ("primary", "vsin", "sbd")
TWO_SOURCE_LEAGUES = frozenset({"WNBA", "UFC"})
WNBA_SOURCES = ("primary", "vsin")
SIDES = ("away", "home")
TOTAL_SIDES = ("over", "under")
ALL_SIDES = SIDES + TOTAL_SIDES
Side = Literal["away", "home", "over", "under"]
SourceName = Literal["primary", "vsin", "sbd"]
Tier = Literal["A+", "A", "B"]


def _league_key(league: str | None) -> str:
    key = str(league or "").strip().upper()
    if key == "CFB":
        return "NCAAF"
    return key


def sources_for_league(league: str | None) -> tuple[str, ...]:
    if _league_key(league) in TWO_SOURCE_LEAGUES:
        return WNBA_SOURCES
    return MLB_SOURCES


def primary_source_label(league: str | None) -> str:
    if _league_key(league) in {"WNBA", "UFC", "NCAAF"}:
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
        return float(value)
    except (TypeError, ValueError):
        return None


# --- Step 1: per-source gaps --------------------------------------------------


@dataclass(frozen=True)
class SourceGaps:
    """Raw handle − public gap for one side, per source. None = missing data."""

    primary: float | None
    vsin: float | None
    sbd: float | None

    def as_dict(self) -> dict[str, float | None]:
        return {"primary": self.primary, "vsin": self.vsin, "sbd": self.sbd}


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


def _try_sbd_override(
    away_gaps: SourceGaps,
    home_gaps: SourceGaps,
    sources: tuple[str, ...],
) -> Agreement | None:
    """Keep the game when VSiN+DK agree strongly and only SBD votes the other way.

    SBD missing/neutral is not handled here (existing two-source Tier B).
    VSiN vs DK disagreement still discards. Composite later drops SBD.
    """
    if not {"primary", "vsin", "sbd"}.issubset(sources):
        return None
    primary_vote = _source_favors(away_gaps, home_gaps, "primary")
    vsin_vote = _source_favors(away_gaps, home_gaps, "vsin")
    sbd_vote = _source_favors(away_gaps, home_gaps, "sbd")
    if primary_vote is None or vsin_vote is None or sbd_vote is None:
        return None
    if primary_vote != vsin_vote or sbd_vote == primary_vote:
        return None
    side_gaps = away_gaps if primary_vote == "away" else home_gaps
    gaps = side_gaps.as_dict()
    primary_gap = gaps.get("primary")
    vsin_gap = gaps.get("vsin")
    if primary_gap is None or vsin_gap is None:
        return None
    if primary_gap < STRONG_SOURCE_GAP_THRESHOLD or vsin_gap < STRONG_SOURCE_GAP_THRESHOLD:
        return None
    dissent_gaps = home_gaps if sbd_vote == "home" else away_gaps
    return Agreement(
        side=primary_vote,
        agreeing_sources=("primary", "vsin"),
        conflict=True,
        sbd_override=True,
        sbd_dissent_gap=dissent_gaps.sbd,
    )


def check_agreement(
    away_gaps: SourceGaps,
    home_gaps: SourceGaps,
    sources: tuple[str, ...] = MLB_SOURCES,
) -> Agreement:
    """Count sources with a positive gap on each side.

    Discards the game (side=None) when:
      - no side has at least 2 of the active sources, or
      - sources vote opposite directions (if REQUIRE_UNANIMOUS_DIRECTION),
        unless the SBD-override path applies (VSiN+DK strong, SBD alone
        dissenting).
    """
    votes: dict[Side, list[str]] = {"away": [], "home": []}
    for source in sources:
        favored = _source_favors(away_gaps, home_gaps, source)
        if favored is not None:
            votes[favored].append(source)

    away_n = len(votes["away"])
    home_n = len(votes["home"])
    conflict = away_n > 0 and home_n > 0

    if conflict and REQUIRE_UNANIMOUS_DIRECTION:
        override = _try_sbd_override(away_gaps, home_gaps, sources)
        if override is not None:
            return override
        return Agreement(side=None, agreeing_sources=(), conflict=True)

    if away_n >= 2 and away_n >= home_n:
        return Agreement(side="away", agreeing_sources=tuple(votes["away"]), conflict=conflict)
    if home_n >= 2:
        return Agreement(side="home", agreeing_sources=tuple(votes["home"]), conflict=conflict)
    return Agreement(side=None, agreeing_sources=(), conflict=conflict)


# --- Step 3: composite gap ----------------------------------------------------


def compute_composite(side_gaps: SourceGaps, agreeing_sources: tuple[str, ...]) -> float:
    """Weighted composite gap using only sources that agree in direction.

    Missing gaps among the agreeing set are skipped (not treated as 0).
    """
    gaps = side_gaps.as_dict()
    total = 0.0
    for source in agreeing_sources:
        gap = gaps.get(source)
        if gap is None:
            continue
        total += SOURCE_WEIGHTS[source] * gap
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
    away_pub = _as_float(away.get("public_bet_pct"))
    home_pub = _as_float(home.get("public_bet_pct"))
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
        open_px, live_px = poly_open, poly_live
    elif eva_available:
        source_used = "eva"
        line_moved_toward = eva_move
        open_px, live_px = eva_open, eva_live
    elif poly_ready:
        source_used = "polymarket"
        line_moved_toward = poly_move
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


def _thespread_pair_complete(away: dict[str, Any], home: dict[str, Any], market: Market) -> bool:
    """True when TheSpread itself supplied an open→live (or juice) pair."""
    for side in (away, home):
        opened, live = _thespread_open_live(side)
        if opened is not None and live is not None:
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
    if market == "spread":
        moved = _spread_number_moved_toward(away, home, _thespread_open_live)
        if moved is None:
            moved = _juice_moved_toward(away, home)
    elif market == "total":
        moved = _total_number_moved_toward(away, home, _thespread_open_live)
        if moved is None:
            moved = _juice_moved_toward(away, home)
    else:
        moved = _american_odds_moved_toward(away, home, _thespread_open_live)
    sharp = away if sharp_side == "away" else home
    opened, live = _thespread_open_live(sharp)
    if opened is None and live is None and market != "moneyline":
        opened = _as_float(sharp.get("open_odds"))
        live = _as_float(sharp.get("live_odds"))
    return moved, opened, live


def _eva_move(
    away: dict[str, Any],
    home: dict[str, Any],
    market: Market,
    sharp_side: Side,
) -> tuple[Side | None, float | None, float | None]:
    if market == "spread":
        moved = _spread_number_moved_toward(away, home, _eva_open_live)
    elif market == "total":
        moved = _total_number_moved_toward(away, home, _eva_open_live)
    else:
        moved = _american_odds_moved_toward(away, home, _eva_open_live)
    sharp = away if sharp_side == "away" else home
    opened, live = _eva_open_live(sharp)
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

    Tier A = every active source agrees. Tier B = all-but-one, and at
    least 2 sources. WNBA has two handle/public sources (DK + VSiN), so
    both agreeing is Tier A; a single source never qualifies.
    The SBD-override path (VSiN+DK strong, SBD dissenting) is capped at B
    even if the two-source composite would clear the Tier A threshold.
    """
    if not rlm_confirmed:
        return None
    if sbd_override:
        if n_agreeing >= 2 and composite_gap >= TIER_B_THRESHOLD:
            return "B"
        return None
    if n_agreeing == n_sources and n_sources >= 2 and composite_gap >= TIER_A_THRESHOLD:
        return "A"
    if (
        n_agreeing == n_sources - 1
        and n_agreeing >= 2
        and composite_gap >= TIER_B_THRESHOLD
    ):
        return "B"
    return None


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
    """De-vig live American odds. Spread/total use juice; ML prefers VSIN then live."""
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
        vsin_away = _as_float(away.get("vsin_line"))
        vsin_home = _as_float(home.get("vsin_line"))
        if vsin_away is not None and vsin_home is not None:
            odds_away, odds_home = vsin_away, vsin_home
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
    for key in ("live", "vsin_line", "eva_line", "sbd_line"):
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
    tier: Tier,
    market: Market,
    sources: tuple[str, ...] = MLB_SOURCES,
) -> dict[str, Any]:
    """Assemble one qualifying play as a JSON-serializable dict."""
    logical: Side = "away" if side in {"away", "over"} else "home"
    gaps = (away_gaps if logical == "away" else home_gaps).as_dict()
    labeled = _label_side(logical, market) or side
    abbr = side_data.get("selection") or game.get(f"{labeled}_abbr") or game.get(f"{side}_abbr")
    fair = _fair_prob_for_side(away, home, logical, market)
    if rlm.open_px is not None or rlm.live_px is not None:
        open_px, live_px = rlm.open_px, rlm.live_px
    else:
        open_px, live_px = _open_live_number(side_data)
    row: dict[str, Any] = {
        "matchup": game.get("matchup"),
        "game_time_utc": game.get("game_time_utc"),
        "date": game.get("date"),
        "event_id": game.get("event_id"),
        "market": market,
        "side": abbr,
        "home_away": labeled,
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
            "public_favors": _label_side(rlm.public_favors, market),
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

    return play


def process_game(
    game: dict[str, Any],
    market: Market = "moneyline",
    sources: tuple[str, ...] = MLB_SOURCES,
) -> dict[str, Any] | None:
    """Run steps 1–5, then exchange enrichment. Return a play dict or None."""
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
    agreement = check_agreement(away_gaps, home_gaps, sources=sources)
    if agreement.side is None:
        return None

    side = _label_side(agreement.side, market) or agreement.side
    side_data = away if agreement.side == "away" else home
    side_gaps = away_gaps if agreement.side == "away" else home_gaps
    composite_gap = compute_composite(side_gaps, agreement.agreeing_sources)
    rlm = check_rlm(away, home, agreement.side, market=market)
    tier = assign_tier(
        agreement.n_agreeing,
        composite_gap,
        rlm.rlm_confirmed,
        n_sources=len(sources),
        sbd_override=agreement.sbd_override,
    )
    if tier is None:
        return None

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
    )
    return enrich_with_exchange_data(play, game)


def process_slate(
    payload: dict[str, Any],
    markets: tuple[Market, ...] = ("moneyline",),
    sources: tuple[str, ...] | None = None,
) -> pd.DataFrame:
    """Process every game and return plays sorted by tier (A+ / A / B), then gap desc."""
    active = sources or sources_for_league(payload.get("league"))
    plays: list[dict[str, Any]] = []
    for game in payload.get("games") or []:
        if not isinstance(game, dict):
            continue
        for market in markets:
            play = process_game(game, market=market, sources=active)
            if play is not None:
                plays.append(play)

    frame = pd.DataFrame(plays)
    if frame.empty:
        return frame
    ranked = frame.assign(_tier_rank=frame["tier"].map({"A+": 0, "A": 1, "B": 2}))
    ranked = ranked.sort_values(["_tier_rank", "composite_gap"], ascending=[True, False])
    return ranked.drop(columns=["_tier_rank"]).reset_index(drop=True)


def config_snapshot(
    markets: tuple[Market, ...],
    sources: tuple[str, ...],
    league: str | None,
) -> dict[str, Any]:
    cfg: dict[str, Any] = {
        "market": list(markets) if len(markets) > 1 else markets[0],
        "sources": list(sources),
        "primary_source": primary_source_label(league),
        "w_vsin": W_VSIN,
        "w_primary": W_PRIMARY,
    }
    if "sbd" in sources:
        cfg["w_sbd"] = W_SBD
    cfg["tier_a_threshold"] = TIER_A_THRESHOLD
    cfg["tier_b_threshold"] = TIER_B_THRESHOLD
    cfg["require_unanimous_direction"] = REQUIRE_UNANIMOUS_DIRECTION
    cfg["strong_source_gap_threshold"] = STRONG_SOURCE_GAP_THRESHOLD
    cfg["low_prob_dog_odds_threshold"] = LOW_PROB_DOG_ODDS_THRESHOLD
    cfg["rlm_source_priority"] = list(RLM_SOURCE_PRIORITY)
    cfg["exchange_rlm_min_pp"] = EXCHANGE_RLM_MIN_PP
    cfg["low_liquidity_threshold"] = LOW_LIQUIDITY_THRESHOLD
    cfg["tier_a_plus_edge_pct"] = TIER_A_PLUS_EDGE_PCT
    return cfg


def slate_to_json(
    payload: dict[str, Any],
    frame: pd.DataFrame,
    source_file: Path,
    markets: tuple[Market, ...],
) -> dict[str, Any]:
    plays = frame.to_dict(orient="records") if not frame.empty else []
    return {
        "source_file": str(source_file),
        "date": payload.get("date"),
        "league": payload.get("league", "MLB"),
        "generated_at": datetime.now(PAGE_TZ).astimezone().isoformat(),
        "game_count": payload.get("game_count") or len(payload.get("games") or []),
        "play_count": len(plays),
        "config": config_snapshot(markets, sources_for_league(payload.get("league")), payload.get("league")),
        "plays": plays,
    }


def print_summary(frame: pd.DataFrame) -> None:
    if frame.empty:
        print("No Tier A/B sharp-money plays.")
        return
    display_cols = [
        "tier",
        "market",
        "matchup",
        "side",
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


def main() -> None:
    parser = argparse.ArgumentParser(description="Find sharp-money plays from combined betting splits")
    parser.add_argument("--input", type=Path, default=DEFAULT_INPUT)
    parser.add_argument("--out", type=Path, default=DEFAULT_JSON_OUT)
    parser.add_argument("--csv", type=Path, default=DEFAULT_CSV_OUT)
    parser.add_argument(
        "--market",
        default="moneyline",
        choices=["moneyline", "spread", "total", "both", "all"],
        help="Market to evaluate (NCAAF should use 'all'; WNBA 'both' or 'spread')",
    )
    args = parser.parse_args()
    args.input = _resolve_cli_path(args.input)
    args.out = _resolve_cli_path(args.out)
    args.csv = _resolve_cli_path(args.csv)

    payload = json.loads(args.input.read_text(encoding="utf-8"))
    markets = _markets_from_arg(args.market)
    frame = process_slate(payload, markets=markets)
    output = slate_to_json(payload, frame, args.input, markets)

    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(output, indent=2) + "\n", encoding="utf-8")
    csv_frame = _frame_for_csv(frame)
    csv_frame.to_csv(args.csv, index=False)

    print_summary(frame)
    print(f"JSON → {args.out}")
    print(f"CSV  → {args.csv}")


if __name__ == "__main__":
    main()
