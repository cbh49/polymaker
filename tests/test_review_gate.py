"""Review-gate keys match the sharp trade review, and only those plays are kept."""

from __future__ import annotations

from polymaker.trading.review_gate import (
    classify_play,
    filter_sharp_plays,
    signal_keys,
    split_slate,
)
from polymaker.trading.sharp import SharpPlay

SYSTEMS = [
    {"key": "sbd_gap:15_to_25"},
    {"key": "line_move_abs:0_to_1"},
    {"key": "pair:league+market:NCAAF|spread"},
    {"key": "favorite_vs_dog:dog"},
]


def _play(**extra: object) -> dict:
    base = {
        "league": "NCAAF",
        "matchup": "UNC @ TCU",
        "event_id": "unc-tcu",
        "date": "2026-08-29",
        "market": "spread",
        "side": "UNC",
        "home_away": "away",
        "tier": "A",
        "qualified": True,
        "rlm_confirmed": True,
        "n_sources_agreeing": 2,
        "agreeing_sources": ["primary", "sbd"],
        "composite_gap": 20,
        "primary_gap": 20,
        "sbd_gap": 20,
        "public_bet_pct": 30,
        "handle_bet_pct": 55,
        "line_move": -0.5,
        "live": 3.5,
        "play_line": 3.5,
        "play_odds": -110,
        "line_moved_toward": "away",
    }
    base.update(extra)
    return base


def test_sbd_gap_15_to_25_is_a_signal_key() -> None:
    keys = signal_keys(_play())
    assert "sbd_gap:15_to_25" in keys
    verdict = classify_play(_play(), SYSTEMS, league="NCAAF")
    assert verdict["decision"] == "traded"
    assert "sbd_gap:15_to_25" in verdict["matched_systems"]


def test_large_line_move_still_trades_when_another_system_matches() -> None:
    play = _play(line_move=-2.5, reject_reasons=["line_move"])
    verdict = classify_play(play, SYSTEMS, league="NCAAF")
    assert "line_move_abs:0_to_1" not in signal_keys(play)
    assert verdict["decision"] == "traded"
    assert "sbd_gap:15_to_25" in verdict["matched_systems"]


def test_play_outside_every_system_is_dropped() -> None:
    play = _play(
        sbd_gap=40,
        line_move=-2.5,
        live=-3.5,
        play_line=-3.5,
        home_away="home",
        composite_gap=40,
        primary_gap=40,
        public_bet_pct=70,
        handle_bet_pct=80,
    )
    verdict = classify_play(play, [{"key": "sbd_gap:15_to_25"}], league="NCAAF")
    assert verdict["decision"] == "dropped"
    assert verdict["drop_reasons"] == ["review_gate"]


def test_finder_reject_is_logged_as_dropped() -> None:
    play = _play(qualified=False, tier=None, reject_reasons=["rlm", "line_move"])
    verdict = classify_play(play, SYSTEMS, league="NCAAF")
    assert verdict["decision"] == "dropped"
    assert verdict["drop_reasons"] == ["rlm"]


def test_cheap_moneyline_is_judged_as_the_spread() -> None:
    play = _play(
        market="moneyline",
        live=109,
        play_line=109,
        play_odds=109,
        trade_market="spread",
        trade_line=3.5,
        trade_odds=-110,
        trade_home_away="away",
        sbd_gap=40,
        line_move=20,
    )
    keys = signal_keys(play, league="NCAAF")
    assert "pair:league+market:NCAAF|spread" in keys
    assert "market:spread" in keys
    verdict = classify_play(play, SYSTEMS, league="NCAAF")
    assert verdict["decision"] == "traded"
    assert verdict["trade_data"]["market"] == "spread"
    assert verdict["trade_data"]["signal_market"] == "moneyline"


def test_skipped_moneyline_is_dropped() -> None:
    play = _play(market="moneyline", trade_market="skip", live=250, play_odds=250)
    verdict = classify_play(play, SYSTEMS, league="NCAAF")
    assert verdict["decision"] == "dropped"
    assert verdict["drop_reasons"] == ["trade_market_skip"]


def test_split_slate_keeps_only_tradable_plays() -> None:
    output = {
        "league": "NCAAF",
        "date": "2026-08-29",
        "plays": [
            _play(),
            _play(event_id="other", sbd_gap=40, line_move=-3, live=-6, play_line=-6, home_away="home"),
        ],
    }
    tradable, rows = split_slate(output, [{"key": "sbd_gap:15_to_25"}], logged_at=1)
    assert len(tradable["plays"]) == 1
    assert tradable["plays"][0]["event_id"] == "unc-tcu"
    assert [row["decision"] for row in rows] == ["traded", "dropped"]
    assert rows[0]["candidateKey"].startswith("NCAAF|2026-08-29|unc-tcu|spread|")


def test_filter_sharp_plays_uses_the_order() -> None:
    raw = _play(
        market="moneyline",
        trade_market="spread",
        trade_line=3.5,
        trade_odds=-110,
        trade_home_away="away",
        sbd_gap=40,
        line_move=15,
        live=120,
        play_odds=120,
    )
    play = SharpPlay(
        league="NCAAF",
        matchup="UNC @ TCU",
        side="UNC",
        market="spread",
        tier="A",
        home_away="away",
        game_time_utc=None,
        implied_fair_prob=0.45,
        rlm_confirmed=True,
        composite_gap=20,
        source_path="",
        raw=raw,
    )
    assert filter_sharp_plays([play], [{"key": "pair:league+market:NCAAF|spread"}]) == [play]
    assert filter_sharp_plays([play], [{"key": "sbd_gap:15_to_25"}]) == []
