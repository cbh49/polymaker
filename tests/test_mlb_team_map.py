"""Bundled MLB team map + TheSpread scrape resilience."""

from __future__ import annotations

import sys
from datetime import date
from pathlib import Path
from unittest.mock import patch

_AGG = Path(__file__).resolve().parents[1] / "data-aggregation"
if str(_AGG) not in sys.path:
    sys.path.insert(0, str(_AGG))

from mlb_team_map import (  # noqa: E402
    canonical_abbr,
    canonical_name,
    load_abbr_to_name,
)
from scrape_playerprops_splits import load_abbr_to_team, parse_event  # noqa: E402
from scrape_thespread_splits import (  # noqa: E402
    _html_has_selector_token,
    parse_games,
    scrape as scrape_thespread,
)


def test_bundled_map_resolves_without_parent_repo_json(tmp_path: Path) -> None:
    missing = tmp_path / "nope.json"
    mapping = load_abbr_to_name(missing)
    assert mapping["TOR"] == "Toronto Blue Jays"
    assert mapping["AZ"] == "Arizona Diamondbacks"
    assert mapping["CWS"] == "Chicago White Sox"
    assert mapping["NYY"] == "New York Yankees"


def test_canonical_abbr_aliases() -> None:
    assert canonical_abbr("AZ") == "ARI"
    assert canonical_abbr("CHW") == "CWS"
    assert canonical_abbr("WAS") == "WSH"
    assert canonical_name("TOR") == "Toronto Blue Jays"
    assert canonical_name("Jays") == "Toronto Blue Jays"


def test_playerprops_names_without_abbrevs_file(tmp_path: Path) -> None:
    mapping = load_abbr_to_team(tmp_path / "missing.json")
    event = {
        "league": "MLB",
        "team1": {"teamName": "NYY", "side": "home"},
        "team2": {"teamName": "TOR", "side": "away"},
        "cardInfo": {},
        "date": "2026-08-22T17:35:00.000Z",
    }
    parsed = parse_event(event, mapping, [])
    assert parsed is not None
    assert parsed["away"] == "Toronto Blue Jays"
    assert parsed["home"] == "New York Yankees"
    assert parsed["matchup"] == "TOR @ NYY"


def test_thespread_parses_datarow_html() -> None:
    html = """
    <div class="datarow">
      <div class="datacell time"><span class="dataheader">Time</span>8/22<span>09:00 PM</span></div>
      <div class="datacell teams">
        <span id="tmv">Connecticut Sun</span>
        <span id="tmh">Los Angeles Sparks</span>
      </div>
      <div class="child-open">+3.5 -109
-3.5 -114</div>
      <div class="child-current">+5.5 -114
-5.5 -106</div>
    </div>
    """
    games = parse_games(html, date(2026, 8, 22), [])
    assert len(games) == 1
    assert games[0]["matchup"] == "CON @ LA"
    assert games[0]["spread"]["away"]["open"] == 3.5
    assert games[0]["spread"]["home"]["live"] == -5.5


def test_thespread_scrape_returns_empty_when_rows_missing() -> None:
    with (
        patch("scrape_thespread_splits._fetch_static_html", return_value="<html>blocked</html>"),
        patch(
            "scrape_thespread_splits.fetch_rendered_html",
            return_value="<html>just a moment</html>",
        ),
    ):
        result = scrape_thespread(day=date(2026, 8, 22))
    assert result["game_count"] == 0
    assert result["games"] == []


def test_thespread_scrape_swallows_playwright_timeout() -> None:
    with (
        patch("scrape_thespread_splits._fetch_static_html", return_value="<html></html>"),
        patch(
            "scrape_thespread_splits.fetch_rendered_html",
            side_effect=TimeoutError("Page.wait_for_selector"),
        ),
    ):
        result = scrape_thespread(day=date(2026, 8, 22))
    assert result["game_count"] == 0
    assert result["games"] == []


def test_html_has_selector_token() -> None:
    assert _html_has_selector_token('<div class="datarow"></div>', ".datarow") is True
    assert _html_has_selector_token("<html>datarow in script</html>", ".datarow") is False


def test_mlb_find_game_matches_az_to_ari() -> None:
    from scrape_mlb_betting_splits import _find_game, _index_games

    pp = {
        "matchup": "AZ @ ATL",
        "away_abbr": "AZ",
        "home_abbr": "ATL",
        "away": "Arizona Diamondbacks",
        "home": "Atlanta Braves",
    }
    vsin = {
        "matchup": "ARI @ ATL",
        "away_abbr": "ARI",
        "home_abbr": "ATL",
        "away": "Arizona Diamondbacks",
        "home": "Atlanta Braves",
    }
    by_matchup, by_teams = _index_games([vsin])
    assert _find_game(pp, by_matchup, by_teams) is vsin
