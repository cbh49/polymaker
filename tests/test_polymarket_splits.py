"""Polymarket snapshots merged into betting-splits sides."""

from __future__ import annotations

import sys
from pathlib import Path
from types import SimpleNamespace

_AGG = Path(__file__).resolve().parents[1] / "data-aggregation"
if str(_AGG) not in sys.path:
    sys.path.insert(0, str(_AGG))

from scrape_polymarket_odds import (  # noqa: E402
    build_poly_sides,
    history_from_clob_points,
    merge_polymarket_into_game,
    parse_pt_number,
    pick_spread_market,
    pick_total_market,
    share_to_american,
)


def test_share_to_american() -> None:
    assert share_to_american(0.5) == -100
    assert share_to_american(0.405) == 147
    assert share_to_american(0.595) == -147
    assert share_to_american(0.0) is None
    assert share_to_american(1.0) is None


def test_parse_pt_number() -> None:
    assert parse_pt_number("1pt5") == 1.5
    assert parse_pt_number("8pt5") == 8.5
    assert parse_pt_number("2") == 2.0


def test_pick_spread_matches_sportsbook_line() -> None:
    spreads = [
        {"favored": "home", "points": 1.5, "liquidity": 100.0, "raw": {"id": "a"}},
        {"favored": "home", "points": 2.5, "liquidity": 500.0, "raw": {"id": "b"}},
        {"favored": "away", "points": 1.5, "liquidity": 50.0, "raw": {"id": "c"}},
    ]
    picked = pick_spread_market(spreads, {"home": {"live": -1.5}, "away": {"live": 1.5}})
    assert picked is not None and picked["raw"]["id"] == "a"


def test_pick_spread_skips_zero_liquidity() -> None:
    spreads = [
        {"favored": "home", "points": 1.5, "liquidity": 0.0, "raw": {"id": "a"}},
        {"favored": "home", "points": 2.5, "liquidity": 80.0, "raw": {"id": "b"}},
    ]
    picked = pick_spread_market(spreads, {"home": {"live": -1.5}})
    assert picked is not None and picked["raw"]["id"] == "b"


def test_pick_total_matches_line() -> None:
    totals = [
        {"points": 7.5, "liquidity": 200.0, "raw": {"id": "a"}},
        {"points": 8.5, "liquidity": 50.0, "raw": {"id": "b"}},
    ]
    picked = pick_total_market(totals, {"over": {"live": 8.5}})
    assert picked is not None and picked["raw"]["id"] == "b"


def test_history_from_clob_points_collapses_unchanged_prices() -> None:
    points = [
        {"t": 1787068800, "p": 0.425},
        {"t": 1787072400, "p": 0.425},
        {"t": 1787076000, "p": 0.41},
        {"t": 1787079600, "p": 0.405},
        {"t": 1787083200, "p": 0.405},
    ]
    hist = history_from_clob_points(points)
    assert [row["implied_prob_pct"] for row in hist] == [42.5, 41.0, 40.5]
    assert hist[0]["line"] == share_to_american(0.425)
    assert "ts" in hist[0]


def test_merge_copies_clob_history() -> None:
    game = {"moneyline": {"away": {"selection": "DET"}, "home": {"selection": "PIT"}}}
    poly = {
        "moneyline": {
            "away": {
                "line": 147,
                "implied_prob_pct": 40.5,
                "liquidity": 100.0,
                "volume_24hr": 10.0,
                "last_updated": "2026-08-19T10:00:00-04:00",
                "market_id": "1",
                "history": [
                    {"ts": "2026-08-18T12:00:00-04:00", "line": 135, "implied_prob_pct": 42.5},
                    {"ts": "2026-08-19T10:00:00-04:00", "line": 147, "implied_prob_pct": 40.5},
                ],
            }
        }
    }
    merge_polymarket_into_game(game, poly)
    away = game["moneyline"]["away"]["polymarket"]
    assert away["line"] == 147
    assert len(away["history"]) == 2
    assert away["history"][0]["line"] == 135
    assert "polymarket" not in game["moneyline"]["home"]


def test_build_poly_sides_omits_zero_liquidity() -> None:
    event = {
        "slug": "mlb-det-pit-2026-08-19",
        "markets": [
            {
                "slug": "mlb-det-pit-2026-08-19",
                "id": 1,
                "closed": False,
                "outcomes": '["Detroit Tigers", "Pittsburgh Pirates"]',
                "outcomePrices": '["0.405", "0.595"]',
                "liquidityNum": 0,
                "volume24hr": 100,
            }
        ],
    }
    dest = {
        "away": "Detroit Tigers",
        "home": "Pittsburgh Pirates",
        "away_abbr": "DET",
        "home_abbr": "PIT",
    }
    away = SimpleNamespace(full_name="Detroit Tigers", poly_code="det")
    home = SimpleNamespace(full_name="Pittsburgh Pirates", poly_code="pit")
    sides = build_poly_sides(event, dest, away_ref=away, home_ref=home, ts="2026-08-19T10:00:00-04:00")
    assert sides["moneyline"] == {}
