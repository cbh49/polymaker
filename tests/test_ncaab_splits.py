"""NCAAB / CBB team matching, sharp-money sources, and trade wiring."""

from __future__ import annotations

import sys
from datetime import date
from pathlib import Path

_AGG = Path(__file__).resolve().parents[1] / "data-aggregation"
_SRC = Path(__file__).resolve().parents[1] / "src"
for _p in (_SRC, _AGG):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

from cbb_team_map import (  # noqa: E402
    canonical_abbr,
    canonical_name,
    names_match,
    poly_code,
)
from find_sharp_money import (  # noqa: E402
    consensus_gap_floor,
    ml_skip_below_fair,
    primary_source_label,
    process_game,
    sources_for_league,
)
from polymaker.catalog.sports import (  # noqa: E402
    is_moneyline_slug,
    is_sports_series,
    look_ahead_days_for_series,
)
from polymaker.trading.kalshi_match import GAME_LINE_SERIES  # noqa: E402
from polymaker.trading.sharp import _league_from_name  # noqa: E402
from polymaker.trading.teams import resolve_team  # noqa: E402
from scrape_covers_odds import PAGE_URLS as COVERS_URLS  # noqa: E402
from scrape_dk_splits import PAGE_URLS as DK_URLS  # noqa: E402
from scrape_eva_splits import PAGE_URLS as EVA_URLS  # noqa: E402
from scrape_eva_splits import SPORT_CODES  # noqa: E402
from scrape_polymarket_odds import poly_event_prefix, poly_series_slugs  # noqa: E402
from scrape_sbd_splits import API_URLS as SBD_APIS  # noqa: E402
from scrape_sbd_splits import PAGE_URLS as SBD_URLS  # noqa: E402
from scrape_scoresodds_splits import PAGE_URLS as SAO_URLS  # noqa: E402
from scrape_thespread_splits import PAGE_URLS as SPREAD_URLS  # noqa: E402
from slate_alignment import slate_window_days  # noqa: E402


def test_cbb_collision_aliases() -> None:
    assert canonical_name("Miami (FL)") == "Miami"
    assert canonical_name("Miami Hurricanes") == "Miami"
    assert canonical_abbr("Miami (FL)") == "MIA"
    assert names_match("Miami", "Miami (FL)")
    assert names_match("Miami", "Miami Hurricanes")
    assert not names_match("Miami", "Miami (OH)")
    assert canonical_name("Miami (OH)") == "Miami (OH)"
    assert canonical_abbr("Miami OH") == "M-OH"

    assert canonical_name("Florida Gators") == "Florida"
    assert canonical_name("Florida State Seminoles") == "Florida State"
    assert canonical_name("North Florida Ospreys") == "North Florida"
    assert not names_match("Florida", "Florida State")
    assert not names_match("Florida", "North Florida")
    assert not names_match("Florida State", "Florida Atlantic")
    assert names_match("Florida", "FLA")
    assert names_match("Duke Blue Devils", "Duke")

    assert poly_code("Florida") == "fl"
    assert poly_code("Florida State") == "flst"
    assert poly_code("North Florida") == "nfl"
    assert poly_code("Miami") == "mia"
    assert poly_code("Miami (OH)") == "miaoh"
    assert canonical_abbr("UH") == "HOU"
    assert not names_match("UH", "Hawaii")
    assert canonical_name("Hawai'i") == "Hawaii"


def test_ncaab_sharp_sources() -> None:
    assert sources_for_league("NCAAB") == ("primary", "sbd", "sao")
    assert sources_for_league("CBB") == ("primary", "sbd", "sao")
    assert primary_source_label("NCAAB") == "draftkings"
    assert primary_source_label("CBB") == "draftkings"
    assert ml_skip_below_fair("NCAAB") == 0.20
    assert ml_skip_below_fair("CBB") == 0.20
    assert consensus_gap_floor("NCAAB") == 15.0
    assert consensus_gap_floor("CBB") == 15.0
    assert consensus_gap_floor("NBA") is None
    assert slate_window_days("NCAAB") == 7
    assert slate_window_days("CBB") == 7


def test_ncaab_source_urls() -> None:
    assert "tb_eg=92483" in DK_URLS["NCAAB"]
    assert "NCAA+Basketball" in DK_URLS["NCAAB"]
    assert SBD_URLS["NCAAB"].endswith("/college-basketball/public-betting-trends/")
    assert SBD_APIS["NCAAB"].endswith("/ncaamb-odds")
    assert SAO_URLS["NCAAB"].endswith("/ncaab/consensus-picks")
    assert EVA_URLS["NCAAB"].endswith("/ncaab/odds")
    assert SPORT_CODES["NCAAB"] == "ncaab"
    assert SPREAD_URLS["NCAAB"].endswith("/ncaa-college-basketball-public-betting-chart/")
    assert COVERS_URLS["NCAAB"].endswith("/sport/basketball/ncaab/odds")


def test_ncaab_polymarket_and_kalshi_tags() -> None:
    assert poly_event_prefix("NCAAB") == "cbb"
    assert poly_event_prefix("CBB") == "cbb"
    assert poly_series_slugs("NCAAB", date(2026, 11, 2)) == ("ncaa-cbb", "cbb")
    assert is_sports_series("ncaa-cbb")
    assert is_sports_series("cbb")
    assert look_ahead_days_for_series("ncaa-cbb", 3) == 7
    slug = "cbb-army-duke-2026-11-02"
    assert is_moneyline_slug(slug)
    assert GAME_LINE_SERIES["ncaab"] == {
        "moneyline": ("KXNCAAMBGAME",),
        "spread": ("KXNCAAMBSPREAD",),
        "total": ("KXNCAAMBTOTAL",),
    }


def test_ncaab_filename_is_not_nba() -> None:
    assert _league_from_name("ncaab_sharp_money.json") == "NCAAB"
    assert _league_from_name("cbb_sharp_money.json") == "NCAAB"
    duke = resolve_team("NCAAB", "Duke Blue Devils")
    assert duke is not None
    assert duke.betting_abbr == "DUKE"
    assert duke.poly_code == "duke"
    florida = resolve_team("CBB", "Florida")
    assert florida is not None
    assert florida.poly_code == "fl"


def _side(**extra):
    row = {
        "public_bet_pct": extra.pop("public"),
        "handle_bet_pct": extra.pop("handle"),
        "sbd_public_bet_pct": extra.pop("sbd_pub"),
        "sbd_handle_bet_pct": extra.pop("sbd_h"),
    }
    row.update(extra)
    return row


def test_ncaab_process_game_moneyline() -> None:
    game = {
        "matchup": "ARMY @ DUKE",
        "away": "Army",
        "home": "Duke",
        "date": "2026-11-02",
        "league": "NCAAB",
        "moneyline": {
            "away": _side(
                public=22,
                handle=41,
                sbd_pub=20,
                sbd_h=39,
                selection="ARMY",
                open=280,
                live=250,
            ),
            "home": _side(
                public=78,
                handle=59,
                sbd_pub=80,
                sbd_h=61,
                selection="DUKE",
                open=-355,
                live=-320,
            ),
        },
    }
    play = process_game(
        game,
        market="moneyline",
        sources=sources_for_league("NCAAB"),
        league="NCAAB",
    )
    assert play is not None
    assert play["side"] == "ARMY"
    assert play["home_away"] == "away"
    assert play["rlm_confirmed"] is True
    assert play["qualified"] is True
    assert "consensus_gap" not in play["reject_reasons"]


def _ncaab_moneyline(away: dict, home: dict) -> dict:
    return {
        "matchup": "ARMY @ DUKE",
        "away": "Army",
        "home": "Duke",
        "date": "2026-11-02",
        "league": "NCAAB",
        "moneyline": {"away": away, "home": home},
    }


def test_ncaab_thin_sbd_gap_does_not_qualify() -> None:
    """A huge DraftKings gap must not qualify on a 4-point SBD gap."""
    play = process_game(
        _ncaab_moneyline(
            _side(
                public=20,
                handle=80,
                sbd_pub=40,
                sbd_h=44,
                sao_public_bet_pct=20,
                sao_handle_bet_pct=60,
                selection="ARMY",
                open=280,
                live=250,
            ),
            _side(
                public=80,
                handle=20,
                sbd_pub=60,
                sbd_h=56,
                sao_public_bet_pct=80,
                sao_handle_bet_pct=40,
                selection="DUKE",
                open=-355,
                live=-320,
            ),
        ),
        market="moneyline",
        sources=sources_for_league("NCAAB"),
        league="NCAAB",
    )
    assert play is not None
    assert play["rlm_confirmed"] is True
    assert play["composite_gap"] >= 15
    assert play["sbd_gap"] == 4
    assert play["qualified"] is False
    assert play["tier"] is None
    assert play["would_be_tier"] == "A"
    assert "consensus_gap" in play["reject_reasons"]
    assert "gap" not in play["reject_reasons"]


def test_ncaab_sao_stands_in_only_when_it_clears_the_floor() -> None:
    def side(public, handle, sao_pub, sao_h, **extra):
        row = {
            "public_bet_pct": public,
            "handle_bet_pct": handle,
            "sao_public_bet_pct": sao_pub,
            "sao_handle_bet_pct": sao_h,
        }
        row.update(extra)
        return row

    thin = process_game(
        _ncaab_moneyline(
            side(20, 80, 40, 44, selection="ARMY", open=280, live=250),
            side(80, 20, 60, 56, selection="DUKE", open=-355, live=-320),
        ),
        market="moneyline",
        sources=sources_for_league("NCAAB"),
        league="CBB",
    )
    assert thin is not None
    assert thin["qualified"] is False
    assert "consensus_gap" in thin["reject_reasons"]

    confirmed = process_game(
        _ncaab_moneyline(
            side(20, 80, 25, 45, selection="ARMY", open=280, live=250),
            side(80, 20, 75, 55, selection="DUKE", open=-355, live=-320),
        ),
        market="moneyline",
        sources=sources_for_league("NCAAB"),
        league="NCAAB",
    )
    assert confirmed is not None
    assert confirmed["qualified"] is True
    assert confirmed["sao_gap"] == 20
    assert "consensus_gap" not in confirmed["reject_reasons"]
