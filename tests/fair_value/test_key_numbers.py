"""Key-number spread translation: 3 is worth more than a non-key half-point."""

from __future__ import annotations

import pytest

from ev_trading.fair_value.config import FairValueConfig
from ev_trading.fair_value.consensus import same_strike_consensus
from ev_trading.fair_value.key_numbers import translate_cover
from ev_trading.fair_value.models import BookPoint
from ev_trading.fair_value.pipeline import process_slate


def test_crossing_three_moves_more_than_a_non_key_half_point() -> None:
    through_3 = translate_cover(0.50, -3.5, -2.5) - 0.50
    through_8 = translate_cover(0.50, -8.5, -7.5) - 0.50
    assert through_3 > through_8
    assert through_3 > 0


def test_integer_push_is_below_the_no_push_cover() -> None:
    strict = translate_cover(0.52, -3.0, -3.5)
    assert strict < 0.52


def _spread_game(
    *, book_line: float, venue_line: float, home_p: float = 0.52, away_p: float = 0.52
) -> dict:
    return {
        "games": [
            {
                "matchup": "NYG @ DAL",
                "kickoff": "2026-12-06T18:00:00+00:00",
                "books_quoted_at": "2026-12-06T16:00:00+00:00",
                "kalshi_quoted_at": "2026-12-06T16:00:30+00:00",
                "polymarket_quoted_at": "2026-12-06T16:00:30+00:00",
                "markets": {
                    "spread": {
                        "consensus_line": book_line,
                        "books": {
                            "draftkings": {
                                "away": {
                                    "line": -book_line,
                                    "odds": away_p,
                                    "implied_prob": away_p,
                                },
                                "home": {"line": book_line, "odds": home_p, "implied_prob": home_p},
                            },
                            "fanduel": {
                                "away": {
                                    "line": -book_line,
                                    "odds": away_p,
                                    "implied_prob": away_p,
                                },
                                "home": {"line": book_line, "odds": home_p, "implied_prob": home_p},
                            },
                            "caesars": {
                                "away": {
                                    "line": -book_line,
                                    "odds": away_p,
                                    "implied_prob": away_p,
                                },
                                "home": {"line": book_line, "odds": home_p, "implied_prob": home_p},
                            },
                            "betr": {
                                "away": {
                                    "line": -book_line,
                                    "odds": away_p,
                                    "implied_prob": away_p,
                                },
                                "home": {"line": book_line, "odds": home_p, "implied_prob": home_p},
                            },
                        },
                        "kalshi": {
                            "ticker": "KXNFLSPREAD-TEST",
                            "yes_bid": 0.40,
                            "yes_ask": 0.41,
                            "no_ask": 0.60,
                            "volume": 5000,
                            "line": venue_line,
                            "line_delta": venue_line - book_line,
                        },
                        "polymarket": {
                            "home_line": venue_line,
                            "line_delta": venue_line - book_line,
                            "home": {
                                "ask": 0.40,
                                "bid": 0.39,
                                "liquidity": 5000,
                                "market_id": "sp",
                            },
                            "away": {
                                "ask": 0.61,
                                "bid": 0.60,
                                "liquidity": 5000,
                                "market_id": "sp",
                            },
                        },
                    }
                },
                "player_props": [],
            }
        ]
    }


def test_same_strike_spread_stays_the_weighted_average() -> None:
    cfg = FairValueConfig(min_edge_pct=0.0)
    report = process_slate(_spread_game(book_line=-3.5, venue_line=-3.5), cfg)
    rows = [
        r
        for r in report.tradable + report.tradable_low_liquidity
        if r.venue == "kalshi" and r.side == "home"
    ]
    assert rows
    row = rows[0]
    assert row.sigma_source is None
    assert row.market_line == pytest.approx(-3.5)
    points = [
        BookPoint("draftkings", -3.5, 0.5, 1.0),
        BookPoint("fanduel", -3.5, 0.5, 1.0),
        BookPoint("caesars", -3.5, 0.5, 1.0),
        BookPoint("betr", -3.5, 0.5, 1.0),
    ]
    # -110-style 0.52/0.52 de-vigs to 0.5. The row fair is that consensus.
    assert row.fair_prob == pytest.approx(same_strike_consensus(points, cfg=cfg).fair_prob)


def test_wide_spread_gap_is_rejected() -> None:
    cfg = FairValueConfig(min_edge_pct=0.0)
    report = process_slate(_spread_game(book_line=-3.5, venue_line=-7.5), cfg)
    kept = report.tradable + report.tradable_low_liquidity
    assert all(r.stat != "spread" or r.veto_reason for r in kept)
    assert kept == [] or all(r.stat != "spread" for r in kept)
    rejected = [r for r in report.rejected if r.stat == "spread"]
    assert rejected
    assert all(r.veto_reason == "spread_span" for r in rejected)


def test_polymarket_spread_uses_its_own_home_line() -> None:
    cfg = FairValueConfig(min_edge_pct=0.0)
    report = process_slate(_spread_game(book_line=-3.5, venue_line=-2.5), cfg)
    rows = [
        r
        for r in report.tradable + report.tradable_low_liquidity
        if r.venue == "polymarket" and r.side == "home"
    ]
    assert rows
    assert rows[0].market_line == pytest.approx(-2.5)
    assert rows[0].sigma_source == "key_numbers"
    assert rows[0].fair_prob > 0.5
