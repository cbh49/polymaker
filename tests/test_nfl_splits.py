"""NFL team matching, sharp-money sources, and split parsers."""

from __future__ import annotations

import sys
from datetime import date
from pathlib import Path

_AGG = Path(__file__).resolve().parents[1] / "data-aggregation"
if str(_AGG) not in sys.path:
    sys.path.insert(0, str(_AGG))

from find_sharp_money import (  # noqa: E402
    _markets_from_arg,
    config_snapshot,
    primary_source_label,
    process_game,
    sources_for_league,
)
from nfl_team_map import canonical_abbr, canonical_name, names_match  # noqa: E402
from polymaker.trading.teams import resolve_team  # noqa: E402
from scrape_dk_splits import PAGE_URLS as DK_URLS  # noqa: E402
from scrape_dk_splits import parse_games  # noqa: E402
from scrape_eva_splits import PAGE_URLS as EVA_URLS  # noqa: E402
from scrape_sbd_splits import API_URLS as SBD_API_URLS  # noqa: E402
from scrape_sbd_splits import PAGE_URLS as SBD_URLS  # noqa: E402
from scrape_sbd_splits import parse_event  # noqa: E402
from scrape_thespread_splits import PAGE_URLS as SPREAD_URLS  # noqa: E402
from slate_alignment import evaluate_payload  # noqa: E402


def test_nfl_aliases() -> None:
    assert canonical_abbr("JAC") == "JAX"
    assert canonical_abbr("WSH") == "WAS"
    assert canonical_abbr("LAR") == "LA"
    assert canonical_abbr("LAC") == "LAC"
    assert canonical_name("Wash Commanders") == "Washington Commanders"
    assert canonical_name("KC Chiefs") == "Kansas City Chiefs"
    assert canonical_name("LA Rams") == "Los Angeles Rams"
    assert canonical_name("LA Chargers") == "Los Angeles Chargers"
    assert names_match("Denver Broncos", "DEN")
    assert names_match("Wash Commanders", "WAS")
    assert names_match("Rams", "LAR")
    assert not names_match("LA Rams", "LAC")
    assert not names_match("NY Giants", "NYJ")


def test_nfl_sharp_sources() -> None:
    assert sources_for_league("NFL") == ("primary", "sbd")
    assert primary_source_label("NFL") == "draftkings"
    assert _markets_from_arg("all") == ("moneyline", "spread", "total")
    cfg = config_snapshot(("moneyline", "spread", "total"), sources_for_league("NFL"), "NFL")
    assert cfg["primary_source"] == "draftkings"
    assert cfg["rlm_source_priority"] == ["eva", "thespread", "polymarket"]
    assert cfg["w_sbd"] == 0.75


def test_nfl_source_urls() -> None:
    assert "tb_eg=NF" in DK_URLS["NFL"]
    assert "n7days" in DK_URLS["NFL"]
    assert EVA_URLS["NFL"].endswith("/nfl/odds")
    assert SBD_URLS["NFL"].endswith("/nfl/public-betting-trends/")
    assert SBD_API_URLS["NFL"].endswith("/nfl-odds")
    assert SPREAD_URLS["NFL"].endswith("/nfl-odds/")


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


def test_nfl_process_game_spread() -> None:
    game = {
        "matchup": "DEN @ KC",
        "away": "Denver Broncos",
        "home": "Kansas City Chiefs",
        "date": "2026-09-14",
        "spread": {
            "away": _side(
                public=65,
                handle=20,
                vsin_pub=64,
                vsin_h=22,
                sbd_pub=63,
                sbd_h=18,
                selection="DEN",
                open=2.5,
                live=3.5,
                live_odds=-108,
            ),
            "home": _side(
                public=35,
                handle=80,
                vsin_pub=36,
                vsin_h=78,
                sbd_pub=37,
                sbd_h=82,
                selection="KC",
                open=-2.5,
                live=-3.5,
                live_odds=-112,
            ),
        },
    }
    play = process_game(game, market="spread", sources=sources_for_league("NFL"))
    assert play is not None
    assert play["home_away"] == "home"
    assert play["side"] == "KC"
    assert play["public_favors"] == "away"
    assert play["line_moved_toward"] == "home"
    assert play["rlm_confirmed"] is True
    assert play["market"] == "spread"
    assert play["play_label"] == "Kansas City Chiefs -3.5"
    assert play["rlm_source_used"] == "thespread"


def test_nfl_process_game_total() -> None:
    game = {
        "matchup": "DEN @ KC",
        "date": "2026-09-14",
        "total": {
            "over": _side(
                public=84,
                handle=19,
                vsin_pub=81,
                vsin_h=20,
                sbd_pub=80,
                sbd_h=18,
                selection="Over",
                open=44.5,
                live=43.5,
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
                open=44.5,
                live=43.5,
                live_odds=-112,
            ),
        },
    }
    play = process_game(game, market="total", sources=sources_for_league("NFL"))
    assert play is not None
    assert play["home_away"] == "under"
    assert play["side"] == "Under"
    assert play["public_favors"] == "over"
    assert play["line_moved_toward"] == "under"
    assert play["rlm_confirmed"] is True


def test_sbd_nfl_event() -> None:
    event = {
        "scheduled": "2026-09-15T00:15:00Z",
        "competitors": {
            "away": {"abbreviation": "DEN", "market": "Denver", "name": "Broncos"},
            "home": {"abbreviation": "KC", "market": "Kansas City", "name": "Chiefs"},
        },
        "bettingSplits": {
            "moneyline": {
                "updated": "2026-09-14T12:00:00Z",
                "away": {"betsPercentage": 36, "stakePercentage": 13},
                "home": {"betsPercentage": 64, "stakePercentage": 87},
            },
            "spread": {
                "away": {"betsPercentage": 65, "stakePercentage": 20},
                "home": {"betsPercentage": 35, "stakePercentage": 80},
            },
            "total": {
                "over": {"betsPercentage": 51, "stakePercentage": 58},
                "under": {"betsPercentage": 49, "stakePercentage": 42},
            },
        },
        "markets": {"moneyline": {"books": []}, "spread": {"books": []}, "total": {"books": []}},
    }
    parsed = parse_event(event, {"DEN": "Denver Broncos", "KC": "Kansas City Chiefs"}, [])
    assert parsed is not None
    assert parsed["away_abbr"] == "DEN"
    assert parsed["home_abbr"] == "KC"
    assert parsed["spread"]["home"]["sbd_handle_bet_pct"] == 80


def _dk_card(matchup: str, when: str, event_id: str = "1") -> str:
    return (
        '<div class="tb-se">'
        '<div class="tb-se-title">'
        f'<a href="/event/{event_id}">{matchup}</a>'
        f"<span>{when}</span>"
        "</div></div>"
    )


def test_dk_nfl_parses_monday_night() -> None:
    html = _dk_card("Denver Broncos @ Kansas City Chiefs", "Mon 9/14 5:15 PM", "10")
    games = parse_games(
        html,
        [],
        league="NFL",
        canonical_name_fn=canonical_name,
        canonical_abbr_fn=canonical_abbr,
        names_match_fn=names_match,
        match_matchup_fn=lambda *_args, **_kw: None,
        day=date(2026, 9, 14),
    )
    assert len(games) == 1
    assert games[0]["away_abbr"] == "DEN"
    assert games[0]["home_abbr"] == "KC"
    assert games[0]["date"] == "2026-09-14"


def test_resolve_team_nfl() -> None:
    kc = resolve_team("NFL", "KC")
    assert kc is not None
    assert kc.poly_code == "kc"
    assert kc.full_name == "Kansas City Chiefs"
    rams = resolve_team("NFL", "LAR")
    assert rams is not None and rams.poly_code == "la"
    chargers = resolve_team("NFL", "LAC")
    assert chargers is not None and chargers.poly_code == "lac"


def test_nfl_weekend_window_aligns() -> None:
    """Monday slate includes next Sunday NFL games (window = 7 days)."""
    game = {
        "matchup": "GB @ NYJ",
        "date": "2026-09-20",
        "moneyline": {
            "away": {
                "public_bet_pct": 40,
                "handle_bet_pct": 55,
                "vsin_handle_bet_pct": 60,
                "sbd_handle_bet_pct": 50,
            },
            "home": {
                "public_bet_pct": 60,
                "handle_bet_pct": 45,
                "vsin_handle_bet_pct": 40,
                "sbd_handle_bet_pct": 50,
            },
        },
    }
    payload = {
        "league": "NFL",
        "date": "2026-09-14",
        "games": [game],
        "sources": {
            "draftkings": {"native_dates": ["2026-09-20"], "game_count": 1},
            "vsin": {"native_dates": ["2026-09-20"], "game_count": 1},
            "sportsbettingdime": {"native_dates": ["2026-09-20"], "game_count": 1},
        },
    }
    result = evaluate_payload(payload, slate_day=date(2026, 9, 14))
    assert result.aligned is True
    assert result.league == "NFL"
    assert result.overlap_count == 1
