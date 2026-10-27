"""NHL team matching, puck line, sharp-money sources, and split URL constants."""

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
from nhl_team_map import canonical_abbr, canonical_name, names_match, poly_code  # noqa: E402
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


def test_nhl_aliases() -> None:
    assert canonical_abbr("LA") == "LAK"
    assert canonical_abbr("LA Kings") == "LAK"
    assert canonical_name("LA Kings") == "Los Angeles Kings"
    assert canonical_abbr("NY Islanders") == "NYI"
    assert canonical_abbr("NY Rangers") == "NYR"
    assert canonical_abbr("NY") is None
    assert canonical_name("MTL Canadiens") == "Montreal Canadiens"
    assert canonical_abbr("MON") == "MTL"
    assert canonical_abbr("TB") == "TBL"
    assert canonical_abbr("SJ") == "SJS"
    assert canonical_abbr("NJ") == "NJD"
    assert canonical_abbr("UTAH") == "UTA"
    assert canonical_name("Utah Hockey Club") == "Utah Mammoth"
    assert names_match("NY Islanders", "NYI")
    assert names_match("DAL Stars", "Dallas Stars")
    assert not names_match("NY Islanders", "NY Rangers")
    assert not names_match("NY Islanders", "NYR")
    assert poly_code("MTL") == "mon"
    assert poly_code("CGY") == "cal"
    assert poly_code("VGK") == "las"
    assert poly_code("UTA") == "utah"
    assert poly_code("SJS") == "sj"
    assert poly_code("TBL") == "tb"
    assert poly_code("NJD") == "nj"
    assert poly_code("LAK") == "lak"


def test_nhl_sharp_sources() -> None:
    assert sources_for_league("NHL") == ("primary", "sbd", "sao")
    assert primary_source_label("NHL") == "draftkings"
    cfg = config_snapshot(("moneyline", "spread", "total"), sources_for_league("NHL"), "NHL")
    assert cfg["primary_source"] == "draftkings"
    assert cfg["rlm_source_priority"] == ["eva", "thespread", "polymarket"]


def test_nhl_source_urls() -> None:
    assert "tb_eg=42133" in DK_URLS["NHL"]
    assert "n7days" in DK_URLS["NHL"]
    assert "itm_content=NHL" in DK_URLS["NHL"]
    assert EVA_URLS["NHL"].endswith("/nhl/odds")
    assert COVERS_URLS["NHL"].endswith("/hockey/nhl/odds")
    assert SBD_URLS["NHL"].endswith("/nhl/public-betting-trends/")
    assert SBD_API_URLS["NHL"].endswith("/nhl-odds")
    assert SPREAD_URLS["NHL"].endswith("/nhl-hockey-public-betting-chart/")
    slugs = poly_series_slugs("NHL", date(2026, 10, 25))
    assert slugs[0] == "nhl"
    assert "nhl-2026" in slugs


def test_resolve_team_nhl() -> None:
    isles = resolve_team("NHL", "NY Islanders")
    assert isles is not None
    assert isles.poly_code == "nyi"
    assert isles.betting_abbr == "NYI"
    assert isles.full_name == "New York Islanders"
    kings = resolve_team("NHL", "LA Kings")
    assert kings is not None and kings.poly_code == "lak"
    canadiens = resolve_team("NHL", "MON")
    assert canadiens is not None and canadiens.poly_code == "mon"
    assert canadiens.betting_abbr == "MTL"
    flames = resolve_team("NHL", "CGY")
    assert flames is not None and flames.poly_code == "cal"
    knights = resolve_team("NHL", "VGK")
    assert knights is not None and knights.poly_code == "las"
    assert resolve_team("NHL", "NY") is None


def _dk_card(body: str) -> str:
    return f'<div class="tb-se">{body}</div>'


def test_dk_nhl_abbr_and_puck_line() -> None:
    html = (
        '<div class="tb-se">'
        '<div class="tb-se-title">'
        '<a href="/event/1">NY Islanders @ DAL Stars</a>'
        "<span>10/25, 06:10PM</span>"
        "</div>"
        '<div class="tb-market-wrap"><div>'
        '<div class="tb-se-head">Puck Line</div>'
        '<div class="tb-sodd">'
        "<div>NY Islanders +1.5</div><div>-166</div><div>65%</div><div>39%</div>"
        "</div>"
        '<div class="tb-sodd">'
        "<div>DAL Stars -1.5</div><div>+140</div><div>35%</div><div>61%</div>"
        "</div>"
        "</div></div></div>"
        + _dk_card(
            '<div class="tb-se-title">'
            '<a href="/event/2">LA Kings @ NY Rangers</a>'
            "<span>10/26, 07:10PM</span></div>"
        )
    )
    games = parse_games(
        html,
        [],
        league="NHL",
        canonical_name_fn=canonical_name,
        canonical_abbr_fn=canonical_abbr,
        names_match_fn=names_match,
        match_matchup_fn=lambda *_args, **_kw: None,
        day=date(2026, 10, 25),
    )
    assert [g["matchup"] for g in games] == ["NYI @ DAL", "LAK @ NYR"]
    assert games[0]["away"] == "New York Islanders"
    spread = games[0]["spread"]
    assert spread["away"]["live"] == 1.5
    assert spread["away"]["live_odds"] == -166
    assert spread["home"]["live"] == -1.5
    assert spread["home"]["handle_bet_pct"] == 35


def _ml_game(day: str) -> dict:
    return {
        "matchup": "NYI @ DAL",
        "date": day,
        "moneyline": {
            "away": {"public_bet_pct": 12, "handle_bet_pct": 29, "sbd_handle_bet_pct": 20},
            "home": {"public_bet_pct": 88, "handle_bet_pct": 71, "sbd_handle_bet_pct": 80},
        },
    }


def test_nhl_week_window_aligns_only_with_both_sources() -> None:
    slate = date(2026, 10, 25)
    both = {
        "league": "NHL",
        "date": "2026-10-25",
        "games": [_ml_game("2026-10-28")],
        "sources": {
            "draftkings": {"native_dates": ["2026-10-28"], "game_count": 1},
            "sportsbettingdime": {"native_dates": ["2026-10-28"], "game_count": 1},
        },
    }
    aligned = evaluate_payload(both, slate_day=slate)
    assert aligned.aligned is True
    assert aligned.league == "NHL"
    assert aligned.overlap_count == 1

    dk_only = _ml_game("2026-10-25")
    for side in dk_only["moneyline"].values():
        side.pop("sbd_handle_bet_pct", None)
    missing_sbd = {
        "league": "NHL",
        "date": "2026-10-25",
        "games": [dk_only],
        "sources": {
            "draftkings": {"native_dates": ["2026-10-25"], "game_count": 1},
        },
    }
    assert evaluate_payload(missing_sbd, slate_day=slate).aligned is False


def test_nhl_catalog_slugs() -> None:
    assert is_moneyline_slug("nhl-nyi-dal-2026-10-25")
    assert is_spread_slug("nhl-nyi-dal-2026-10-25-spread-home-1pt5")
    assert is_total_slug("nhl-mon-wpg-2026-10-25-total-5pt5")
    assert is_sports_series("nhl")
    assert is_sports_series("nhl-2026")
    slugs = default_sports_series_slugs(date(2026, 10, 25))
    assert "nhl" in slugs
    assert "nhl-2026" in slugs
