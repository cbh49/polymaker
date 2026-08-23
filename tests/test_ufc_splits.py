"""UFC fighter matching, sharp-money source selection, and Polymarket slug shape."""

from __future__ import annotations

import sys
from pathlib import Path

_AGG = Path(__file__).resolve().parents[1] / "data-aggregation"
if str(_AGG) not in sys.path:
    sys.path.insert(0, str(_AGG))

from find_sharp_money import (  # noqa: E402
    primary_source_label,
    process_game,
    sources_for_league,
)
from polymaker.catalog.sports import is_moneyline_slug
from polymaker.trading.teams import parse_matchup, resolve_team
from ufc_fighter_map import (  # noqa: E402
    align_game_to,
    names_match,
    pair_key,
    parse_vs_title,
    sides_swapped,
)


def test_ufc_name_matching() -> None:
    assert names_match("Reinier de Ridder", "Reinier De Ridder")
    assert names_match("Marquel Mederos", "MarQuel Mederos")
    assert names_match("Anthony Hernandez", "Hernandez")
    assert names_match("Serghei Spivac", "Sergey Spivak")
    assert not names_match("Mason Jones", "Jamall Emmers")


def test_ufc_pair_key_ignores_corner_order() -> None:
    assert pair_key("Shanelle Dyer", "Elise Reed") == pair_key("Elise Reed", "Shanelle Dyer")
    assert sides_swapped("Shanelle Dyer", "Elise Reed", "Elise Reed", "Shanelle Dyer") is True
    assert sides_swapped("Shanelle Dyer", "Elise Reed", "Shanelle Dyer", "Elise Reed") is False


def test_parse_vs_title() -> None:
    assert parse_vs_title("Shanelle Dyer vs Elise Reed") == ("Shanelle Dyer", "Elise Reed")
    assert parse_matchup("Shanelle Dyer vs Elise Reed") == ("Shanelle Dyer", "Elise Reed")


def test_align_game_swaps_moneyline() -> None:
    src = {
        "away": "Elise Reed",
        "home": "Shanelle Dyer",
        "away_abbr": "Elise Reed",
        "home_abbr": "Shanelle Dyer",
        "matchup": "Elise Reed vs Shanelle Dyer",
        "moneyline": {
            "away": {"selection": "Elise Reed", "vsin_handle_bet_pct": 52},
            "home": {"selection": "Shanelle Dyer", "vsin_handle_bet_pct": 48},
        },
    }
    dest = {"away": "Shanelle Dyer", "home": "Elise Reed"}
    aligned = align_game_to(src, dest)
    assert aligned is not None
    assert aligned["away"] == "Shanelle Dyer"
    assert aligned["moneyline"]["away"]["vsin_handle_bet_pct"] == 48
    assert aligned["moneyline"]["home"]["vsin_handle_bet_pct"] == 52


def test_align_game_fixes_flopped_thespread_prices() -> None:
    """TheSpread names can sit on the opposite ML; re-pair by DK live price."""
    dest = {
        "away": "Anthony Hernandez",
        "home": "Gregory Rodrigues",
        "moneyline": {
            "away": {"selection": "Anthony Hernandez", "live": -225},
            "home": {"selection": "Gregory Rodrigues", "live": 185},
        },
    }
    src = {
        "away": "Anthony Hernandez",
        "home": "Gregory Rodrigues",
        "moneyline": {
            "away": {"selection": "Anthony Hernandez", "open": 122, "live": 173},
            "home": {"selection": "Gregory Rodrigues", "open": -142, "live": -205},
        },
    }
    aligned = align_game_to(src, dest)
    assert aligned is not None
    assert aligned["moneyline"]["away"]["open"] == -142
    assert aligned["moneyline"]["away"]["live"] == -205
    assert aligned["moneyline"]["home"]["open"] == 122
    assert aligned["moneyline"]["home"]["live"] == 173


def test_ufc_sharp_sources() -> None:
    assert sources_for_league("UFC") == ("primary", "vsin")
    assert primary_source_label("UFC") == "draftkings"


def test_ufc_process_game_tier_a() -> None:
    game = {
        "matchup": "Chris Padilla vs Nasrat Haqparast",
        "away": "Chris Padilla",
        "home": "Nasrat Haqparast",
        "date": "2026-08-22",
        "moneyline": {
            "away": {
                "selection": "Chris Padilla",
                "public_bet_pct": 25,
                "handle_bet_pct": 83,
                "vsin_public_bet_pct": 23,
                "vsin_handle_bet_pct": 86,
                "open": 150,
                "live": -115,
            },
            "home": {
                "selection": "Nasrat Haqparast",
                "public_bet_pct": 75,
                "handle_bet_pct": 17,
                "vsin_public_bet_pct": 77,
                "vsin_handle_bet_pct": 14,
                "open": -180,
                "live": -105,
            },
        },
    }
    play = process_game(game, market="moneyline", sources=sources_for_league("UFC"))
    assert play is not None
    assert play["side"] == "Chris Padilla"
    assert play["tier"] in {"A", "A+"}
    assert play["rlm_confirmed"] is True


def test_ufc_moneyline_slug() -> None:
    assert is_moneyline_slug("ufc-ant-gre3-2026-08-22")
    assert not is_moneyline_slug("ufc-ant-gre3-2026-08-22-totals-1pt5")
    assert not is_moneyline_slug("ufc-ant-gre3-2026-08-22-go-the-distance")


def test_resolve_team_ufc() -> None:
    ref = resolve_team("UFC", "Anthony Hernandez")
    assert ref is not None
    assert ref.full_name == "Anthony Hernandez"
    assert ref.poly_code == "hernandez"
