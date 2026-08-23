#!/usr/bin/env python3
"""
Identify daily "sharp money" plays from combined betting splits.

Input schema (per game.moneyline.away/home or game.spread.away/home) —
confirmed against mlb_betting_splits.json / wnba_betting_splits.json /
ufc_betting_splits.json:

  primary  public_bet_pct / handle_bet_pct
           (MLB: PlayerProps.ai; WNBA/UFC: DraftKings Network)
  sbd      sbd_public_bet_pct / sbd_handle_bet_pct   (MLB only: SportsBettingDime)
  vsin     vsin_public_bet_pct / vsin_handle_bet_pct
  prices   open / live  (ML: American odds; spread: the number)
  juice    open_odds / live_odds  (spread vig, used when the number is flat)
  vsin ML  vsin_line    (American odds, used for no-vig fair probability)

Moneyline is the default market. Spread open/live are point-spread numbers,
so RLM uses the number first and falls back to juice implied-prob movement.
WNBA/UFC weight DraftKings (primary) + VSiN only. TheSpread supplies RLM
(open → live): spread for WNBA, moneyline for UFC. SBD is not scraped for
WNBA/UFC. Covers is not scraped for UFC; Polymarket implied prob is used
as the exchange fair in that case.

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

Market = Literal["moneyline", "spread"]

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
REQUIRE_UNANIMOUS_DIRECTION = True

SOURCE_WEIGHTS: dict[str, float] = {
    "vsin": W_VSIN,
    "primary": W_PRIMARY,
    "sbd": W_SBD,
}
MLB_SOURCES = ("primary", "vsin", "sbd")
TWO_SOURCE_LEAGUES = frozenset({"WNBA", "UFC"})
WNBA_SOURCES = ("primary", "vsin")
SIDES = ("away", "home")
Side = Literal["away", "home"]
SourceName = Literal["primary", "vsin", "sbd"]
Tier = Literal["A+", "A", "B"]


def sources_for_league(league: str | None) -> tuple[str, ...]:
    if str(league or "").upper() in TWO_SOURCE_LEAGUES:
        return WNBA_SOURCES
    return MLB_SOURCES


def primary_source_label(league: str | None) -> str:
    if str(league or "").upper() in {"WNBA", "UFC"}:
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
) -> Agreement:
    """Count sources with a positive gap on each side.

    Discards the game (side=None) when:
      - no side has at least 2 of the active sources, or
      - sources vote opposite directions (if REQUIRE_UNANIMOUS_DIRECTION).
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

    Moneyline: American odds shortened (implied win probability up).
    Spread: the point-spread number first (more negative = toward that side);
    if the number is unchanged, juice (open_odds → live_odds) is used.
    """
    away_pub = _as_float(away.get("public_bet_pct"))
    home_pub = _as_float(home.get("public_bet_pct"))
    public_favors: Side | None = None
    if away_pub is not None and home_pub is not None:
        if away_pub > home_pub:
            public_favors = "away"
        elif home_pub > away_pub:
            public_favors = "home"

    if market == "spread":
        line_moved_toward = _spread_number_moved_toward(away, home)
        if line_moved_toward is None:
            line_moved_toward = _juice_moved_toward(away, home)
    else:
        line_moved_toward = _american_odds_moved_toward(away, home)

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
    )


def _spread_number_moved_toward(away: dict[str, Any], home: dict[str, Any]) -> Side | None:
    """Side whose spread decreased (became more negative / less plus)."""
    votes: list[Side] = []
    away_open = _as_float(away.get("open"))
    away_live = _as_float(away.get("live"))
    home_open = _as_float(home.get("open"))
    home_live = _as_float(home.get("live"))
    if away_open is not None and away_live is not None and away_live != away_open:
        votes.append("home" if away_live > away_open else "away")
    if home_open is not None and home_live is not None and home_live != home_open:
        votes.append("away" if home_live > home_open else "home")
    if not votes:
        return None
    if all(v == votes[0] for v in votes):
        return votes[0]
    return votes[0]


def _juice_moved_toward(away: dict[str, Any], home: dict[str, Any]) -> Side | None:
    return _implied_move_toward(
        away.get("open_odds"),
        away.get("live_odds"),
        home.get("open_odds"),
        home.get("live_odds"),
    )


def _american_odds_moved_toward(away: dict[str, Any], home: dict[str, Any]) -> Side | None:
    return _implied_move_toward(
        away.get("open"),
        away.get("live"),
        home.get("open"),
        home.get("live"),
    )


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
) -> Tier | None:
    """Return 'A' or 'B', or None if the play should not be output.

    Tier A = every active source agrees. Tier B = all-but-one, and at
    least 2 sources. WNBA has two handle/public sources (DK + VSiN), so
    both agreeing is Tier A; a single source never qualifies.
    """
    if not rlm_confirmed:
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


def _fair_prob_for_side(
    away: dict[str, Any], home: dict[str, Any], side: Side, market: Market = "moneyline"
) -> float | None:
    """De-vig live American odds. Spread uses juice; ML prefers VSIN then live."""
    if market == "spread":
        odds_away = _as_float(away.get("live_odds"))
        odds_home = _as_float(home.get("live_odds"))
        if odds_away is None or odds_home is None:
            odds_away = _as_float(away.get("open_odds"))
            odds_home = _as_float(home.get("open_odds"))
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
            if live_away is None or live_home is None:
                return None
            odds_away, odds_home = live_away, live_home
    try:
        p_away, p_home = no_vig_fair_probs(odds_away, odds_home)
    except ValueError:
        return None
    return p_away if side == "away" else p_home


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
    gaps = (away_gaps if side == "away" else home_gaps).as_dict()
    abbr = side_data.get("selection") or game.get(f"{side}_abbr")
    fair = _fair_prob_for_side(away, home, side, market)
    row: dict[str, Any] = {
        "matchup": game.get("matchup"),
        "game_time_utc": game.get("game_time_utc"),
        "date": game.get("date"),
        "event_id": game.get("event_id"),
        "market": market,
        "side": abbr,
        "home_away": side,
        "tier": tier,
        "composite_gap": round(composite_gap, 4),
        "primary_gap": gaps["primary"],
        "vsin_gap": gaps["vsin"],
    }
    if "sbd" in sources:
        row["sbd_gap"] = gaps["sbd"]
    row.update(
        {
            "n_sources_agreeing": agreement.n_agreeing,
            "agreeing_sources": list(agreement.agreeing_sources),
            "rlm_confirmed": rlm.rlm_confirmed,
            "public_favors": rlm.public_favors,
            "line_moved_toward": rlm.line_moved_toward,
            "open": side_data.get("open"),
            "live": side_data.get("live"),
            "open_odds": side_data.get("open_odds"),
            "live_odds": side_data.get("live_odds"),
            "implied_fair_prob": None if fair is None else round(fair, 6),
        }
    )
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


def _covers_book_fair_prob(book: dict[str, Any], side: Side) -> float | None:
    """De-vig one book's moneyline pair; return fair prob for `side`."""
    ml = book.get("moneyline")
    if not isinstance(ml, dict):
        return None
    away = ml.get("away") if isinstance(ml.get("away"), dict) else {}
    home = ml.get("home") if isinstance(ml.get("home"), dict) else {}
    odds_away = _as_float(away.get("line"))
    odds_home = _as_float(home.get("line"))
    if odds_away is None or odds_home is None:
        return None
    try:
        p_away, p_home = no_vig_fair_probs(odds_away, odds_home)
    except ValueError:
        return None
    return p_away if side == "away" else p_home


def _polymarket_block(game: dict[str, Any], home_away: Side) -> dict[str, Any] | None:
    """Embedded polymarket object under moneyline[away|home], not covers_odds."""
    ml = game.get("moneyline")
    if not isinstance(ml, dict):
        return None
    side_data = ml.get(home_away)
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
    side: Side | None = home_away if home_away in SIDES else None
    poly = _polymarket_block(game_data, side) if side is not None else None

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
            fair = _covers_book_fair_prob(book, side)
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
    away = block.get("away")
    home = block.get("home")
    if not isinstance(away, dict) or not isinstance(home, dict):
        return None

    away_gaps = compute_gaps(away)
    home_gaps = compute_gaps(home)
    agreement = check_agreement(away_gaps, home_gaps, sources=sources)
    if agreement.side is None:
        return None

    side = agreement.side
    side_data = away if side == "away" else home
    side_gaps = away_gaps if side == "away" else home_gaps
    composite_gap = compute_composite(side_gaps, agreement.agreeing_sources)
    rlm = check_rlm(away, home, side, market=market)
    tier = assign_tier(
        agreement.n_agreeing,
        composite_gap,
        rlm.rlm_confirmed,
        n_sources=len(sources),
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
    if frame.empty or "exchange_confirmation" not in frame.columns:
        return frame
    conf = pd.json_normalize(frame["exchange_confirmation"].tolist())
    if "books_used" in conf.columns:
        conf["books_used"] = conf["books_used"].apply(
            lambda v: ",".join(v) if isinstance(v, list) else v
        )
    return pd.concat(
        [frame.drop(columns=["exchange_confirmation"]).reset_index(drop=True), conf],
        axis=1,
    )


def _markets_from_arg(value: str) -> tuple[Market, ...]:
    if value == "both":
        return ("moneyline", "spread")
    if value in {"moneyline", "spread"}:
        return (value,)  # type: ignore[return-value]
    raise ValueError(f"unsupported market: {value}")


def main() -> None:
    parser = argparse.ArgumentParser(description="Find sharp-money plays from combined betting splits")
    parser.add_argument("--input", type=Path, default=DEFAULT_INPUT)
    parser.add_argument("--out", type=Path, default=DEFAULT_JSON_OUT)
    parser.add_argument("--csv", type=Path, default=DEFAULT_CSV_OUT)
    parser.add_argument(
        "--market",
        default="moneyline",
        choices=["moneyline", "spread", "both"],
        help="Market to evaluate (WNBA should use 'both' or 'spread')",
    )
    args = parser.parse_args()

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
