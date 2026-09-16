"""Polymarket snapshots merged into betting-splits sides."""

from __future__ import annotations

import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

_AGG = Path(__file__).resolve().parents[1] / "data-aggregation"
if str(_AGG) not in sys.path:
    sys.path.insert(0, str(_AGG))

from scrape_polymarket_odds import (  # noqa: E402
    _EVENT_SLUG_RE,
    _SPREAD_SLUG_RE,
    _TOTAL_SLUG_RE,
    _cfb_side_match,
    _classify_markets,
    build_player_props,
    build_poly_sides,
    history_from_clob_points,
    merge_polymarket_into_game,
    normalize_league,
    parse_pt_number,
    pick_spread_market,
    pick_total_market,
    poly_event_prefix,
    poly_series_slugs,
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


def test_build_poly_sides_records_spread_points_and_alts() -> None:
    event = {
        "slug": "nfl-atl-pit-2026-09-13",
        "markets": [
            {
                "slug": "nfl-atl-pit-2026-09-13",
                "id": 1,
                "closed": False,
                "outcomes": '["Atlanta Falcons", "Pittsburgh Steelers"]',
                "outcomePrices": '["0.30", "0.70"]',
                "bestBid": 0.28,
                "bestAsk": 0.32,
                "liquidityNum": 100,
            },
            {
                "slug": "nfl-atl-pit-2026-09-13-spread-home-3pt5",
                "id": 2,
                "closed": False,
                "outcomes": '["Atlanta Falcons", "Pittsburgh Steelers"]',
                "outcomePrices": '["0.45", "0.55"]',
                "liquidityNum": 80,
            },
            {
                "slug": "nfl-atl-pit-2026-09-13-spread-home-7pt5",
                "id": 3,
                "closed": False,
                "outcomes": '["Atlanta Falcons", "Pittsburgh Steelers"]',
                "outcomePrices": '["0.25", "0.75"]',
                "liquidityNum": 20,
            },
            {
                "slug": "nfl-atl-pit-2026-09-13-total-44pt5",
                "id": 4,
                "closed": False,
                "outcomes": '["Over", "Under"]',
                "outcomePrices": '["0.50", "0.50"]',
                "liquidityNum": 50,
            },
        ],
    }
    dest = {
        "away": "Atlanta Falcons",
        "home": "Pittsburgh Steelers",
        "away_abbr": "ATL",
        "home_abbr": "PIT",
    }
    away = SimpleNamespace(full_name="Atlanta Falcons", poly_code="atl")
    home = SimpleNamespace(full_name="Pittsburgh Steelers", poly_code="pit")
    sides = build_poly_sides(event, dest, away_ref=away, home_ref=home, ts="2026-09-12T10:00:00-04:00")
    assert sides["moneyline"]["away"]["bid"] == 0.28
    assert sides["moneyline"]["away"]["ask"] == 0.32
    assert sides["moneyline"]["home"]["bid"] == pytest.approx(0.68)
    assert sides["moneyline"]["home"]["ask"] == pytest.approx(0.72)
    assert sides["spread"]["home_line"] == -3.5
    assert sides["spread"]["points"] == 3.5
    assert sides["total"]["points"] == 44.5
    assert [row["home_line"] for row in sides["spread_alts"]] == [-7.5, -3.5]
    assert sides["total_alts"][0]["points"] == 44.5
    assert "history" not in sides["spread_alts"][0]["home"]


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


def test_normalize_league_cfb_is_ncaaf() -> None:
    assert normalize_league("CFB") == "NCAAF"
    assert normalize_league("ncaaf") == "NCAAF"
    assert poly_event_prefix("NCAAF") == "cfb"
    assert poly_event_prefix("CFB") == "cfb"


def test_poly_series_slugs_cfb_is_year_tagged() -> None:
    from datetime import date

    slugs = poly_series_slugs("NCAAF", date(2026, 8, 29))
    assert slugs[0] == "cfb-2026"
    assert "cfb" in slugs
    assert poly_series_slugs("MLB") == ("mlb",)
    nfl = poly_series_slugs("NFL", date(2026, 9, 13))
    assert nfl[0] == "nfl-2026"
    assert "nfl" in nfl


def test_cfb_slug_regexes() -> None:
    m = _EVENT_SLUG_RE.match("cfb-hawaii-stan-2026-08-29")
    assert m is not None
    assert m.group("league") == "cfb"
    assert m.group("away") == "hawaii"
    assert m.group("home") == "stan"
    spread = _SPREAD_SLUG_RE.match("cfb-hawaii-stan-2026-08-29-spread-home-5pt5")
    assert spread is not None and parse_pt_number(spread.group("pts")) == 5.5
    total = _TOTAL_SLUG_RE.match("cfb-hawaii-stan-2026-08-29-total-49pt5")
    assert total is not None and parse_pt_number(total.group("pts")) == 49.5
    assert _EVENT_SLUG_RE.match("cfb-hawaii-stan-2026-08-29-spread-home-5pt5") is None


def test_classify_cfb_markets() -> None:
    event = {
        "slug": "cfb-hawaii-stan-2026-08-29",
        "markets": [
            {"slug": "cfb-hawaii-stan-2026-08-29", "closed": False, "liquidityNum": 10},
            {
                "slug": "cfb-hawaii-stan-2026-08-29-spread-home-5pt5",
                "closed": False,
                "liquidityNum": 8,
            },
            {
                "slug": "cfb-hawaii-stan-2026-08-29-total-49pt5",
                "closed": False,
                "liquidityNum": 7,
            },
            {
                "slug": "cfb-hawaii-stan-2026-08-29-1h-moneyline",
                "closed": False,
                "liquidityNum": 3,
            },
        ],
    }
    classified = _classify_markets(event)
    assert classified["moneyline"]["slug"] == "cfb-hawaii-stan-2026-08-29"
    assert classified["spreads"][0]["points"] == 5.5
    assert classified["totals"][0]["points"] == 49.5


def test_cfb_side_match_names_and_slug_codes() -> None:
    labels = ["Hawaii", "Stanford"]
    assert _cfb_side_match("Hawaii", labels, "hawaii")
    assert _cfb_side_match("STAN", labels, "stan")
    assert _cfb_side_match("Stanford", labels, "stan")
    assert not _cfb_side_match("Alabama", ["Georgia", "Auburn"], "uga")


def test_build_player_props_yardage_over_under() -> None:
    event = {
        "slug": "nfl-atl-pit-2026-09-13-player-props",
        "markets": [
            {
                "slug": "nfl-atl-pit-2026-09-13-pyd-aaron-rodgers-149pt5",
                "sportsMarketType": "passing_yards",
                "line": 149.5,
                "groupItemTitle": "Aaron Rodgers: Passing Yards O/U 149.5",
                "closed": False,
                "id": 4377001,
                "outcomes": '["Over", "Under"]',
                "outcomePrices": '["0.435", "0.565"]',
                "liquidityNum": 12.5,
                "volume24hrClob": 88.0,
            },
            {
                "slug": "nfl-atl-pit-2026-09-13-rec-drake-london-3pt5",
                "sportsMarketType": "receptions",
                "line": 3.5,
                "closed": False,
                "outcomes": '["Over", "Under"]',
                "outcomePrices": '["0.5", "0.5"]',
                "liquidityNum": 9.0,
            },
        ],
    }
    props = build_player_props(event)
    assert len(props) == 1
    rodgers = props[0]
    assert rodgers["type"] == "passing_yards"
    assert rodgers["player"] == "Aaron Rodgers"
    assert rodgers["line"] == 149.5
    assert rodgers["over"] == 0.435
    assert rodgers["under"] == 0.565
    assert rodgers["market_id"] == "4377001"
    assert "over_ask" not in rodgers


def test_build_player_props_uses_gamma_book_not_mid() -> None:
    event = {
        "slug": "nfl-buf-hou-2026-09-13-player-props",
        "markets": [
            {
                "slug": "nfl-buf-hou-2026-09-13-pyd-josh-allen-224pt5",
                "sportsMarketType": "passing_yards",
                "line": 224.5,
                "groupItemTitle": "Josh Allen: Passing Yards O/U 224.5",
                "closed": False,
                "id": 4196015,
                "outcomes": '["Over", "Under"]',
                "outcomePrices": '["0.26", "0.74"]',
                "bestBid": 0.02,
                "bestAsk": 0.5,
                "liquidityNum": 269.61,
            }
        ],
    }
    props = build_player_props(event)
    assert len(props) == 1
    allen = props[0]
    assert allen["over"] == 0.26
    assert allen["under"] == 0.74
    assert allen["over_bid"] == 0.02
    assert allen["over_ask"] == 0.5
    assert allen["under_bid"] == 0.5
    assert allen["under_ask"] == pytest.approx(0.98)
