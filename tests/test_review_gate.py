"""Review-gate keys match the sharp trade review, and only those plays are kept."""

from __future__ import annotations

from polymaker.trading.review_gate import (
    _fallback_avoid,
    _fallback_systems,
    candidate_for_skipped_order,
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
MILD_EDGE = {
    "key": "exchange_edge:1_5_to_3",
    "units_per_bet": 0.074,
    "decided": 65,
    "wins": 38,
    "losses": 27,
}
TOTAL_GAP = {
    "key": "pair:market+gap:total|15_to_25",
    "units_per_bet": 0.1626,
    "decided": 99,
    "wins": 60,
    "losses": 39,
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
        TOTAL_GAP,
        {
            "key": "pair:market+gap:spread|15_to_25",
            "units_per_bet": -0.16,
            "decided": 133,
            "wins": 57,
            "losses": 76,
        },
        {
            "key": "tier:A",
            "units_per_bet": -0.037,
            "decided": 382,
            "wins": 189,
            "losses": 193,
        },
        {
            "key": "exchange_edge:lt_1_5",
            "units_per_bet": -0.128,
            "decided": 653,
            "wins": 291,
            "losses": 362,
        },
        {"key": "sbd_gap:15_to_25", "units_per_bet": 0.75, "decided": 10, "wins": 9, "losses": 1},
        {"key": "line_move_abs:0_to_1", "units_per_bet": 0.13, "decided": 27, "wins": 16, "losses": 11},
        {"key": "favorite_vs_dog:dog"},
    ]
    assert system_key_set(systems) == {
        "exchange_edge:ge_3",
        "exchange_edge:1_5_to_3",
        "pair:market+gap:total|15_to_25",
    }


def test_exchange_edge_at_least_3_is_a_signal_key() -> None:
    play = _with_edge()
    keys = signal_keys(play)
    assert "exchange_edge:ge_3" in keys
    assert "sbd_gap:15_to_25" in keys
    assert "pair:exchange_edge+line_move_abs:ge_3|0_to_1" in keys
    assert "pair:exchange_edge+sbd_gap:ge_3|15_to_25" in keys
    nfl = _with_edge(league="NFL")
    verdict = classify_play(nfl, [EDGE], league="NFL")
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
    play = _play(league="NFL", line_move=-3.0, reject_reasons=["line_move"])
    assert "line_move_abs:0_to_1" not in signal_keys(play)
    assert "sbd_gap:15_to_25" in signal_keys(play)
    verdict = classify_play(play, old, league="NFL")
    assert verdict["decision"] == "dropped"
    assert verdict["drop_reasons"] == ["review_gate"]


def test_edge_under_3_is_dropped() -> None:
    play = _with_edge(2.0, league="NFL")
    verdict = classify_play(play, [EDGE], league="NFL")
    assert "exchange_edge:1_5_to_3" in signal_keys(play)
    assert verdict["decision"] == "dropped"
    assert verdict["drop_reasons"] == ["review_gate"]


def test_mild_edge_and_total_gap_trade_when_they_clear() -> None:
    mild = _with_edge(2.0, league="NFL", market="total", home_away="over", side="Over", composite_gap=20)
    verdict = classify_play(mild, [EDGE, MILD_EDGE, TOTAL_GAP], league="NFL")
    assert verdict["decision"] == "traded"
    assert "exchange_edge:1_5_to_3" in verdict["matched_systems"]
    assert "pair:market+gap:total|15_to_25" in verdict["matched_systems"]

    weak = _with_edge(0.4, league="NFL", composite_gap=20)
    dropped = classify_play(
        weak,
        [
            EDGE,
            MILD_EDGE,
            TOTAL_GAP,
            {"key": "tier:A", "units_per_bet": -0.04, "decided": 382, "wins": 189, "losses": 193},
            {
                "key": "pair:market+gap:spread|15_to_25",
                "units_per_bet": -0.16,
                "decided": 133,
                "wins": 57,
                "losses": 76,
            },
        ],
        league="NFL",
    )
    assert "tier:A" in signal_keys(weak)
    assert "pair:market+gap:spread|15_to_25" in signal_keys(weak)
    assert "exchange_edge:lt_1_5" in signal_keys(weak)
    assert dropped["decision"] == "dropped"
    assert dropped["drop_reasons"] == ["review_gate"]


def test_skipped_order_is_logged_as_a_pass() -> None:
    play = _with_edge()
    row = candidate_for_skipped_order(
        play,
        [EDGE],
        league="NCAAF",
        slate_date="2026-08-29",
        detail="no open spread",
        logged_at=1,
    )
    assert row["decision"] == "dropped"
    assert row["dropReasons"] == ["no open spread"]
    assert row["matchedSystems"] == ["league_override:NCAAF:spread"]
    assert row["candidateKey"].startswith("NCAAF|2026-08-29|unc-tcu|spread|")
    assert row["tradeData"]["exchange_confirmation"]["exchange_edge_pct"] == 4.0


def test_large_line_move_still_trades_when_the_edge_clears() -> None:
    play = _with_edge(4.2, league="NFL", line_move=-3.0, reject_reasons=["line_move"])
    assert "line_move_abs:gt_2_5" in signal_keys(play)
    verdict = classify_play(play, [EDGE], league="NFL")
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
    assert verdict["matched_systems"] == ["league_override:NCAAF:spread"]
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
            _play(
                event_id="other",
                market="moneyline",
                side="TCU",
                home_away="home",
                live=150,
                play_line=150,
                play_odds=150,
            ),
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
        league="NFL",
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


def _sharp(raw: dict, *, league: str, market: str | None = None) -> SharpPlay:
    return SharpPlay(
        league=league,
        matchup=str(raw.get("matchup") or ""),
        side=str(raw.get("side") or ""),
        market=market or str(raw.get("market") or ""),
        tier=str(raw.get("tier") or "A"),
        home_away=str(raw.get("home_away") or "") or None,
        game_time_utc=None,
        implied_fair_prob=None,
        rlm_confirmed=bool(raw.get("rlm_confirmed")),
        composite_gap=float(raw["composite_gap"]) if raw.get("composite_gap") is not None else None,
        source_path="",
        raw=raw,
    )


def test_ncaaf_blank_edge_spread_bypasses_the_review_gate() -> None:
    play = _play(exchange_confirmation={"exchange_edge_pct": ""}, composite_gap=20)
    keys = signal_keys(play)
    assert not any(key.startswith("exchange_edge:") for key in keys)
    verdict = classify_play(play, [], league="NCAAF")
    assert verdict["decision"] == "traded"
    assert verdict["matched_systems"] == ["league_override:NCAAF:spread"]
    tradable, rows = split_slate(
        {"league": "NCAAF", "date": "2026-08-29", "plays": [play]},
        [],
        logged_at=1,
    )
    assert tradable["plays"] == [play]
    assert rows[0]["decision"] == "traded"
    assert rows[0]["matchedSystems"] == ["league_override:NCAAF:spread"]
    sharp = _sharp(play, league="NCAAF")
    assert filter_sharp_plays([sharp], []) == [sharp]


def test_ncaaf_total_and_moneyline_do_not_fall_through_to_edge() -> None:
    blocked_total = _with_edge(4.0, market="total", home_away="over", side="Over", composite_gap=30)
    blocked = classify_play(blocked_total, [EDGE], league="NCAAF")
    assert "exchange_edge:ge_3" in signal_keys(blocked_total)
    assert blocked["decision"] == "dropped"
    assert blocked["drop_reasons"] == ["review_gate"]

    opened = _with_edge(0.4, market="total", home_away="over", side="Over", composite_gap=30)
    verdict = classify_play(opened, [], league="NCAAF")
    assert verdict["decision"] == "traded"
    assert verdict["matched_systems"] == ["league_override:NCAAF:total"]

    gap_band = _with_edge(0.4, market="total", home_away="over", side="Over", composite_gap=20)
    held = classify_play(gap_band, [TOTAL_GAP], league="NCAAF")
    assert "pair:market+gap:total|15_to_25" in signal_keys(gap_band)
    assert held["decision"] == "dropped"
    assert held["drop_reasons"] == ["review_gate"]

    moneyline = _with_edge(4.0, market="moneyline", live=150, play_line=150, play_odds=150)
    ml = classify_play(moneyline, [EDGE], league="NCAAF")
    assert "exchange_edge:ge_3" in signal_keys(moneyline)
    assert ml["decision"] == "dropped"
    assert ml["drop_reasons"] == ["review_gate"]


def test_nba_spread_under_1_5_edge_is_added_without_touching_totals() -> None:
    spread = _with_edge(1.0, league="NBA")
    verdict = classify_play(spread, [EDGE], league="NBA")
    assert verdict["decision"] == "traded"
    assert verdict["matched_systems"] == ["league_override:NBA:spread_lt_1_5"]
    assert filter_sharp_plays([_sharp(spread, league="NBA")], [EDGE]) == [_sharp(spread, league="NBA")]

    total = _with_edge(4.0, league="NBA", market="total", home_away="over", side="Over", composite_gap=30)
    kept = classify_play(total, [EDGE], league="NBA")
    assert kept["decision"] == "traded"
    assert kept["matched_systems"] == ["exchange_edge:ge_3"]

    quiet_total = _with_edge(
        0.4, league="NBA", market="total", home_away="over", side="Over", composite_gap=30
    )
    dropped = classify_play(quiet_total, [EDGE], league="NBA")
    assert dropped["decision"] == "dropped"
    assert dropped["drop_reasons"] == ["review_gate"]

    hot_spread = _with_edge(4.0, league="NBA")
    hot = classify_play(hot_spread, [EDGE], league="NBA")
    assert hot["decision"] == "traded"
    assert hot["matched_systems"] == ["exchange_edge:ge_3"]
    assert "league_override:NBA:spread_lt_1_5" not in hot["matched_systems"]


def test_nfl_edge_gate_does_not_inherit_the_nba_spread_override() -> None:
    hot = _with_edge(4.0, league="NFL")
    verdict = classify_play(hot, [EDGE], league="NFL")
    assert verdict["decision"] == "traded"
    assert verdict["matched_systems"] == ["exchange_edge:ge_3"]

    mild = _with_edge(1.0, league="NFL")
    dropped = classify_play(mild, [EDGE], league="NFL")
    assert "exchange_edge:lt_1_5" in signal_keys(mild)
    assert dropped["decision"] == "dropped"
    assert dropped["drop_reasons"] == ["review_gate"]


def test_qualified_ncaab_bypasses_the_review_gate() -> None:
    play = _play(league="NCAAB", event_id="duke-unc")
    assert not any(key.startswith("exchange_edge:") for key in signal_keys(play, league="NCAAB"))
    verdict = classify_play(play, [], league="NCAAB")
    assert verdict["decision"] == "traded"
    assert verdict["matched_systems"] == ["league_override:NCAAB"]
    tradable, rows = split_slate(
        {"league": "NCAAB", "date": "2026-11-03", "plays": [play]},
        [],
        logged_at=1,
    )
    assert tradable["plays"] == [play]
    assert rows[0]["decision"] == "traded"
    assert rows[0]["matchedSystems"] == ["league_override:NCAAB"]
    sharp = _sharp(play, league="NCAAB")
    assert filter_sharp_plays([sharp], []) == [sharp]

    rejected = _play(league="NCAAB", qualified=False, reject_reasons=["rlm"])
    assert classify_play(rejected, [], league="NCAAB")["drop_reasons"] == ["rlm"]
    skipped = _play(league="NCAAB", market="moneyline", trade_market="skip", live=250, play_odds=250)
    assert classify_play(skipped, [], league="NCAAB")["drop_reasons"] == ["trade_market_skip"]


def test_avoid_key_drops_a_play_the_positive_list_would_trade() -> None:
    play = _with_edge(league="NFL")
    avoid = [
        {
            "key": "sbd_gap:15_to_25",
            "units_per_bet": -0.12,
            "decided": 80,
            "wins": 30,
            "losses": 50,
        }
    ]
    verdict = classify_play(play, [EDGE], league="NFL", avoid=avoid)
    assert verdict["decision"] == "dropped"
    assert verdict["drop_reasons"] == ["avoid_system"]
    assert verdict["matched_systems"] == ["exchange_edge:ge_3"]
    assert filter_sharp_plays([_sharp(play, league="NFL")], [EDGE], avoid) == []
    mild = {
        "key": "sbd_gap:15_to_25",
        "units_per_bet": -0.02,
        "decided": 80,
        "wins": 35,
        "losses": 45,
    }
    kept = classify_play(play, [EDGE], league="NFL", avoid=[mild])
    assert kept["decision"] == "traded"


def test_league_metric_pair_matches_only_when_both_are_present() -> None:
    nfl = _with_edge(league="NFL", composite_gap=42)
    nba = _with_edge(league="NBA", composite_gap=42)
    assert "pair:league+composite_gap:NFL|ge_40" in signal_keys(nfl, league="NFL")
    assert "pair:league+composite_gap:NFL|ge_40" not in signal_keys(nba, league="NBA")
    assert "pair:league+composite_gap:NBA|ge_40" in signal_keys(nba, league="NBA")
    system = {
        "key": "pair:league+composite_gap:NFL|ge_40",
        "units_per_bet": 0.2,
        "decided": 50,
        "wins": 35,
        "losses": 15,
        "wilson_lower": 0.55,
    }
    assert classify_play(nfl, [system], league="NFL")["decision"] == "traded"
    assert classify_play(nba, [system], league="NBA")["decision"] == "dropped"


def test_october_fallback_lists_clear_the_bars() -> None:
    trade = {row["key"] for row in _fallback_systems()}
    avoid = {row["key"] for row in _fallback_avoid()}
    assert trade == {
        "sbd_gap:15_to_25",
        "pair:league+sbd_gap:NBA|lt_15",
        "pair:league+rlm_source_used:NBA|eva",
        "pair:league+public_pct:NCAAF|lt_30",
    }
    assert avoid == {
        "sao_gap:lt_15",
        "ml_spread_divergence:false",
        "sbd_gap:25_to_40",
        "model_confidence:78_to_85",
        "pair:league+public_pct:NCAAF|30_to_45",
        "pair:market+gap:spread|25_to_40",
        "pair:league+ml_spread_divergence:NCAAF|false",
    }


def test_league_override_ignores_an_avoid_key() -> None:
    play = _play(league="NCAAB", event_id="duke-unc")
    avoid = [
        {
            "key": "composite_gap:15_to_25",
            "units_per_bet": -0.2,
            "decided": 40,
            "wins": 10,
            "losses": 30,
        }
    ]
    verdict = classify_play(play, [], league="NCAAB", avoid=avoid)
    assert verdict["decision"] == "traded"
    assert verdict["matched_systems"] == ["league_override:NCAAB"]
