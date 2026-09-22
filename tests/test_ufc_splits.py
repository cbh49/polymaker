"""UFC fighter matching, sharp-money source selection, and Polymarket slug shape."""

from __future__ import annotations

import sys
from datetime import date, timedelta
from pathlib import Path

_AGG = Path(__file__).resolve().parents[1] / "data-aggregation"
if str(_AGG) not in sys.path:
    sys.path.insert(0, str(_AGG))

from find_sharp_money import (  # noqa: E402
    primary_source_label,
    process_game,
    sources_for_league,
)
from polymaker.catalog.sports import is_moneyline_slug, look_ahead_days_for_series
from polymaker.trading.teams import parse_matchup, resolve_team
from scrape_dk_splits import parse_games
from ufc_fighter_map import (  # noqa: E402
    UFC_LOOKAHEAD_DAYS,
    align_game_to,
    canonical_name,
    filter_to_next_ufc_card,
    names_match,
    next_ufc_card_dates,
    pair_key,
    parse_vs_title,
    sides_swapped,
    ufc_allowed_days,
)


def test_dk_ufc_filters_to_slate_date() -> None:
    html = """
    <div class="tb-se">
      <div class="tb-se-title">
        <a href="/event/1">Shanelle Dyer vs Elise Reed</a>
        <span>Sat 8/22 7:00 PM</span>
      </div>
    </div>
    <div class="tb-se">
      <div class="tb-se-title">
        <a href="/event/2">Chris Padilla vs Nasrat Haqparast</a>
        <span>Sat 8/30 7:00 PM</span>
      </div>
    </div>
    <div class="tb-se">
      <div class="tb-se-title">
        <a href="/event/3">Anthony Hernandez vs Gregory Rodrigues</a>
        <span>7:00 PM</span>
      </div>
    </div>
    """
    games = parse_games(
        html,
        [],
        league="UFC",
        canonical_name_fn=canonical_name,
        canonical_abbr_fn=lambda name: name,
        names_match_fn=names_match,
        day=date(2026, 8, 22),
    )
    assert len(games) == 1
    assert games[0]["date"] == "2026-08-22"
    assert games[0]["away"] == "Shanelle Dyer"
    assert games[0]["home"] == "Elise Reed"


def test_dk_ufc_lookahead_keeps_next_card() -> None:
    html = """
    <div class="tb-se">
      <div class="tb-se-title">
        <a href="/event/1">Shanelle Dyer vs Elise Reed</a>
        <span>Sat 8/22 7:00 PM</span>
      </div>
    </div>
    <div class="tb-se">
      <div class="tb-se-title">
        <a href="/event/2">Chris Padilla vs Nasrat Haqparast</a>
        <span>Sat 8/30 7:00 PM</span>
      </div>
    </div>
    """
    as_of = date(2026, 8, 21)
    games = parse_games(
        html,
        [],
        league="UFC",
        canonical_name_fn=canonical_name,
        canonical_abbr_fn=lambda name: name,
        names_match_fn=names_match,
        day=as_of,
        allowed_days=ufc_allowed_days(as_of),
    )
    assert {g["date"] for g in games} == {"2026-08-22", "2026-08-30"}
    kept, card = filter_to_next_ufc_card(games, as_of)
    assert card == {date(2026, 8, 22)}
    assert len(kept) == 1
    assert kept[0]["away"] == "Shanelle Dyer"


def test_next_ufc_card_includes_friday_prelims() -> None:
    games = [
        {"date": "2026-08-28", "matchup": "prelim"},
        {"date": "2026-08-29", "matchup": "main"},
        {"date": "2026-09-05", "matchup": "next week"},
    ]
    kept, card = filter_to_next_ufc_card(games, date(2026, 8, 28))
    assert card == {date(2026, 8, 28), date(2026, 8, 29)}
    assert {g["matchup"] for g in kept} == {"prelim", "main"}
    assert next_ufc_card_dates(
        [date(2026, 8, 29), date(2026, 9, 5)], date(2026, 8, 28)
    ) == {date(2026, 8, 29)}
    assert max(ufc_allowed_days(date(2026, 8, 28))) == date(2026, 8, 28) + timedelta(
        days=UFC_LOOKAHEAD_DAYS
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
    assert sources_for_league("UFC") == ("primary",)
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
    assert look_ahead_days_for_series("ufc", 3) == 14


def test_resolve_team_ufc() -> None:
    ref = resolve_team("UFC", "Anthony Hernandez")
    assert ref is not None
    assert ref.full_name == "Anthony Hernandez"
    assert ref.poly_code == "hernandez"
