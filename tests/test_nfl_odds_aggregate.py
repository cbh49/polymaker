"""NFL cross-venue odds matching and aggregation."""

from __future__ import annotations

import sys
from pathlib import Path

from ev_trading.nfl_odds import (
    ROTOWIRE_PROP_TO_CANONICAL,
    canonical_abbr,
    game_key,
    game_start_ms,
    kalshi_spread_contract,
    kalshi_spread_home_line,
    kickoff_ms,
    match_team_side,
    median_line,
    normalize_player_name,
    parse_kalshi_spread,
    parse_kalshi_sub_title,
    parse_kalshi_total,
    pick_closest,
    player_initial_key,
)
from ev_trading.venues.kalshi.kalshi_nfl import (
    parse_spread_subtitle,
    parse_total_subtitle,
    ticker_team_abbr,
)

_SUPPORT = Path(__file__).resolve().parents[1] / "src" / "ev_trading" / "supporting-lines"
if str(_SUPPORT) not in sys.path:
    sys.path.insert(0, str(_SUPPORT))

from aggregate_nfl_odds import aggregate_nfl_slate  # noqa: E402


def test_kickoff_ms_naive_eastern() -> None:
    from datetime import datetime
    from zoneinfo import ZoneInfo

    ms = kickoff_ms("2026-09-20 13:00:00")
    expected = datetime(2026, 9, 20, 13, 0, tzinfo=ZoneInfo("America/New_York"))
    assert ms == int(expected.timestamp() * 1000)
    assert game_start_ms({"gameDate": "2026-09-20 13:00:00"}) == ms
    assert game_start_ms({"start_time_ms": ms}) == ms
    assert kickoff_ms(None) is None


def test_canonical_abbr_jax_was_rams() -> None:
    assert canonical_abbr("JAC") == "JAX"
    assert canonical_abbr("JAX") == "JAX"
    assert canonical_abbr("WSH") == "WAS"
    assert canonical_abbr("LAR") == "LA"


def test_game_key_joins_jac_and_jax() -> None:
    assert game_key("CLE", "JAC") == ("CLE", "JAX")
    assert game_key("CLE", "JAX") == ("CLE", "JAX")
    parsed = parse_kalshi_sub_title("CLE vs JAC (Sep 13)")
    assert parsed == ("CLE", "JAC")
    assert game_key(*parsed) == ("CLE", "JAX")


def test_player_name_strips_suffixes() -> None:
    assert normalize_player_name("Brian Thomas Jr.") == "brian thomas"
    assert normalize_player_name("Brian Thomas") == "brian thomas"
    assert normalize_player_name("Kenneth Walker III") == "kenneth walker"
    assert player_initial_key("Brian Thomas Jr.") == ("thomas", "b")


def test_market_type_map() -> None:
    assert ROTOWIRE_PROP_TO_CANONICAL["passyds"] == "passing_yards"
    assert ROTOWIRE_PROP_TO_CANONICAL["recyds"] == "receiving_yards"
    assert ROTOWIRE_PROP_TO_CANONICAL["rushyds"] == "rushing_yards"
    assert ROTOWIRE_PROP_TO_CANONICAL["rushrec"] == "rushing_receiving_yards"
    assert ROTOWIRE_PROP_TO_CANONICAL["anytd"] == "anytime_td"
    assert ROTOWIRE_PROP_TO_CANONICAL["firsttd"] == "first_td"
    assert ROTOWIRE_PROP_TO_CANONICAL["twotd"] == "2plus_td"


def test_pick_closest_rodgers_main_line() -> None:
    alts = [{"line": 199.5}, {"line": 224.5}, {"line": 249.5}]
    picked = pick_closest(alts, 212.5)
    assert picked is not None and picked["line"] == 224.5


def test_kalshi_spread_parse_and_home_line() -> None:
    team, line = parse_kalshi_spread("Pittsburgh wins by over 3.5 points")
    assert team == "Pittsburgh"
    assert line == 3.5
    assert parse_spread_subtitle("Pittsburgh wins by over 3.5 points") == ("Pittsburgh", 3.5)
    assert parse_total_subtitle("Over 44.5 points scored") == 44.5
    assert parse_kalshi_total("Over 44.5 points scored") == 44.5
    assert ticker_team_abbr("KXNFLSPREAD-26SEP13ATLPIT-PIT4") == "PIT"
    assert ticker_team_abbr("KXNFLTOTAL-26SEP13ATLPIT-21") is None

    home_line = kalshi_spread_home_line(
        {
            "yes_sub_title": "Pittsburgh wins by over 3.5 points",
            "line": 3.5,
            "team": "Pittsburgh",
            "team_abbr": "PIT",
            "ticker": "KXNFLSPREAD-26SEP13ATLPIT-PIT4",
        },
        away_abbr="ATL",
        home_abbr="PIT",
        away_name="Atlanta Falcons",
        home_name="Pittsburgh Steelers",
    )
    assert home_line == -3.5

    away_fav = kalshi_spread_home_line(
        {
            "yes_sub_title": "Atlanta wins by over 2.5 points",
            "line": 2.5,
            "team_abbr": "ATL",
        },
        away_abbr="ATL",
        home_abbr="PIT",
        away_name="Atlanta Falcons",
        home_name="Pittsburgh Steelers",
    )
    assert away_fav == 2.5
    home_line, yes_side = kalshi_spread_contract(
        {
            "yes_sub_title": "Carolina wins by over 3.5 points",
            "line": 3.5,
            "team_abbr": "CAR",
            "ticker": "KXNFLSPREAD-26SEP22CARCLE-CAR4",
        },
        away_abbr="CAR",
        home_abbr="CLE",
        away_name="Carolina Panthers",
        home_name="Cleveland Browns",
    )
    assert home_line == 3.5
    assert yes_side == "away"


def test_match_team_side_short_kalshi_names() -> None:
    assert (
        match_team_side(
            "New York G",
            away_abbr="DAL",
            home_abbr="NYG",
            away_name="Dallas Cowboys",
            home_name="New York Giants",
        )
        == "home"
    )
    assert (
        match_team_side(
            "Dallas",
            away_abbr="DAL",
            home_abbr="NYG",
            away_name="Dallas Cowboys",
            home_name="New York Giants",
            team_abbr="DAL",
        )
        == "away"
    )


def test_median_line() -> None:
    assert median_line([214.5, 210.5, 212.5]) == 212.5
    assert median_line([210.5, 212.5]) == 211.5


def test_aggregate_joins_jac_jax_and_closest_prop_line() -> None:
    game_odds = {
        "source": "rotowire-games",
        "periods": [
            {
                "period": "game",
                "games": [
                    {
                        "gameID": "1",
                        "gameDate": "2026-09-13 13:00:00",
                        "away": {"team": "Cleveland Browns", "abbr": "CLE", "nickname": "Browns"},
                        "home": {"team": "Jacksonville Jaguars", "abbr": "JAC", "nickname": "Jaguars"},
                        "books": {
                            "draftkings": {
                                "spread": {
                                    "away": {"line": 3.5, "odds": -110},
                                    "home": {"line": -3.5, "odds": -110},
                                },
                                "moneyline": {"away": 150, "home": -180},
                                "overUnder": {"line": 44.5, "overOdds": -110, "underOdds": -110},
                            }
                        },
                    }
                ],
            }
        ],
    }
    player_props = {
        "source": "rotowire-props",
        "markets": [
            {
                "prop": "passyds",
                "players": [
                    {
                        "name": "Brian Thomas",
                        "playerID": "99",
                        "gameID": "1",
                        "team": "Jacksonville Jaguars",
                        "books": {
                            "draftkings": {"line": 212.5, "overOdds": -112, "underOdds": -112},
                            "fanduel": {"line": 212.5, "overOdds": -110, "underOdds": -110},
                        },
                    }
                ],
            }
        ],
    }
    kalshi = {
        "generated_at": "2026-09-12T00:00:00+00:00",
        "games": [
            {
                "event_ticker": "KXNFLGAME-26SEP13CLEJAC",
                "sub_title": "CLE vs JAC (Sep 13)",
                "game_lines": [
                    {
                        "type": "moneyline",
                        "ticker": "KXNFLGAME-26SEP13CLEJAC-JAC",
                        "yes_sub_title": "Jacksonville",
                        "yes_bid": 0.64,
                        "yes_ask": 0.65,
                    },
                    {
                        "type": "spread",
                        "ticker": "KXNFLSPREAD-26SEP13CLEJAC-JAC4",
                        "yes_sub_title": "Jacksonville wins by over 3.5 points",
                        "yes_bid": 0.50,
                        "yes_ask": 0.51,
                        "line": 3.5,
                        "team": "Jacksonville",
                        "team_abbr": "JAC",
                    },
                    {
                        "type": "total",
                        "ticker": "KXNFLTOTAL-26SEP13CLEJAC-45",
                        "yes_sub_title": "Over 44.5 points scored",
                        "yes_bid": 0.49,
                        "yes_ask": 0.51,
                        "line": 44.5,
                    },
                ],
                "player_props": [
                    {
                        "type": "passing_yards",
                        "player": "Brian Thomas Jr.",
                        "line": 199.5,
                        "yes_bid": 0.56,
                        "yes_ask": 0.60,
                        "ticker": "K1",
                    },
                    {
                        "type": "passing_yards",
                        "player": "Brian Thomas Jr.",
                        "line": 224.5,
                        "yes_bid": 0.41,
                        "yes_ask": 0.43,
                        "ticker": "K2",
                    },
                ],
            },
            {
                "event_ticker": "KXNFLGAME-26SEP20DALNYG",
                "sub_title": "DAL vs NYG (Sep 20)",
                "game_lines": [],
                "player_props": [],
            },
        ],
    }
    polymarket = {
        "source": "gamma",
        "games": [
            {
                "matchup": "CLE @ JAX",
                "away_abbr": "CLE",
                "home_abbr": "JAX",
                "away": "Cleveland Browns",
                "home": "Jacksonville Jaguars",
                "date": "2026-09-13",
                "moneyline": {
                    "away": {"line": 150, "implied_prob_pct": 40.0, "market_id": "ml-a"},
                    "home": {"line": -150, "implied_prob_pct": 60.0, "market_id": "ml-h"},
                },
                "spread_alts": [
                    {
                        "home_line": -3.5,
                        "points": 3.5,
                        "away": {"line": -110, "implied_prob_pct": 52.4, "market_id": "s1"},
                        "home": {"line": -110, "implied_prob_pct": 47.6, "market_id": "s1"},
                        "market_id": "s1",
                    },
                    {
                        "home_line": -7.5,
                        "points": 7.5,
                        "away": {"line": -200, "implied_prob_pct": 66.7, "market_id": "s2"},
                        "home": {"line": 150, "implied_prob_pct": 40.0, "market_id": "s2"},
                        "market_id": "s2",
                    },
                ],
                "total_alts": [
                    {
                        "points": 44.5,
                        "over": {"line": -110, "implied_prob_pct": 52.4, "market_id": "t1"},
                        "under": {"line": -110, "implied_prob_pct": 47.6, "market_id": "t1"},
                        "market_id": "t1",
                    },
                    {
                        "points": 50.5,
                        "over": {"line": 150, "implied_prob_pct": 40.0, "market_id": "t2"},
                        "under": {"line": -200, "implied_prob_pct": 66.7, "market_id": "t2"},
                        "market_id": "t2",
                    },
                ],
                "player_props": [
                    {
                        "type": "passing_yards",
                        "player": "Brian Thomas Jr.",
                        "line": 209.5,
                        "over": 0.51,
                        "under": 0.49,
                        "over_bid": 0.02,
                        "over_ask": 0.50,
                        "under_bid": 0.50,
                        "under_ask": 0.98,
                        "market_id": "p1",
                    },
                    {
                        "type": "passing_yards",
                        "player": "Brian Thomas Jr.",
                        "line": 249.5,
                        "over": 0.30,
                        "under": 0.70,
                        "market_id": "p2",
                    },
                ],
            }
        ],
    }

    result = aggregate_nfl_slate(game_odds, player_props, kalshi, polymarket)
    assert result["game_count"] == 1
    assert result["kalshi_games_skipped"] == 1
    game = result["games"][0]
    assert game["away"]["abbr"] == "CLE"
    assert game["home"]["abbr"] == "JAX"
    assert game["start_time_ms"] == kickoff_ms("2026-09-13 13:00:00")
    assert game["markets"]["spread"]["consensus_line"] == -3.5
    assert game["markets"]["spread"]["kalshi"]["line"] == -3.5
    assert game["markets"]["spread"]["kalshi"]["yes_side"] == "home"
    assert game["markets"]["spread"]["polymarket"]["home_line"] == -3.5
    assert game["markets"]["total"]["kalshi"]["line"] == 44.5
    assert game["markets"]["total"]["polymarket"]["points"] == 44.5
    assert game["markets"]["moneyline"]["kalshi"]["home"]["implied_prob"] == 0.645

    props = game["player_props"]
    assert len(props) == 1
    row = props[0]
    assert row["player"] == "Brian Thomas"
    assert row["playerID"] == "99"
    assert row["type"] == "passing_yards"
    assert row["consensus_line"] == 212.5
    assert row["kalshi"]["line"] == 224.5
    assert row["kalshi"]["line_delta"] == 12.0
    assert row["polymarket"]["line"] == 209.5
    assert row["polymarket"]["over"] == 0.51
    assert row["polymarket"]["over_ask"] == 0.50
    assert row["polymarket"]["under_ask"] == 0.98
    assert "draftkings" in row["books"]
    assert row["books"]["draftkings"]["over_implied_prob"] is not None
