"""ScoresAndOdds parsing, DraftKings outage, and SAO voting."""

from __future__ import annotations

import json
import sys
from datetime import date
from pathlib import Path

_AGG = Path(__file__).resolve().parents[1] / "data-aggregation"
if str(_AGG) not in sys.path:
    sys.path.insert(0, str(_AGG))

import find_sharp_money as sharp  # noqa: E402
from find_sharp_money import (  # noqa: E402
    COMPOSITE_MODE,
    SourceGaps,
    compute_composite,
    process_game,
    sources_for_league,
)
from scrape_dk_splits import dk_splits_unavailable, scrape as scrape_dk  # noqa: E402
from scrape_scoresodds_splits import (  # noqa: E402
    abbr_map_for,
    merge_sao_into_game,
    parse_games,
)
from slate_alignment import evaluate_payload  # noqa: E402

FIXTURES = Path(__file__).resolve().parent / "fixtures"
HTML = (FIXTURES / "sao_consensus.html").read_text(encoding="utf-8")
REGRESSION = json.loads((FIXTURES / "sao_regression_sum.json").read_text(encoding="utf-8"))


def _moneyline_side(**fields: object) -> dict:
    row = {
        "public_bet_pct": 30,
        "handle_bet_pct": 60,
        "sbd_public_bet_pct": 20,
        "sbd_handle_bet_pct": 50,
        "live": 150,
        "eva_open": 200,
        "eva_line": 100,
    }
    row.update(fields)
    return row


def _game(**overrides: object) -> dict:
    game = {
        "matchup": "DAL @ PHI",
        "away_team": "Dallas Cowboys",
        "home_team": "Philadelphia Eagles",
        "date": "2026-11-01",
        "event_id": "dal-phi",
        "moneyline": {
            "away": _moneyline_side(selection="DAL", live=150, eva_open=200, eva_line=100),
            "home": _moneyline_side(
                public_bet_pct=70,
                handle_bet_pct=40,
                sbd_public_bet_pct=80,
                sbd_handle_bet_pct=50,
                selection="PHI",
                live=-170,
                eva_open=-200,
                eva_line=-120,
            ),
        },
    }
    game.update(overrides)
    return game


def test_sao_html_parses_markets_blank_side_and_bad_sum(capsys) -> None:
    games = parse_games(HTML, "NFL", scraped_at="2026-10-27T16:00:00+00:00")
    by_matchup = {g["matchup"]: g for g in games}
    assert "ZZZ @ PHI" not in by_matchup
    err = capsys.readouterr().err
    assert "unmatched NFL ZZZ @ PHI" in err
    assert "dropping market" in err

    cowboys = by_matchup["DAL @ PHI"]
    assert cowboys["away"] == "Dallas Cowboys"
    assert cowboys["home"] == "Philadelphia Eagles"
    assert cowboys["date"] == "2026-11-01"
    ml = cowboys["moneyline"]["away"]
    assert ml["sao_public_bet_pct"] == 40
    assert ml["sao_handle_bet_pct"] == 28
    assert ml["sao_line"] is None
    assert ml["sao_scraped_at"] == "2026-10-27T16:00:00+00:00"
    assert cowboys["spread"]["away"]["sao_line"] == 4.5
    assert cowboys["spread"]["home"]["sao_line"] == -4.5
    assert cowboys["spread"]["home"]["sao_public_bet_pct"] == 45
    total = cowboys["total"]
    assert total["over"]["selection"] == "Over"
    assert total["under"]["selection"] == "Under"
    assert total["over"]["sao_public_bet_pct"] == 63
    assert total["over"]["sao_handle_bet_pct"] == 70
    assert total["over"]["sao_line"] == 44.5
    assert total["under"]["sao_public_bet_pct"] == 37

    chiefs = by_matchup["KC @ DEN"]
    assert chiefs["moneyline"]["away"]["sao_public_bet_pct"] == 91
    assert chiefs["moneyline"]["home"]["sao_public_bet_pct"] == 9
    assert chiefs["moneyline"]["home"]["sao_handle_bet_pct"] == 60
    assert "BUF @ MIA" not in by_matchup


def test_sao_abbr_map_is_explicit() -> None:
    nfl = abbr_map_for("NFL")
    assert nfl["LA"] == ("LA", "Los Angeles Rams")
    assert nfl["LAR"][0] == "LA"
    nhl = abbr_map_for("NHL")
    assert nhl["LA"][0] == "LAK"
    assert "ZZZ" not in nfl


def test_merge_does_not_overwrite_primary_fields() -> None:
    game = {
        "moneyline": {
            "away": {"selection": "DAL", "public_bet_pct": 10, "handle_bet_pct": 20},
            "home": {"selection": "PHI"},
        }
    }
    sao = {
        "moneyline": {
            "away": {
                "selection": "DAL",
                "sao_public_bet_pct": 40,
                "sao_handle_bet_pct": 28,
                "sao_line": None,
                "sao_scraped_at": "2026-10-27T16:00:00+00:00",
            },
            "home": {
                "selection": "PHI",
                "sao_public_bet_pct": 60,
                "sao_handle_bet_pct": 72,
                "sao_line": None,
                "sao_scraped_at": "2026-10-27T16:00:00+00:00",
            },
        }
    }
    merge_sao_into_game(game, sao)
    assert game["moneyline"]["away"]["public_bet_pct"] == 10
    assert game["moneyline"]["away"]["sao_public_bet_pct"] == 40
    assert game["moneyline"]["home"]["sao_handle_bet_pct"] == 72


def test_dk_unavailable_banner_returns_empty_without_retry(monkeypatch) -> None:
    html = "<html><body><p>Betting splits are temporarily unavailable</p></body></html>"
    calls: list[str] = []

    def fake(url, wait_selector, **kwargs):
        calls.append(url)
        assert kwargs.get("abort_text")
        return html

    monkeypatch.setattr("scrape_dk_splits.fetch_rendered_html", fake)
    assert dk_splits_unavailable(html) is True
    result = scrape_dk(day=date(2026, 10, 27), league="NBA", retries=3)
    assert result["unavailable"] is True
    assert result["games"] == []
    assert result["game_count"] == 0
    assert len(calls) == 1


def test_primary_unavailable_requires_sbd_and_sao_to_agree() -> None:
    def without_primary(game: dict, *, sao_away: int, sao_home: int, include_sao: bool = True) -> dict:
        copied = json.loads(json.dumps(game))
        for side in copied["moneyline"].values():
            side.pop("public_bet_pct", None)
            side.pop("handle_bet_pct", None)
        if include_sao:
            copied["moneyline"]["away"]["sao_public_bet_pct"] = sao_away
            copied["moneyline"]["away"]["sao_handle_bet_pct"] = sao_away + 30
            copied["moneyline"]["home"]["sao_public_bet_pct"] = sao_home
            copied["moneyline"]["home"]["sao_handle_bet_pct"] = sao_home - 30
        return copied

    base = _game()
    agreed = without_primary(base, sao_away=20, sao_home=80)
    slate = {"league": "NFL", "date": "2026-11-01", "games": [agreed, without_primary(base, sao_away=22, sao_home=78)]}
    play = process_game(
        agreed,
        market="moneyline",
        sources=sources_for_league("NFL"),
        primary_down=True,
        down_note=sharp.primary_down_note("NFL"),
    )
    assert play is not None
    assert play["qualified"] is True
    assert play["primary_status"] == "unavailable"
    assert play["public_side_source"] == "sbd"
    assert play["sources_voted"] == ["sbd", "sao"]
    assert play["primary_down_note"] == "DK splits down: SBD + SAO"
    assert play["sao_gap"] == 30

    flipped = without_primary(base, sao_away=80, sao_home=20)
    flipped["moneyline"]["away"]["sao_handle_bet_pct"] = 50
    flipped["moneyline"]["home"]["sao_handle_bet_pct"] = 50
    assert (
        process_game(
            flipped,
            market="moneyline",
            sources=("sbd", "sao"),
            primary_down=True,
        )
        is None
    )
    only_sbd = without_primary(base, sao_away=20, sao_home=80, include_sao=False)
    assert (
        process_game(
            only_sbd,
            market="moneyline",
            sources=("sbd", "sao"),
            primary_down=True,
        )
        is None
    )
    assert sharp._slate_primary_unavailable(slate["games"]) is True


def test_alignment_uses_sbd_and_sao_when_primary_is_empty() -> None:
    slate_day = date(2026, 11, 1)
    game = {
        "date": "2026-11-01",
        "matchup": "DAL @ PHI",
        "moneyline": {
            "away": {"sbd_handle_bet_pct": 50, "sao_handle_bet_pct": 40},
            "home": {"sbd_handle_bet_pct": 50, "sao_handle_bet_pct": 60},
        },
    }
    payload = {
        "league": "NFL",
        "date": "2026-11-01",
        "games": [game],
        "sources": {
            "draftkings": {"native_dates": [], "game_count": 0},
            "sportsbettingdime": {"native_dates": ["2026-11-01"], "game_count": 1},
            "scoresandodds": {"native_dates": ["2026-11-01"], "game_count": 1},
        },
    }
    result = evaluate_payload(payload, slate_day=slate_day)
    assert result.aligned is True
    assert result.overlap_count == 1

    payload["games"] = [
        {
            "date": "2026-11-01",
            "moneyline": {"away": {"sbd_handle_bet_pct": 50}, "home": {"sbd_handle_bet_pct": 50}},
        }
    ]
    assert evaluate_payload(payload, slate_day=slate_day).aligned is False


def test_composite_average_and_sum() -> None:
    gaps = SourceGaps(primary=30.0, vsin=None, sbd=30.0, sao=30.0)
    assert compute_composite(gaps, ("primary", "sbd"), mode="sum") == 52.5
    assert compute_composite(gaps, ("primary", "sbd"), mode="average") == 30.0
    assert compute_composite(gaps, ("primary", "sbd", "sao"), mode="average") == 30.0
    assert COMPOSITE_MODE == "average"


def test_sao_votes_false_and_sum_match_saved_output(monkeypatch) -> None:
    monkeypatch.setattr(sharp, "SAO_VOTES", False)
    monkeypatch.setattr(sharp, "COMPOSITE_MODE", "sum")
    assert sources_for_league("NFL") == ("primary", "sbd")
    play = process_game(_game(), market="moneyline", sources=sources_for_league("NFL"))
    assert play is not None
    for key, expected in REGRESSION.items():
        assert play[key] == expected
    assert "sao" not in play["agreeing_sources"]
