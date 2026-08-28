"""NCAAF / CFB team matching, sharp-money sources, and split parsers."""

from __future__ import annotations

import sys
from pathlib import Path

_AGG = Path(__file__).resolve().parents[1] / "data-aggregation"
if str(_AGG) not in sys.path:
    sys.path.insert(0, str(_AGG))

from cfb_team_map import (  # noqa: E402
    canonical_abbr,
    canonical_name,
    names_match,
    poly_code,
)
from find_sharp_money import (  # noqa: E402
    _markets_from_arg,
    primary_source_label,
    process_game,
    sources_for_league,
)
from polymaker.catalog.sports import is_moneyline_slug, look_ahead_days_for_series
from polymaker.trading.teams import resolve_team
from scrape_dk_splits import PAGE_URLS as DK_URLS
from scrape_sbd_splits import has_usable_splits, parse_event
from scrape_thespread_splits import PAGE_URLS as SPREAD_URLS


def test_cfb_hawaii_aliases() -> None:
    assert canonical_abbr("HAW") == "HAW"
    assert canonical_name("Hawai'i") == "Hawaii"
    assert canonical_name("Hawaii Rainbow Warriors") == "Hawaii"
    assert names_match("Hawai'i", "HAW")
    assert poly_code("HAW") == "hawaii"


def test_cfb_san_jose_state() -> None:
    assert canonical_name("San Jose ST Spartans") == "San Jose State"
    assert canonical_abbr("San Jose State") == "SJSU"
    assert names_match("San Jose State", "SJSU")


def test_cfb_nc_state() -> None:
    assert canonical_abbr("NCST") == "NCST"
    assert canonical_abbr("NC State") == "NCST"
    assert canonical_name("North Carolina State") == "NC State"
    assert names_match("NCST", "NC State")


def test_cfb_stanford_cardinal_mascot() -> None:
    """VSiN labels Stanford as 'Stanford Cardinal'; that must still map to STAN."""
    assert canonical_name("Stanford Cardinal") == "Stanford"
    assert canonical_abbr("Stanford Cardinal") == "STAN"
    assert names_match("Stanford", "Stanford Cardinal")
    assert names_match("HAW", "Hawaii")


def test_cfb_unknown_fcs_abbr_stays_uppercase() -> None:
    assert canonical_abbr("STON") == "STON"
    assert canonical_abbr("DSU") == "DSU"


def test_ncaaf_sharp_sources() -> None:
    assert sources_for_league("NCAAF") == ("primary", "vsin", "sbd")
    assert sources_for_league("CFB") == ("primary", "vsin", "sbd")
    assert primary_source_label("NCAAF") == "draftkings"
    assert primary_source_label("CFB") == "draftkings"
    assert _markets_from_arg("all") == ("moneyline", "spread", "total")


def _side(*, public: int, handle: int, vsin_pub: int, vsin_h: int, sbd_pub: int, sbd_h: int, **extra):
    row = {
        "public_bet_pct": public,
        "handle_bet_pct": handle,
        "vsin_public_bet_pct": vsin_pub,
        "vsin_handle_bet_pct": vsin_h,
        "sbd_public_bet_pct": sbd_pub,
        "sbd_handle_bet_pct": sbd_h,
    }
    row.update(extra)
    return row


def test_ncaaf_process_game_moneyline() -> None:
    game = {
        "matchup": "UNC @ TCU",
        "away": "North Carolina",
        "home": "TCU",
        "date": "2026-08-29",
        "moneyline": {
            "away": _side(
                public=18,
                handle=38,
                vsin_pub=20,
                vsin_h=40,
                sbd_pub=19,
                sbd_h=41,
                selection="UNC",
                open=280,
                live=250,
            ),
            "home": _side(
                public=82,
                handle=62,
                vsin_pub=80,
                vsin_h=60,
                sbd_pub=81,
                sbd_h=59,
                selection="TCU",
                open=-355,
                live=-320,
            ),
        },
    }
    play = process_game(game, market="moneyline", sources=sources_for_league("NCAAF"))
    assert play is not None
    assert play["side"] == "UNC"
    assert play["home_away"] == "away"
    assert play["rlm_confirmed"] is True
    assert play["tier"] in {"A", "A+"}


def test_ncaaf_process_game_total() -> None:
    game = {
        "matchup": "MEM @ UNLV",
        "date": "2026-08-29",
        "total": {
            "over": _side(
                public=84,
                handle=19,
                vsin_pub=81,
                vsin_h=20,
                sbd_pub=80,
                sbd_h=18,
                selection="Over",
                open=57.5,
                live=56.5,
                live_odds=-108,
            ),
            "under": _side(
                public=16,
                handle=81,
                vsin_pub=19,
                vsin_h=80,
                sbd_pub=20,
                sbd_h=82,
                selection="Under",
                open=57.5,
                live=56.5,
                live_odds=-112,
            ),
        },
    }
    play = process_game(game, market="total", sources=sources_for_league("NCAAF"))
    assert play is not None
    assert play["home_away"] == "under"
    assert play["side"] == "Under"
    assert play["public_favors"] == "over"
    assert play["line_moved_toward"] == "under"
    assert play["rlm_confirmed"] is True
    assert play["tier"] in {"A", "A+", "B"}


def test_ncaaf_total_rlm_falls_back_to_eva() -> None:
    game = {
        "matchup": "HAW @ STAN",
        "date": "2026-08-29",
        "total": {
            "over": _side(
                public=34,
                handle=80,
                vsin_pub=37,
                vsin_h=78,
                sbd_pub=40,
                sbd_h=82,
                selection="Over",
                eva_open=48.5,
                eva_line=49.5,
                live_odds=-112,
            ),
            "under": _side(
                public=66,
                handle=20,
                vsin_pub=63,
                vsin_h=22,
                sbd_pub=60,
                sbd_h=18,
                selection="Under",
                eva_open=48.5,
                eva_line=49.5,
                live_odds=-108,
            ),
        },
    }
    play = process_game(game, market="total", sources=sources_for_league("NCAAF"))
    assert play is not None
    assert play["home_away"] == "over"
    assert play["line_moved_toward"] == "over"
    assert play["public_favors"] == "under"
    assert play["rlm_confirmed"] is True
    assert play["open"] == 48.5
    assert play["live"] == 49.5


def test_sbd_ncaaf_abbreviation_and_market() -> None:
    event = {
        "scheduled": "2026-08-29T23:00:00Z",
        "competitors": {
            "away": {"abbreviation": "HAW", "market": "Hawaii", "name": "Rainbow Warriors"},
            "home": {"abbreviation": "STAN", "market": "Stanford", "name": "Cardinal"},
        },
        "bettingSplits": {
            "moneyline": {
                "updated": "2026-08-27T12:00:00Z",
                "away": {"betsPercentage": 40, "stakePercentage": 56},
                "home": {"betsPercentage": 60, "stakePercentage": 44},
            },
            "spread": {
                "away": {"betsPercentage": 37, "stakePercentage": 45},
                "home": {"betsPercentage": 63, "stakePercentage": 55},
            },
            "total": {
                "over": {"betsPercentage": 66, "stakePercentage": 96},
                "under": {"betsPercentage": 34, "stakePercentage": 4},
            },
        },
        "markets": {"moneyline": {"books": []}, "spread": {"books": []}, "total": {"books": []}},
    }
    parsed = parse_event(event, {"HAW": "Hawaii", "STAN": "Stanford"}, [])
    assert parsed is not None
    assert parsed["away_abbr"] == "HAW"
    assert parsed["home_abbr"] == "STAN"
    assert parsed["total"]["over"]["sbd_public_bet_pct"] == 66
    assert parsed["total"]["over"]["sbd_handle_bet_pct"] == 96


def test_sbd_empty_string_splits_are_unusable() -> None:
    splits = {
        "moneyline": {
            "updated": "2026-08-27T12:00:00Z",
            "away": {"betsPercentage": "", "stakePercentage": ""},
            "home": {"betsPercentage": "", "stakePercentage": ""},
        }
    }
    assert has_usable_splits(splits) is False


def test_dk_and_thespread_ncaaf_urls() -> None:
    assert "NCAA+Football" in DK_URLS["NCAAF"]
    assert DK_URLS["NCAAF"].endswith("itm_content=NCAA+Football") or "tb_eg=NCAA+Football" in DK_URLS["NCAAF"]
    assert "ncaa-college-football-public-betting-chart" in SPREAD_URLS["NCAAF"]


def test_cfb_moneyline_slug() -> None:
    assert is_moneyline_slug("cfb-hawaii-stan-2026-08-29")
    assert not is_moneyline_slug("cfb-hawaii-stan-2026-08-29-total-49pt5")
    assert look_ahead_days_for_series("cfb-2026", 3) == 7


def test_resolve_team_ncaaf() -> None:
    haw = resolve_team("NCAAF", "HAW")
    assert haw is not None
    assert haw.poly_code == "hawaii"
    assert haw.full_name == "Hawaii"
    ncst = resolve_team("CFB", "NC State")
    assert ncst is not None
    assert ncst.betting_abbr == "NCST"


def test_ncaaf_process_game_spread() -> None:
    game = {
        "matchup": "HAW @ STAN",
        "date": "2026-08-29",
        "spread": {
            "away": _side(
                public=39,
                handle=77,
                vsin_pub=40,
                vsin_h=75,
                sbd_pub=38,
                sbd_h=70,
                selection="HAW",
                open=6.0,
                live=3.5,
                live_odds=-108,
            ),
            "home": _side(
                public=61,
                handle=23,
                vsin_pub=60,
                vsin_h=25,
                sbd_pub=62,
                sbd_h=30,
                selection="STAN",
                open=-6.0,
                live=-3.5,
                live_odds=-112,
            ),
        },
    }
    play = process_game(game, market="spread", sources=sources_for_league("NCAAF"))
    assert play is not None
    assert play["home_away"] == "away"
    assert play["side"] == "HAW"
    assert play["public_favors"] == "home"
    assert play["line_moved_toward"] == "away"
    assert play["rlm_confirmed"] is True
    assert play["market"] == "spread"
