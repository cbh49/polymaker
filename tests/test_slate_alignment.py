"""Same-day source alignment for sharp-money trading."""

from __future__ import annotations

import sys
from datetime import date
from pathlib import Path

_AGG = Path(__file__).resolve().parents[1] / "data-aggregation"
if str(_AGG) not in sys.path:
    sys.path.insert(0, str(_AGG))

from slate_alignment import (  # noqa: E402
    evaluate_payload,
    native_dates,
    overlap_games,
    same_slate,
)


def _ml_side(*, public: int | None = None, vsin: int | None = None, sbd: int | None = None) -> dict:
    row: dict = {"selection": "X"}
    if public is not None:
        row["public_bet_pct"] = public
        row["handle_bet_pct"] = public
    if vsin is not None:
        row["vsin_handle_bet_pct"] = vsin
        row["vsin_public_bet_pct"] = vsin
    if sbd is not None:
        row["sbd_handle_bet_pct"] = sbd
        row["sbd_public_bet_pct"] = sbd
    return row


def _mlb_game(matchup: str, day: str, **kwargs) -> dict:
    return {
        "matchup": matchup,
        "date": day,
        "game_time_utc": f"{day}T17:00:00.000Z",
        "moneyline": {
            "away": _ml_side(**kwargs),
            "home": _ml_side(**kwargs),
        },
        "spread": {"away": {"selection": "A"}, "home": {"selection": "H"}},
    }


def test_same_slate_rejects_next_day_rematch() -> None:
    dest = {"date": "2026-08-22", "game_time_utc": "2026-08-22T17:00:00.000Z"}
    nxt = {"date": "2026-08-23", "game_time_utc": "2026-08-23T17:00:00.000Z"}
    assert same_slate(dest, dest, date(2026, 8, 22)) is True
    assert same_slate(nxt, dest, date(2026, 8, 22)) is False


def test_native_dates_from_mixed_vsin() -> None:
    games = [
        {"date": "2026-08-22", "matchup": "TOR @ NYY"},
        {"date": "2026-08-23", "matchup": "BOS @ NYY"},
    ]
    assert native_dates(games) == ["2026-08-22", "2026-08-23"]


def test_aligned_when_all_mlb_sources_share_today() -> None:
    day = "2026-08-22"
    game = _mlb_game("TOR @ NYY", day, public=40, vsin=55, sbd=60)
    payload = {
        "league": "MLB",
        "date": day,
        "games": [game],
        "sources": {
            "playerprops": {"native_dates": [day], "game_count": 1},
            "vsin": {"native_dates": [day], "game_count": 1},
            "sportsbettingdime": {"native_dates": [day], "game_count": 1},
        },
    }
    result = evaluate_payload(payload, slate_day=date(2026, 8, 22))
    assert result.aligned is True
    assert result.overlap_count == 1


def test_unaligned_when_vsin_still_yesterday() -> None:
    day = "2026-08-23"
    game = _mlb_game("TOR @ NYY", day, public=40, vsin=55, sbd=60)
    payload = {
        "league": "MLB",
        "date": day,
        "games": [game],
        "sources": {
            "playerprops": {"native_dates": [day], "game_count": 1},
            "vsin": {"native_dates": ["2026-08-22"], "game_count": 4},
            "sportsbettingdime": {"native_dates": [day], "game_count": 1},
        },
    }
    result = evaluate_payload(payload, slate_day=date(2026, 8, 23))
    assert result.aligned is False
    assert "vsin" in result.reason


def test_unaligned_when_sbd_empty_for_today() -> None:
    day = "2026-08-23"
    game = _mlb_game("TOR @ NYY", day, public=40, vsin=55)
    payload = {
        "league": "MLB",
        "date": day,
        "games": [game],
        "sources": {
            "playerprops": {"native_dates": [day], "game_count": 1},
            "vsin": {"native_dates": [day], "game_count": 1},
            "sportsbettingdime": {"native_dates": [], "game_count": 0},
        },
    }
    result = evaluate_payload(payload, slate_day=date(2026, 8, 23))
    assert result.aligned is False
    assert "sbd" in result.reason


def test_overlap_requires_all_fields() -> None:
    day = date(2026, 8, 22)
    games = [
        _mlb_game("TOR @ NYY", "2026-08-22", public=40, vsin=55),
        _mlb_game("BOS @ BAL", "2026-08-22", public=40, vsin=55, sbd=60),
    ]
    assert len(overlap_games(games, day, ("primary", "vsin", "sbd"))) == 1


def test_wnba_needs_thespread_open() -> None:
    day = "2026-08-22"
    game = {
        "matchup": "NY @ LV",
        "date": day,
        "moneyline": {
            "away": {"public_bet_pct": 40, "handle_bet_pct": 55, "vsin_handle_bet_pct": 60},
            "home": {"public_bet_pct": 60, "handle_bet_pct": 45, "vsin_handle_bet_pct": 40},
        },
        "spread": {
            "away": {"selection": "NY", "open": 2.5, "live": 3.0},
            "home": {"selection": "LV", "open": -2.5, "live": -3.0},
        },
    }
    payload = {
        "league": "WNBA",
        "date": day,
        "games": [game],
        "sources": {
            "draftkings": {"native_dates": [day], "game_count": 1},
            "vsin": {"native_dates": [day], "game_count": 1},
            "thespread": {"native_dates": [day], "game_count": 1},
        },
    }
    result = evaluate_payload(payload, slate_day=date(2026, 8, 22))
    assert result.aligned is True


def _ncaaf_side(*, public: int, handle: int, vsin: int, sbd: int, extra: dict | None = None) -> dict:
    row = {
        "public_bet_pct": public,
        "handle_bet_pct": handle,
        "vsin_public_bet_pct": public,
        "vsin_handle_bet_pct": vsin,
        "sbd_public_bet_pct": public,
        "sbd_handle_bet_pct": sbd,
    }
    if extra:
        row.update(extra)
    return row


def test_ncaaf_weekend_window_aligns() -> None:
    """Thursday slate includes Saturday CFB games (window = 6 days)."""
    game = {
        "matchup": "HAW @ STAN",
        "date": "2026-08-29",
        "moneyline": {
            "away": _ncaaf_side(public=40, handle=55, vsin=60, sbd=50),
            "home": _ncaaf_side(public=60, handle=45, vsin=40, sbd=50),
        },
    }
    payload = {
        "league": "NCAAF",
        "date": "2026-08-27",
        "games": [game],
        "sources": {
            "draftkings": {"native_dates": ["2026-08-29"], "game_count": 1},
            "vsin": {"native_dates": ["2026-08-29"], "game_count": 1},
            "sportsbettingdime": {"native_dates": ["2026-08-29"], "game_count": 1},
        },
    }
    result = evaluate_payload(payload, slate_day=date(2026, 8, 27))
    assert result.aligned is True
    assert result.overlap_count == 1


def test_ncaaf_spread_only_game_overlaps() -> None:
    """SJSU @ USC often has no moneyline; spread fields still count."""
    game = {
        "matchup": "SJSU @ USC",
        "date": "2026-08-29",
        "spread": {
            "away": _ncaaf_side(public=20, handle=33, vsin=30, sbd=25, extra={"open": 38.5}),
            "home": _ncaaf_side(public=80, handle=67, vsin=70, sbd=75, extra={"open": -38.5}),
        },
    }
    payload = {
        "league": "CFB",
        "date": "2026-08-27",
        "games": [game],
        "sources": {
            "draftkings": {"native_dates": ["2026-08-29"], "game_count": 1},
            "vsin": {"native_dates": ["2026-08-29"], "game_count": 1},
            "sportsbettingdime": {"native_dates": ["2026-08-29"], "game_count": 1},
        },
    }
    result = evaluate_payload(payload, slate_day=date(2026, 8, 27))
    assert result.aligned is True
    assert result.league == "NCAAF"
