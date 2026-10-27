"""NBA team matching, sharp-money sources, and split URL constants."""

from __future__ import annotations

import sys
from datetime import date
from pathlib import Path

_AGG = Path(__file__).resolve().parents[1] / "data-aggregation"
if str(_AGG) not in sys.path:
    sys.path.insert(0, str(_AGG))

from find_sharp_money import (  # noqa: E402
    config_snapshot,
    primary_source_label,
    sources_for_league,
)
from nba_team_map import canonical_abbr, canonical_name, names_match  # noqa: E402
from polymaker.catalog.sports import (  # noqa: E402
    default_sports_series_slugs,
    is_moneyline_slug,
    is_sports_series,
    is_spread_slug,
    is_total_slug,
)
from polymaker.trading.teams import resolve_team  # noqa: E402
from scrape_covers_odds import PAGE_URLS as COVERS_URLS  # noqa: E402
from scrape_dk_splits import PAGE_URLS as DK_URLS  # noqa: E402
from scrape_dk_splits import parse_games  # noqa: E402
from scrape_eva_splits import PAGE_URLS as EVA_URLS  # noqa: E402
from scrape_polymarket_odds import poly_series_slugs  # noqa: E402
from scrape_sbd_splits import API_URLS as SBD_API_URLS  # noqa: E402
from scrape_sbd_splits import PAGE_URLS as SBD_URLS  # noqa: E402
from scrape_thespread_splits import PAGE_URLS as SPREAD_URLS  # noqa: E402
from slate_alignment import evaluate_payload  # noqa: E402


def test_nba_aliases() -> None:
    assert canonical_abbr("GSW") == "GS"
    assert canonical_abbr("PHX") == "PHO"
    assert canonical_abbr("NOP") == "NO"
    assert canonical_abbr("NYK") == "NY"
    assert canonical_abbr("SAS") == "SA"
    assert canonical_abbr("WSH") == "WAS"
    assert canonical_name("GS Warriors") == "Golden State Warriors"
    assert canonical_name("PHO Suns") == "Phoenix Suns"
    assert canonical_name("NO Pelicans") == "New Orleans Pelicans"
    assert canonical_abbr("LA Lakers") == "LAL"
    assert canonical_abbr("LA Clippers") == "LAC"
    assert canonical_name("LA Lakers") == "Los Angeles Lakers"
    assert canonical_name("LA Clippers") == "Los Angeles Clippers"
    assert names_match("LA Lakers", "LAL")
    assert names_match("GSW", "Golden State Warriors")
    assert not names_match("LA Lakers", "LAC")
    assert not names_match("LA Clippers", "LAL")
    assert canonical_abbr("LA") is None
    # Same abbr token, different sport — do not attach these to NBA teams.
    assert canonical_abbr("MIN Lynx") is None
    assert canonical_abbr("IND Fever") is None
    assert not names_match("MIN Lynx", "MIN")


def test_nba_sharp_sources() -> None:
    assert sources_for_league("NBA") == ("primary", "sbd", "sao")
    assert primary_source_label("NBA") == "draftkings"
    cfg = config_snapshot(("moneyline", "spread", "total"), sources_for_league("NBA"), "NBA")
    assert cfg["primary_source"] == "draftkings"
    assert cfg["rlm_source_priority"] == ["eva", "thespread", "polymarket"]


def test_nba_source_urls() -> None:
    assert "tb_eg=42648" in DK_URLS["NBA"]
    assert "n7days" in DK_URLS["NBA"]
    assert "itm_content=NBA" in DK_URLS["NBA"]
    assert EVA_URLS["NBA"].endswith("/nba/odds")
    assert COVERS_URLS["NBA"].endswith("/basketball/nba/odds")
    assert SBD_URLS["NBA"].endswith("/nba/public-betting-trends/")
    assert SBD_API_URLS["NBA"].endswith("/nba-odds")
    assert SPREAD_URLS["NBA"].endswith("/nba-basketball-public-betting-chart/")
    slugs = poly_series_slugs("NBA", date(2026, 10, 21))
    assert slugs[0] == "nba"
    assert "nba-2026" in slugs


def test_resolve_team_nba() -> None:
    warriors = resolve_team("NBA", "GS")
    assert warriors is not None
    assert warriors.poly_code == "gsw"
    assert warriors.full_name == "Golden State Warriors"
    lakers = resolve_team("NBA", "LAL")
    assert lakers is not None and lakers.poly_code == "lal"
    clippers = resolve_team("NBA", "LAC")
    assert clippers is not None and clippers.poly_code == "lac"
    assert resolve_team("NBA", "PHO") is not None
    assert resolve_team("NBA", "PHO").poly_code == "phx"  # type: ignore[union-attr]
    assert resolve_team("NBA", "NO").poly_code == "nop"  # type: ignore[union-attr]
    assert resolve_team("NBA", "NY").poly_code == "nyk"  # type: ignore[union-attr]
    assert resolve_team("NBA", "UTA").poly_code == "uta"  # type: ignore[union-attr]
    assert resolve_team("NBA", "WAS").poly_code == "was"  # type: ignore[union-attr]
    from_slug = resolve_team("NBA", "GSW")
    assert from_slug is not None and from_slug.poly_code == "gsw"


def _dk_card(matchup: str, when: str, event_id: str) -> str:
    return (
        '<div class="tb-se">'
        '<div class="tb-se-title">'
        f'<a href="/event/{event_id}">{matchup}</a>'
        f"<span>{when}</span>"
        "</div></div>"
    )


def test_dk_nba_abbr_nickname_and_drops_other_sports() -> None:
    html = (
        _dk_card("GS Warriors @ LA Lakers", "Wed 10/21 10:10 PM", "1")
        + _dk_card("PHO Suns @ NO Pelicans", "Wed 10/21 8:10 PM", "2")
        + _dk_card("LA Clippers @ POR Trail Blazers", "Thu 10/22 7:00 PM", "3")
        + _dk_card("MIN Lynx @ IND Fever", "Wed 10/21 7:00 PM", "4")
    )
    games = parse_games(
        html,
        [],
        league="NBA",
        canonical_name_fn=canonical_name,
        canonical_abbr_fn=canonical_abbr,
        names_match_fn=names_match,
        match_matchup_fn=lambda *_args, **_kw: None,
        day=date(2026, 10, 21),
    )
    matchups = [g["matchup"] for g in games]
    assert matchups == ["GS @ LAL", "PHO @ NO", "LAC @ POR"]
    assert games[0]["away"] == "Golden State Warriors"
    assert games[0]["home"] == "Los Angeles Lakers"


def _ml_game(day: str) -> dict:
    return {
        "matchup": "MIL @ WAS",
        "date": day,
        "moneyline": {
            "away": {"public_bet_pct": 19, "handle_bet_pct": 9, "sbd_handle_bet_pct": 12},
            "home": {"public_bet_pct": 81, "handle_bet_pct": 91, "sbd_handle_bet_pct": 88},
        },
    }


def test_nba_week_window_aligns_only_with_both_sources() -> None:
    slate = date(2026, 10, 21)
    both = {
        "league": "NBA",
        "date": "2026-10-21",
        "games": [_ml_game("2026-10-28")],
        "sources": {
            "draftkings": {"native_dates": ["2026-10-28"], "game_count": 1},
            "sportsbettingdime": {"native_dates": ["2026-10-28"], "game_count": 1},
        },
    }
    aligned = evaluate_payload(both, slate_day=slate)
    assert aligned.aligned is True
    assert aligned.league == "NBA"
    assert aligned.overlap_count == 1

    dk_only = _ml_game("2026-10-21")
    for side in dk_only["moneyline"].values():
        side.pop("sbd_handle_bet_pct", None)
    missing_sbd = {
        "league": "NBA",
        "date": "2026-10-21",
        "games": [dk_only],
        "sources": {
            "draftkings": {"native_dates": ["2026-10-21"], "game_count": 1},
        },
    }
    assert evaluate_payload(missing_sbd, slate_day=slate).aligned is False

    too_far = {
        "league": "NBA",
        "date": "2026-10-21",
        "games": [_ml_game("2026-10-29")],
        "sources": {
            "draftkings": {"native_dates": ["2026-10-29"], "game_count": 1},
            "sportsbettingdime": {"native_dates": ["2026-10-29"], "game_count": 1},
        },
    }
    late = evaluate_payload(too_far, slate_day=slate)
    assert late.aligned is False
    assert late.overlap_count == 0


def test_nba_catalog_slugs() -> None:
    assert is_moneyline_slug("nba-mil-was-2026-10-21")
    assert is_spread_slug("nba-gsw-lal-2026-10-21-spread-home-6pt5")
    assert is_total_slug("nba-mil-was-2026-10-21-total-238pt5")
    assert is_sports_series("nba")
    assert is_sports_series("nba-2026")
    slugs = default_sports_series_slugs(date(2026, 10, 21))
    assert "nba" in slugs
    assert "nba-2026" in slugs
