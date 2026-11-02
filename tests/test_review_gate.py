"""Review-gate keys match the sharp trade review, and only those plays are kept."""

from __future__ import annotations

from polymaker.trading.review_gate import (
    classify_play,
    filter_sharp_plays,
    signal_keys,
    split_slate,
    system_key_set,
)
from polymaker.trading.sharp import SharpPlay

EDGE = {
    "key": "exchange_edge:ge_3",
    "units_per_bet": 0.2682,
    "decided": 167,
    "wins": 111,
    "losses": 56,
    "hit_rate": 0.6647,
    "wilson_lower": 0.5901,
}


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


def _with_edge(edge: float = 4.0, **extra: object) -> dict:
    play = _play(**extra)
    play["exchange_confirmation"] = {"exchange_edge_pct": edge}
    return play


def test_only_metric_slices_that_clear_the_bar_authorize_a_trade() -> None:
    systems = [
        EDGE,
        {
            "key": "exchange_edge:1_5_to_3",
            "units_per_bet": 0.1058,
            "decided": 60,
            "wins": 36,
            "losses": 24,
        },
        {
            "key": "pair:league+market:NCAAF|spread",
            "units_per_bet": 0.25,
            "decided": 80,
            "wins": 55,
            "losses": 25,
        },
        {
            "key": "pair:market+gap:total|15_to_25",
            "units_per_bet": 0.20,
            "decided": 90,
            "wins": 70,
            "losses": 20,
        },
        {"key": "sbd_gap:15_to_25", "units_per_bet": 0.75, "decided": 10, "wins": 9, "losses": 1},
        {"key": "line_move_abs:0_to_1", "units_per_bet": 0.13, "decided": 27, "wins": 16, "losses": 11},
        {"key": "favorite_vs_dog:dog"},
    ]
    assert system_key_set(systems) == {"exchange_edge:ge_3"}


def test_exchange_edge_at_least_3_is_a_signal_key() -> None:
    play = _with_edge()
    keys = signal_keys(play)
    assert "exchange_edge:ge_3" in keys
    assert "sbd_gap:15_to_25" in keys
    assert "pair:exchange_edge+line_move_abs:ge_3|0_to_1" in keys
    assert "pair:exchange_edge+sbd_gap:ge_3|15_to_25" in keys
    verdict = classify_play(play, [EDGE], league="NCAAF")
    assert verdict["decision"] == "traded"
    assert verdict["matched_systems"] == ["exchange_edge:ge_3"]


def test_old_slices_do_not_trade_without_a_3_point_edge() -> None:
    old = [
        {"key": "sbd_gap:15_to_25", "units_per_bet": 0.75, "decided": 10, "wins": 9, "losses": 1},
        {"key": "line_move_abs:0_to_1", "units_per_bet": 0.13, "decided": 27, "wins": 16, "losses": 11},
        {
            "key": "pair:league+market:NCAAF|spread",
            "units_per_bet": 0.25,
            "decided": 80,
            "wins": 55,
            "losses": 25,
        },
        EDGE,
    ]
    play = _play(line_move=-3.0, reject_reasons=["line_move"])
    assert "line_move_abs:0_to_1" not in signal_keys(play)
    assert "sbd_gap:15_to_25" in signal_keys(play)
    verdict = classify_play(play, old, league="NCAAF")
    assert verdict["decision"] == "dropped"
    assert verdict["drop_reasons"] == ["review_gate"]


def test_edge_under_3_is_dropped() -> None:
    verdict = classify_play(_with_edge(2.0), [EDGE], league="NCAAF")
    assert "exchange_edge:1_5_to_3" in signal_keys(_with_edge(2.0))
    assert verdict["decision"] == "dropped"
    assert verdict["drop_reasons"] == ["review_gate"]


def test_large_line_move_still_trades_when_the_edge_clears() -> None:
    play = _with_edge(4.2, line_move=-3.0, reject_reasons=["line_move"])
    assert "line_move_abs:gt_2_5" in signal_keys(play)
    verdict = classify_play(play, [EDGE], league="NCAAF")
    assert verdict["decision"] == "traded"
    assert verdict["matched_systems"] == ["exchange_edge:ge_3"]


def test_finder_reject_is_logged_as_dropped() -> None:
    play = _with_edge(qualified=False, tier=None, reject_reasons=["rlm", "line_move"])
    verdict = classify_play(play, [EDGE], league="NCAAF")
    assert verdict["decision"] == "dropped"
    assert verdict["drop_reasons"] == ["rlm"]


def test_cheap_moneyline_is_judged_as_the_spread() -> None:
    play = _with_edge(
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
    assert "exchange_edge:ge_3" in keys
    verdict = classify_play(play, [EDGE], league="NCAAF")
    assert verdict["decision"] == "traded"
    assert verdict["matched_systems"] == ["exchange_edge:ge_3"]
    assert verdict["trade_data"]["market"] == "spread"
    assert verdict["trade_data"]["signal_market"] == "moneyline"


def test_skipped_moneyline_is_dropped() -> None:
    play = _with_edge(market="moneyline", trade_market="skip", live=250, play_odds=250)
    verdict = classify_play(play, [EDGE], league="NCAAF")
    assert verdict["decision"] == "dropped"
    assert verdict["drop_reasons"] == ["trade_market_skip"]


def test_split_slate_keeps_only_tradable_plays() -> None:
    output = {
        "league": "NCAAF",
        "date": "2026-08-29",
        "plays": [
            _with_edge(),
            _play(event_id="other", sbd_gap=40, line_move=-3, live=-6, play_line=-6, home_away="home"),
        ],
    }
    tradable, rows = split_slate(output, [EDGE], logged_at=1)
    assert len(tradable["plays"]) == 1
    assert tradable["plays"][0]["event_id"] == "unc-tcu"
    assert [row["decision"] for row in rows] == ["traded", "dropped"]
    assert rows[0]["candidateKey"].startswith("NCAAF|2026-08-29|unc-tcu|spread|")


def test_filter_sharp_plays_uses_the_order() -> None:
    raw = _with_edge(
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
    league_only = {
        "key": "pair:league+market:NCAAF|spread",
        "units_per_bet": 0.25,
        "decided": 80,
        "wins": 55,
        "losses": 25,
    }
    assert filter_sharp_plays([play], [league_only]) == []
    assert filter_sharp_plays([play], [EDGE]) == [play]
