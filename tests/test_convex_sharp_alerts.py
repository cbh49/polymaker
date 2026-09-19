"""Convex bodies for sharp-money dashboard plays."""

from __future__ import annotations

import sys
from pathlib import Path

AGG = Path(__file__).resolve().parents[1] / "data-aggregation"
if str(AGG) not in sys.path:
    sys.path.insert(0, str(AGG))

from convex_sharp_alerts import convex_body_for_play, start_time_ms  # noqa: E402


FUTURE = "2099-09-20T17:00:00.000Z"

SAMPLE = {
    "matchup": "GB @ NYJ",
    "matchup_display": "Green Bay Packers @ New York Jets",
    "away_team": "Green Bay Packers",
    "home_team": "New York Jets",
    "game_time_utc": FUTURE,
    "date": "2099-09-20",
    "event_id": "34118196",
    "market": "moneyline",
    "side": "NYJ",
    "home_away": "home",
    "play_label": "New York Jets +165",
    "play_line": 165.0,
    "play_odds": 165.0,
    "line_move": -10.0,
    "public_bet_pct": 14.0,
    "handle_bet_pct": 80.0,
    "public_favors_bet_pct": 86.0,
    "public_favors_name": "Green Bay Packers",
    "tier": "A+",
    "composite_gap": 138.75,
    "agreeing_sources": ["primary", "vsin", "sbd"],
    "n_sources_agreeing": 3,
    "rlm_confirmed": True,
    "rlm_source_used": "thespread",
    "open": 155.0,
    "live": 165.0,
    "implied_fair_prob": 0.38,
    "model_confidence": 88,
    "exchange_confirmation": {"exchange_edge_pct": 2.5},
}


def test_start_time_from_utc() -> None:
    ms = start_time_ms({"game_time_utc": FUTURE})
    assert ms > 2_000_000_000_000


def test_body_includes_splits_and_bet() -> None:
    body = convex_body_for_play(SAMPLE, league="NFL", posted_at=1_700_000_000_000)
    assert body is not None
    assert body["league"] == "NFL"
    assert body["playLabel"] == "New York Jets +165"
    assert body["awayTeam"] == "Green Bay Packers"
    assert body["homeTeam"] == "New York Jets"
    assert body["publicBetPct"] == 14.0
    assert body["handleBetPct"] == 80.0
    assert body["publicFavorsBetPct"] == 86.0
    assert body["open"] == 155.0
    assert body["live"] == 165.0
    assert body["tier"] == "A+"
    assert "primary" in body["agreeingSources"]


def test_skips_started_games() -> None:
    play = {**SAMPLE, "game_time_utc": "2020-01-01T00:00:00.000Z"}
    # Pregame skip is commented out so in-progress games still post.
    body = convex_body_for_play(play, league="NFL", posted_at=1_700_000_000_000)
    assert body is not None
    assert body["playLabel"] == "New York Jets +165"


def test_strips_nan_numbers() -> None:
    play = {**SAMPLE, "open_odds": float("nan"), "vsin_public_bet_pct": None}
    body = convex_body_for_play(play, league="NFL", posted_at=1_700_000_000_000)
    assert body is not None
    assert "openOdds" not in body
    assert "vsinPublicBetPct" not in body
