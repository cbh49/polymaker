"""End-to-end slate walk on the same-strike + different-strike example blobs."""

from __future__ import annotations

import pytest

from ev_trading.fair_value.config import FairValueConfig
from ev_trading.fair_value.models import FittedDistribution
from ev_trading.fair_value.pipeline import _allow_ou_side, process_slate
from ev_trading.fair_value.report import is_td_yesno
from ev_trading.fair_value.tradable_pricer import (
    kalshi_no_ask,
    kalshi_taker_fee,
    kalshi_yes_ask,
    polymarket_side_ask,
)

SPREAD = {
    "consensus_line": -3.5,
    "books": {
        "betr": {
            "away": {"line": 3.5, "odds": -106, "implied_prob": 0.5146},
            "home": {"line": -3.5, "odds": -119, "implied_prob": 0.5434},
        },
        "caesars": {
            "away": {"line": 3.5, "odds": -108, "implied_prob": 0.5192},
            "home": {"line": -3.5, "odds": -111, "implied_prob": 0.5261},
        },
        "draftkings": {
            "away": {"line": 3.5, "odds": -108, "implied_prob": 0.5192},
            "home": {"line": -3.5, "odds": -112, "implied_prob": 0.5283},
        },
        "fanduel": {
            "away": {"line": 3.5, "odds": -105, "implied_prob": 0.5122},
            "home": {"line": -3.5, "odds": -115, "implied_prob": 0.5349},
        },
        "circasports": {
            "away": {"line": 4.0, "odds": -110, "implied_prob": 0.5238},
            "home": {"line": -4.0, "odds": -110, "implied_prob": 0.5238},
        },
    },
    "kalshi": {
        "ticker": "KXNFLSPREAD-26SEP13TBCIN-CIN4",
        "yes_bid": 0.50,
        "yes_ask": 0.51,
        "no_bid": 0.49,
        "no_ask": 0.50,
        "implied_prob": 0.505,
        "volume": 47169.63,
        "line": -3.5,
    },
    "polymarket": {
        "away": {
            "line": -102,
            "implied_prob": 0.505,
            "bid": 0.50,
            "ask": 0.51,
            "liquidity": 88010.69,
            "volume_24hr": 76.63,
            "market_id": "3340062",
        },
        "home": {
            "line": 102,
            "implied_prob": 0.495,
            "bid": 0.49,
            "ask": 0.50,
            "liquidity": 88010.69,
            "volume_24hr": 76.63,
            "market_id": "3340062",
        },
    },
}

PASSING_YARDS = {
    "player": "Jaxson Dart",
    "playerID": "18574",
    "type": "passing_yards",
    "consensus_line": 213.0,
    "books": {
        "betr": {
            "line": 213.5,
            "over_odds": -118,
            "under_odds": -118,
            "over_implied_prob": 0.5413,
            "under_implied_prob": 0.5413,
        },
        "betrivers": {
            "line": 218.5,
            "over_odds": -115,
            "under_odds": -114,
            "over_implied_prob": 0.5349,
            "under_implied_prob": 0.5327,
        },
        "caesars": {
            "line": 212.5,
            "over_odds": -113,
            "under_odds": -115,
            "over_implied_prob": 0.5305,
            "under_implied_prob": 0.5349,
        },
        "draftkings": {
            "line": 211.5,
            "over_odds": -111,
            "under_odds": -113,
            "over_implied_prob": 0.5261,
            "under_implied_prob": 0.5305,
        },
        "fanduel": {
            "line": 213.5,
            "over_odds": -114,
            "under_odds": -114,
            "over_implied_prob": 0.5327,
            "under_implied_prob": 0.5327,
        },
        "thescore": {
            "line": 214.5,
            "over_odds": -115,
            "under_odds": -115,
            "over_implied_prob": 0.5349,
            "under_implied_prob": 0.5349,
        },
    },
    "kalshi": {
        "ticker": "KXNFLPASSYDS-26SEP13DALNYG-NYGJDART6-225",
        "yes_sub_title": "Jaxson Dart: 225+",
        "yes_bid": 0.41,
        "yes_ask": 0.42,
        "no_bid": 0.58,
        "no_ask": 0.59,
        "implied_prob": 0.415,
        "volume": 163.25,
        "line": 224.5,
        "line_delta": 11.5,
    },
    "polymarket": {
        "line": 199.5,
        "line_delta": -13.5,
        "over": 0.32,
        "under": 0.68,
        "over_bid": 0.30,
        "over_ask": 0.34,
        "under_bid": 0.66,
        "under_ask": 0.70,
        "implied_prob": 0.32,
        "volume": 0.0,
        "liquidity": 129.1,
        "market_id": "4080561",
    },
}


def _slate() -> dict:
    return {
        "games": [
            {
                "matchup": "NYG @ DAL",
                "markets": {"spread": SPREAD},
                "player_props": [PASSING_YARDS],
            }
        ]
    }


def test_kalshi_fee_matches_published_100_contract_table() -> None:
    assert kalshi_taker_fee(0.50, contracts=100) == pytest.approx(1.75)
    assert kalshi_taker_fee(0.10, contracts=100) == pytest.approx(0.63)
    assert kalshi_taker_fee(0.40, contracts=100) == pytest.approx(1.68)
    assert kalshi_taker_fee(0.90, contracts=100) == pytest.approx(0.63)


def test_kalshi_yes_ask_reads_dollar_fields() -> None:
    assert kalshi_yes_ask({"yes_ask_dollars": "0.5700"}) == pytest.approx(0.57)
    assert kalshi_yes_ask({"yes_ask": 0.51}) == pytest.approx(0.51)
    assert kalshi_yes_ask({"yes_ask": 57}) == pytest.approx(0.57)
    assert kalshi_yes_ask({"yes_ask_dollars": "0.0000"}) is None
    assert kalshi_no_ask({"no_ask_dollars": "0.8700"}) == pytest.approx(0.87)
    assert kalshi_no_ask({"yes_bid_dollars": "0.1300"}) == pytest.approx(0.87)


def test_pipeline_separates_tradable_and_informational() -> None:
    cfg = FairValueConfig(min_edge_pct=0.0)
    report = process_slate(_slate(), cfg)
    payload = report.to_dict()
    assert "tradable" in payload
    assert "tradable_low_liquidity" in payload
    assert "tradable_td" in payload
    assert "tradable_td_low_liquidity" in payload
    assert "informational" in payload
    assert "rejected" in payload
    tradable = report.tradable + report.tradable_low_liquidity
    assert all(not is_td_yesno(row.stat) for row in tradable)
    assert tradable, "expected at least one Kalshi/Polymarket row"
    venues = {row.venue for row in tradable}
    assert venues <= {"kalshi", "polymarket"}
    assert all(row.book for row in report.informational)
    assert all(row.actionable is False for row in report.informational)
    names = {row.market for row in tradable}
    assert any("spread" in n for n in names)
    assert any("Dart" in n or "passing_yards" in n for n in names)
    for row in tradable:
        assert 0.0 <= row.confidence <= 1.0
        assert row.fair_prob == pytest.approx(row.fair_prob)
        assert "raw_edge" in row.to_dict()
        assert "fee_adjusted_edge" in row.to_dict()
        assert "expected_value_per_contract" in row.to_dict()


def test_tradable_ranked_by_ev_times_confidence() -> None:
    cfg = FairValueConfig(min_edge_pct=0.0)
    report = process_slate(_slate(), cfg)
    scores = [row.rank_score for row in report.tradable]
    assert scores == sorted(scores, reverse=True)


COUSINS_RUSH = {
    "player": "Kirk Cousins",
    "type": "rushing_yards",
    "consensus_line": 0.5,
    "books": {
        "betr": {
            "line": 0.5,
            "over_odds": 103,
            "under_odds": -143,
            "over_implied_prob": 0.4926,
            "under_implied_prob": 0.5885,
        },
        "circasports": {
            "line": 0.5,
            "over_odds": 125,
            "under_odds": -145,
            "over_implied_prob": 0.4444,
            "under_implied_prob": 0.5918,
        },
        "draftkings": {
            "line": 0.5,
            "over_odds": 115,
            "under_odds": -144,
            "over_implied_prob": 0.4651,
            "under_implied_prob": 0.5902,
        },
        "fanduel": {
            "line": 0.5,
            "over_odds": 120,
            "under_odds": -156,
            "over_implied_prob": 0.4545,
            "under_implied_prob": 0.6094,
        },
        "hardrock": {
            "line": 0.5,
            "over_odds": 120,
            "under_odds": -155,
            "over_implied_prob": 0.4545,
            "under_implied_prob": 0.6078,
        },
        "thescore": {
            "line": 0.5,
            "over_odds": 115,
            "under_odds": -150,
            "over_implied_prob": 0.4651,
            "under_implied_prob": 0.6,
        },
    },
    "kalshi": {
        "ticker": "KXNFLRSHYDS-26SEP13MIALV-LVKCOUSINS8-10",
        "yes_sub_title": "Kirk Cousins: 10+",
        "yes_bid": 0.13,
        "yes_ask": 0.19,
        "volume": 87.0,
        "line": 9.5,
        "line_delta": 9.0,
    },
}


def _cousins_slate(*, yes_bid: float = 0.13, yes_ask: float = 0.19) -> dict:
    blob = dict(COUSINS_RUSH)
    blob["kalshi"] = {**COUSINS_RUSH["kalshi"], "yes_bid": yes_bid, "yes_ask": yes_ask}
    return {
        "games": [
            {
                "matchup": "MIA @ LV",
                "markets": {},
                "player_props": [blob],
            }
        ]
    }


def test_longshot_rush_over_is_not_a_plus_ev_over() -> None:
    """Kalshi 10+ vs books at 0.5 is a worse over, not a better one."""
    cfg = FairValueConfig(min_edge_pct=0.0)
    report = process_slate(_cousins_slate(), cfg)
    rows = [
        r
        for r in report.tradable + report.tradable_low_liquidity
        if r.player == "Kirk Cousins" and r.venue == "kalshi"
    ]
    assert all(r.side != "over" for r in rows)
    rejected = [r for r in report.rejected if r.player == "Kirk Cousins"]
    # Fallback σ walking 0.5 → 9.5 is a hard veto, not a ranked under.
    assert rejected
    assert all(r.veto_reason for r in rejected)
    assert all(
        r.veto_reason in {"fallback_sigma_extrapolation", "fit_quality_too_low"}
        for r in rejected
    )


def test_longshot_under_is_vetoed_even_when_no_is_cheap() -> None:
    cfg = FairValueConfig(min_edge_pct=1.0)
    cheap = process_slate(_cousins_slate(yes_bid=0.50, yes_ask=0.55), cfg)
    cheap_unders = [
        r
        for r in cheap.tradable + cheap.tradable_low_liquidity
        if r.player == "Kirk Cousins" and r.side == "under"
    ]
    assert cheap_unders == []
    rejected = [
        r
        for r in cheap.rejected
        if r.player == "Kirk Cousins" and r.venue == "kalshi"
    ]
    assert rejected
    assert all(r.is_extrapolation is True for r in rejected)


def test_allow_ou_side_blocks_longshot_over_keeps_nearby_alt() -> None:
    cfg = FairValueConfig()
    default_rush = FittedDistribution(
        mu=0.5,
        sigma=8.0,
        distribution_type="normal",
        r2=1.0,
        n_books=6,
        low_confidence=True,
        sigma_source="fallback",
    )
    assert _allow_ou_side(
        "over", market_line=9.5, fair_line=0.5, fit=default_rush, cfg=cfg
    ) is False
    assert _allow_ou_side(
        "under", market_line=9.5, fair_line=0.5, fit=default_rush, cfg=cfg
    ) is True

    default_pass = FittedDistribution(
        mu=213.0,
        sigma=62.0,
        distribution_type="normal",
        r2=0.8,
        n_books=6,
        low_confidence=True,
        sigma_source="fallback",
    )
    assert _allow_ou_side(
        "over", market_line=224.5, fair_line=213.0, fit=default_pass, cfg=cfg
    ) is True
    assert _allow_ou_side(
        "under", market_line=224.5, fair_line=213.0, fit=default_pass, cfg=cfg
    ) is True

    fitted = FittedDistribution(
        mu=0.5,
        sigma=4.0,
        distribution_type="normal",
        r2=0.99,
        n_books=6,
        low_confidence=False,
        sigma_source="fitted",
    )
    assert _allow_ou_side(
        "over", market_line=9.5, fair_line=0.5, fit=fitted, cfg=cfg
    ) is True


def test_cheap_nearby_passing_over_still_tradable() -> None:
    """A 225+ passing alt is a nearby number; a 20¢ over can still be +EV."""
    cfg = FairValueConfig(min_edge_pct=0.0)
    slate = _slate()
    slate["games"][0]["player_props"][0]["kalshi"]["yes_ask"] = 0.20
    slate["games"][0]["player_props"][0]["kalshi"]["yes_bid"] = 0.18
    slate["games"][0]["player_props"][0]["kalshi"]["no_ask"] = 0.82
    report = process_slate(slate, cfg)
    dart = [
        r
        for r in report.tradable + report.tradable_low_liquidity
        if r.venue == "kalshi" and r.player == "Jaxson Dart"
    ]
    assert any(r.side == "over" and r.market_price == pytest.approx(0.20) for r in dart)


def test_polymarket_prop_uses_ask_not_mid() -> None:
    """Wide Polymarket books must be scored at the ask, not Gamma's 26¢ mid."""
    cfg = FairValueConfig(min_edge_pct=-50.0)
    slate = _slate()
    slate["games"][0]["player_props"][0]["polymarket"] = {
        "line": 224.5,
        "line_delta": 11.5,
        "over": 0.26,
        "under": 0.74,
        "implied_prob": 0.26,
        "over_bid": 0.02,
        "over_ask": 0.50,
        "under_bid": 0.50,
        "under_ask": 0.98,
        "volume": 0.0,
        "liquidity": 269.61,
        "market_id": "4196015",
    }
    report = process_slate(slate, cfg)
    rows = [
        r
        for r in report.tradable + report.tradable_low_liquidity
        if r.venue == "polymarket" and r.player == "Jaxson Dart"
    ]
    overs = [r for r in rows if r.side == "over"]
    unders = [r for r in rows if r.side == "under"]
    assert overs and all(r.market_price == pytest.approx(0.50) for r in overs)
    assert unders and all(r.market_price == pytest.approx(0.98) for r in unders)
    assert all(r.market_price != pytest.approx(0.26) for r in rows)


def test_polymarket_mid_only_is_not_tradable() -> None:
    cfg = FairValueConfig(min_edge_pct=0.0)
    slate = _slate()
    slate["games"][0]["player_props"][0]["polymarket"] = {
        "line": 224.5,
        "over": 0.26,
        "under": 0.74,
        "implied_prob": 0.26,
        "liquidity": 269.61,
        "market_id": "4196015",
    }
    report = process_slate(slate, cfg)
    rows = [
        r
        for r in report.tradable + report.tradable_low_liquidity
        if r.venue == "polymarket" and r.player == "Jaxson Dart"
    ]
    assert rows == []


def test_polymarket_side_ask_ignores_mid() -> None:
    wide = {
        "over": 0.26,
        "under": 0.74,
        "implied_prob": 0.26,
        "over_bid": 0.02,
        "over_ask": 0.50,
        "under_bid": 0.50,
        "under_ask": 0.98,
    }
    assert polymarket_side_ask(wide, "over") == pytest.approx(0.50)
    assert polymarket_side_ask(wide, "under") == pytest.approx(0.98)
    assert polymarket_side_ask({"over": 0.26, "implied_prob": 0.26}, "over") is None
    assert polymarket_side_ask({"over_bid": 0.02, "over_ask": 0.50}, "under") == pytest.approx(0.98)


CHASE_REC = {
    "player": "Ja'Marr Chase",
    "type": "receiving_yards",
    "consensus_line": 85.5,
    "books": {
        "betr": {
            "line": 85.5,
            "over_odds": -118,
            "under_odds": -118,
            "over_implied_prob": 0.5413,
            "under_implied_prob": 0.5413,
        },
        "betrivers": {
            "line": 83.5,
            "over_odds": -115,
            "under_odds": -115,
            "over_implied_prob": 0.5349,
            "under_implied_prob": 0.5349,
        },
        "circasports": {
            "line": 85.5,
            "over_odds": -110,
            "under_odds": -110,
            "over_implied_prob": 0.5238,
            "under_implied_prob": 0.5238,
        },
        "draftkings": {
            "line": 85.5,
            "over_odds": -113,
            "under_odds": -111,
            "over_implied_prob": 0.5305,
            "under_implied_prob": 0.5261,
        },
        "fanatics": {
            "line": 85.5,
            "over_odds": -120,
            "under_odds": -110,
            "over_implied_prob": 0.5455,
            "under_implied_prob": 0.5238,
        },
        "fanduel": {
            "line": 85.5,
            "over_odds": -113,
            "under_odds": -113,
            "over_implied_prob": 0.5305,
            "under_implied_prob": 0.5305,
        },
        "hardrock": {
            "line": 86.5,
            "over_odds": -115,
            "under_odds": -115,
            "over_implied_prob": 0.5349,
            "under_implied_prob": 0.5349,
        },
        "thescore": {
            "line": 86.5,
            "over_odds": -115,
            "under_odds": -115,
            "over_implied_prob": 0.5349,
            "under_implied_prob": 0.5349,
        },
    },
    "kalshi": {
        "ticker": "KXNFLRECYDS-26SEP13TBCIN-CINJCHASE1-50",
        "yes_sub_title": "Ja'Marr Chase: 50+",
        "yes_bid": 0.76,
        "yes_ask": 0.84,
        "volume": 154.57,
        "line": 49.5,
        "line_delta": -36.0,
    },
}

TRAUTMAN_REC = {
    "player": "Adam Trautman",
    "type": "receiving_yards",
    "consensus_line": 7.5,
    "books": {
        "betr": {
            "line": 8.5,
            "over_odds": -112,
            "under_odds": -122,
            "over_implied_prob": 0.5283,
            "under_implied_prob": 0.5495,
        },
        "betrivers": {
            "line": 7.5,
            "over_odds": -112,
            "under_odds": -120,
            "over_implied_prob": 0.5283,
            "under_implied_prob": 0.5455,
        },
        "draftkings": {
            "line": 7.5,
            "over_odds": -117,
            "under_odds": -107,
            "over_implied_prob": 0.5392,
            "under_implied_prob": 0.5169,
        },
        "fanatics": {
            "line": 10.5,
            "over_odds": 125,
            "under_odds": -170,
            "over_implied_prob": 0.4444,
            "under_implied_prob": 0.6296,
        },
        "fanduel": {
            "line": 7.5,
            "over_odds": -114,
            "under_odds": -114,
            "over_implied_prob": 0.5327,
            "under_implied_prob": 0.5327,
        },
        "hardrock": {
            "line": 6.5,
            "over_odds": -120,
            "under_odds": -110,
            "over_implied_prob": 0.5455,
            "under_implied_prob": 0.5238,
        },
    },
    "kalshi": {
        "ticker": "KXNFLRECYDS-26SEP14DENKC-DENATRAUTMAN82-15",
        "yes_sub_title": "Adam Trautman: 15+",
        "yes_bid": 0.2,
        "yes_ask": 0.25,
        "volume": 300.0,
        "line": 14.5,
        "line_delta": 7.0,
    },
}


def _prop_slate(prop: dict, matchup: str) -> dict:
    return {"games": [{"matchup": matchup, "markets": {}, "player_props": [prop]}]}


def test_chase_silent_fit_failure_is_rejected() -> None:
    """Production Chase 50+ vs ~85.5: r2=0 walk, must not rank as tradable."""
    cfg = FairValueConfig(min_edge_pct=0.0)
    report = process_slate(_prop_slate(CHASE_REC, "TB @ CIN"), cfg)
    tradable = [
        r
        for r in report.tradable + report.tradable_low_liquidity
        if r.player == "Ja'Marr Chase" and r.market_line == pytest.approx(49.5)
    ]
    assert tradable == []
    rejected = [
        r
        for r in report.rejected
        if r.player == "Ja'Marr Chase" and r.venue == "kalshi"
    ]
    assert rejected
    assert all(r.veto_reason == "fit_quality_too_low" for r in rejected)
    assert all(r.is_extrapolation is True for r in rejected)
    assert all(r.extrapolation_distance == pytest.approx(34.0) for r in rejected)
    assert all(r.sigma_source == "fallback" for r in rejected)
    assert all(r.role_bucket == "high_target_share" for r in rejected)


def test_trautman_fallback_extrapolation_is_rejected() -> None:
    """Production Trautman 15+ vs books ~6.5–10.5: far walk on fallback σ."""
    cfg = FairValueConfig(min_edge_pct=0.0)
    report = process_slate(_prop_slate(TRAUTMAN_REC, "DEN @ KC"), cfg)
    tradable = [
        r
        for r in report.tradable + report.tradable_low_liquidity
        if r.player == "Adam Trautman"
    ]
    assert tradable == []
    rejected = [
        r
        for r in report.rejected
        if r.player == "Adam Trautman" and r.venue == "kalshi"
    ]
    assert rejected
    assert all(r.is_extrapolation is True for r in rejected)
    assert all(r.extrapolation_distance == pytest.approx(4.0) for r in rejected)
    assert all(r.veto_reason == "fallback_sigma_extrapolation" for r in rejected)
    assert all(r.role_bucket == "low_target_share" for r in rejected)


def test_nearby_passing_alt_is_not_vetoed() -> None:
    """Worked Picture B: books 211.5–218.5, Kalshi 224.5 — near-range, keep."""
    cfg = FairValueConfig(min_edge_pct=0.0)
    report = process_slate(_slate(), cfg)
    dart = [
        r
        for r in report.tradable + report.tradable_low_liquidity
        if r.player == "Jaxson Dart" and r.venue == "kalshi"
    ]
    rejected_dart = [
        r for r in report.rejected if r.player == "Jaxson Dart" and r.venue == "kalshi"
    ]
    assert dart
    assert rejected_dart == []
    for row in dart:
        assert row.veto_reason is None
        assert row.is_extrapolation is True
        assert row.extrapolation_distance == pytest.approx(6.0)
        assert row.role_bucket == "starter"


ANYTIME_TD = {
    "player": "Jalen McMillan",
    "type": "anytime_td",
    "books": {
        "draftkings": {"odds": 300, "implied_prob": 0.25},
        "fanduel": {"odds": 280, "implied_prob": 0.2632},
        "betr": {"odds": 320, "implied_prob": 0.2381},
        "thescore": {"odds": 300, "implied_prob": 0.25},
    },
    "kalshi": {
        "ticker": "KXNFLATD-TEST-JMCMILLAN",
        "yes_bid": 0.18,
        "yes_ask": 0.19,
        "volume": 200.0,
    },
}


def test_td_yesno_is_split_from_tradable() -> None:
    cfg = FairValueConfig(min_edge_pct=0.0)
    report = process_slate(_prop_slate(ANYTIME_TD, "TB @ CIN"), cfg)
    payload = report.to_dict()
    assert "tradable_td" in payload
    assert "tradable_td_low_liquidity" in payload
    main = report.tradable + report.tradable_low_liquidity
    tds = report.tradable_td + report.tradable_td_low_liquidity
    assert all(r.stat != "anytime_td" for r in main)
    assert tds
    assert all(r.stat == "anytime_td" for r in tds)
    assert all(r.player == "Jalen McMillan" for r in tds)


def test_first_td_and_2plus_td_go_to_tradable_td() -> None:
    cfg = FairValueConfig(min_edge_pct=0.0)
    first = dict(ANYTIME_TD)
    first["type"] = "first_td"
    first["player"] = "Ja'Marr Chase"
    plus = dict(ANYTIME_TD)
    plus["type"] = "2plus_td"
    plus["player"] = "Ja'Marr Chase"
    plus["kalshi"] = {**ANYTIME_TD["kalshi"], "ticker": "KXNFL2TD-TEST"}
    report = process_slate(
        {
            "games": [
                {
                    "matchup": "TB @ CIN",
                    "markets": {},
                    "player_props": [first, plus],
                }
            ]
        },
        cfg,
    )
    main = report.tradable + report.tradable_low_liquidity
    tds = report.tradable_td + report.tradable_td_low_liquidity
    stats = {r.stat for r in tds}
    assert "first_td" in stats
    assert "2plus_td" in stats
    assert all(r.stat not in {"first_td", "2plus_td", "anytime_td"} for r in main)


def _spread_book(home_line: float, away_line: float) -> dict:
    return {
        "away": {"line": away_line, "odds": -110, "implied_prob": 0.5238},
        "home": {"line": home_line, "odds": -110, "implied_prob": 0.5238},
    }


def test_away_favorite_spread_prices_yes_as_away() -> None:
    """CAR wins by over 3.5 is Carolina -3.5, not Cleveland +3.5."""
    books = {
        name: _spread_book(3.5, -3.5) for name in ("betr", "caesars", "draftkings", "fanduel")
    }
    slate = {
        "games": [
            {
                "matchup": "CAR @ CLE",
                "markets": {
                    "spread": {
                        "consensus_line": 3.5,
                        "books": books,
                        "kalshi": {
                            "ticker": "KXNFLSPREAD-26SEP22CARCLE-CAR4",
                            "yes_sub_title": "Carolina wins by over 3.5 points",
                            "yes_side": "away",
                            "yes_bid": 0.42,
                            "yes_ask": 0.44,
                            "no_bid": 0.56,
                            "no_ask": 0.58,
                            "line": 3.5,
                            "volume": 20000,
                        },
                    }
                },
            }
        ]
    }
    report = process_slate(slate, FairValueConfig(min_edge_pct=0.0))
    kalshi_info = [
        row
        for row in report.informational
        if row.book == "kalshi" and row.stat == "spread"
    ]
    by_side = {row.side: row for row in kalshi_info}
    assert set(by_side) == {"home", "away"}
    assert by_side["away"].book_prob == pytest.approx(0.44)
    assert by_side["home"].book_prob == pytest.approx(0.58)
    assert by_side["away"].fair_prob == pytest.approx(1.0 - by_side["home"].fair_prob)

    priced = [
        row
        for row in report.tradable + report.tradable_low_liquidity + report.rejected
        if row.venue == "kalshi" and row.stat == "spread"
    ]
    by_order = {row.side: row for row in priced}
    assert by_order["yes"].market_price == pytest.approx(0.44)
    assert by_order["yes"].fair_prob == pytest.approx(by_side["away"].fair_prob)
    assert "no" not in by_order
