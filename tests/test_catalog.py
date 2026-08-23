"""Tests for market parsing, scoring, and the SQLite catalog store."""

from __future__ import annotations

import json
from datetime import UTC

import pytest

from polymaker.catalog.gamma import parse_market
from polymaker.catalog.scoring import score_market
from polymaker.catalog.store import CatalogStore

RAW = {
    "conditionId": "0xabc",
    "question": "Will candidate X win?",
    "slug": "will-x-win",
    "clobTokenIds": json.dumps(["tok-yes", "tok-no"]),
    "outcomes": json.dumps(["Yes", "No"]),
    "orderPriceMinTickSize": 0.01,
    "orderMinSize": 5,
    "negRisk": True,
    "acceptingOrders": True,
    "rewardsMinSize": 10,
    "rewardsMaxSpread": 3.0,
    "feesEnabled": True,
    "feeSchedule": {"rate": 0.01, "takerOnly": True, "rebateRate": 0.25},
    "bestBid": 0.48,
    "bestAsk": 0.50,
    "liquidityNum": 20000.0,
    "volumeNum": 500000.0,
    "endDate": "2028-11-07T00:00:00Z",
    "events": [{"id": 999, "slug": "2028-election"}],
}


def test_parse_market_maps_fields():
    m = parse_market(RAW, reward_rates={"0xabc": 42.0})
    assert m is not None
    assert m.condition_id == "0xabc"
    assert m.yes.token_id == "tok-yes"
    assert m.no.token_id == "tok-no"
    assert m.tick_size == 0.01
    assert m.neg_risk is True
    assert m.rewards_daily_rate == 42.0
    assert m.taker_fee_bps == 100  # 0.01 -> 100 bps
    assert m.maker_fee_bps == 0  # V2 makers pay zero
    assert m.rebate_rate == 0.25
    assert m.event_id == "999"
    assert m.start_time_iso is None


def test_parse_market_reads_event_start_time():
    raw = {
        **RAW,
        "events": [{"id": 999, "slug": "mlb-atl-cws-2026-08-20",
                    "startTime": "2026-08-20T17:05:00Z"}],
    }
    m = parse_market(raw)
    assert m is not None
    assert m.start_time_iso == "2026-08-20T17:05:00Z"


def test_parse_market_rejects_non_binary_and_closed():
    triple = {**RAW, "clobTokenIds": json.dumps(["a", "b", "c"]),
              "outcomes": json.dumps(["A", "B", "C"])}
    assert parse_market(triple) is None
    not_accepting = {**RAW, "acceptingOrders": False}
    assert parse_market(not_accepting) is None


def test_score_prefers_rewards_and_rebates():
    good = parse_market(RAW, {"0xabc": 100.0})
    poor = parse_market({**RAW, "conditionId": "0xdef", "rewardsMinSize": 0,
                         "rewardsMaxSpread": 0, "feesEnabled": False},
                        {"0xdef": 0.0})
    assert score_market(good).score > score_market(poor).score


def test_score_penalizes_extremity():
    balanced = parse_market(RAW, {"0xabc": 50.0})
    extreme = parse_market({**RAW, "conditionId": "0xext", "bestBid": 0.96, "bestAsk": 0.98},
                           {"0xext": 50.0})
    assert score_market(extreme).extremity > score_market(balanced).extremity


def test_parse_market_reads_price_changes():
    raw = {**RAW, "oneHourPriceChange": 0.03, "oneDayPriceChange": -0.08}
    m = parse_market(raw, {"0xabc": 42.0})
    assert m is not None
    assert m.one_hour_price_change == pytest.approx(0.03)
    assert m.one_day_price_change == pytest.approx(-0.08)


def test_gap_risk_low_on_deep_quiet_book():
    from polymaker.catalog.scoring import gap_risk

    deep = parse_market(RAW, {"0xabc": 50.0})
    assert deep is not None
    assert gap_risk(deep) < 0.15


def test_gap_risk_high_on_thin_volatile_book():
    """Romania-style: modest liquidity, large reward min, big trailing move."""
    from polymaker.catalog.scoring import gap_risk

    gappy = parse_market({
        **RAW,
        "conditionId": "0xgap",
        "liquidityNum": 800.0,
        "rewardsMinSize": 50,
        "bestBid": 0.47,
        "bestAsk": 0.49,
        "oneDayPriceChange": 0.09,
        "oneHourPriceChange": 0.04,
    }, {"0xgap": 50.0})
    assert gappy is not None
    assert gap_risk(gappy) > 0.5


def test_score_discounts_gap_prone_markets():
    """Same rewards/liquidity: a quiet book must outrank one with big trailing gaps."""
    deep = parse_market(RAW, {"0xabc": 100.0})
    gappy = parse_market({
        **RAW,
        "conditionId": "0xgap",
        "oneDayPriceChange": 0.12,
        "oneHourPriceChange": 0.06,
    }, {"0xgap": 100.0})
    assert deep is not None and gappy is not None
    deep_sc = score_market(deep)
    gappy_sc = score_market(gappy)
    assert gappy_sc.gap_risk > deep_sc.gap_risk
    assert deep_sc.score > gappy_sc.score


def test_store_roundtrip_and_top(tmp_path):
    store = CatalogStore(tmp_path / "s.db")
    m = parse_market(RAW, {"0xabc": 42.0})
    store.upsert_market(m)
    assert store.get("0xabc").condition_id == "0xabc"
    assert store.get("0xabc").start_time_iso is None
    assert store.get_by_slug("will-x-win").slug == "will-x-win"
    top = store.top(10)
    assert len(top) == 1 and top[0][0].condition_id == "0xabc"
    # tokens survive the JSON round-trip as a 2-tuple
    assert len(store.get("0xabc").tokens) == 2
    store.close()


def test_store_upsert_is_idempotent(tmp_path):
    store = CatalogStore(tmp_path / "s.db")
    m = parse_market(RAW, {"0xabc": 42.0})
    store.upsert_market(m)
    store.upsert_market(m)  # second time updates, not duplicates
    assert len(store.top(10)) == 1
    store.close()


# ── sports discovery helpers ───────────────────────────────────────────────


def test_moneyline_slug_pattern():
    from polymaker.catalog.sports import is_moneyline_slug, pick_moneyline_market

    assert is_moneyline_slug("mlb-atl-cws-2026-08-20")
    assert is_moneyline_slug("wnba-wsh-gsv-2026-07-20")
    assert is_moneyline_slug("ufc-ant-gre3-2026-08-22")
    assert not is_moneyline_slug("mlb-atl-cws-2026-08-20-spread-home-1pt5")
    assert not is_moneyline_slug("mlb-ari-stl-2026-06-25-first-five-winner")
    assert not is_moneyline_slug("ufc-ant-gre3-2026-08-22-totals-1pt5")
    assert not is_moneyline_slug("will-x-win")

    event = {
        "slug": "mlb-atl-cws-2026-08-20",
        "markets": [
            {"slug": "mlb-atl-cws-2026-08-20-total-8pt5", "closed": False},
            {"slug": "mlb-atl-cws-2026-08-20", "closed": False, "conditionId": "0xmlb"},
            {"slug": "mlb-atl-cws-2026-08-20", "closed": True, "conditionId": "0xold"},
        ],
    }
    money = pick_moneyline_market(event)
    assert money is not None and money["conditionId"] == "0xmlb"


def test_event_date_window():
    from datetime import date

    from polymaker.catalog.sports import event_date_in_window

    today = date(2026, 7, 20)
    assert event_date_in_window("2026-07-20", look_ahead_days=3, today=today)
    assert event_date_in_window("2026-07-23", look_ahead_days=3, today=today)
    assert not event_date_in_window("2026-07-24", look_ahead_days=3, today=today)
    assert not event_date_in_window("2026-07-19", look_ahead_days=3, today=today)
    # full ISO still works (first 10 chars)
    assert event_date_in_window("2026-07-21T00:00:00Z", look_ahead_days=3, today=today)


def test_is_pre_game():
    from datetime import datetime, timedelta

    from polymaker.catalog.sports import is_pre_game

    now = datetime(2026, 8, 22, 18, 0, tzinfo=UTC)
    # More than 5 minutes out → safe to trade.
    assert is_pre_game({"startTime": "2026-08-22T18:10:00Z"}, now=now)
    # Inside the kickoff buffer.
    assert not is_pre_game({"startTime": "2026-08-22T18:04:00Z"}, now=now)
    # Already started / finished.
    assert not is_pre_game({"startTime": "2026-08-22T17:00:00Z"}, now=now)
    # live / gameStatus must not override startTime (Gamma often leaves them null).
    assert is_pre_game(
        {"startTime": "2026-08-22T19:00:00Z", "live": True, "gameStatus": "live"},
        now=now,
    )
    assert not is_pre_game(
        {"startTime": "2026-08-22T17:00:00Z", "live": False, "gameStatus": None},
        now=now,
    )
    # Missing startTime fails closed.
    assert not is_pre_game({}, now=now)
    assert not is_pre_game(None, now=now)
    # Configurable buffer.
    assert is_pre_game(
        {"startTime": "2026-08-22T18:04:00Z"}, buffer_minutes=2, now=now
    )
    later = now + timedelta(minutes=20)
    assert not is_pre_game({"startTime": "2026-08-22T18:10:00Z"}, now=later)


def test_skip_live_events():
    from datetime import datetime

    from polymaker.catalog.sports import should_skip_live_event

    now = datetime(2026, 7, 20, 18, 0, tzinfo=UTC)
    base = {"slug": "mlb-atl-cws-2026-08-20", "seriesSlug": "mlb"}

    # Missing startTime on a sports event → skip (fail closed).
    assert should_skip_live_event({**base, "live": True}, now=now)
    assert should_skip_live_event({**base, "ended": True}, now=now)
    assert should_skip_live_event(
        {**base, "live": False, "ended": False, "startTime": "2026-07-20T17:00:00Z"},
        now=now,
    )
    assert not should_skip_live_event(
        {**base, "live": False, "ended": False, "startTime": "2026-07-20T19:00:00Z"},
        now=now,
    )
    # live=true is ignored when startTime is still in the future.
    assert not should_skip_live_event(
        {**base, "live": True, "startTime": "2026-07-20T19:00:00Z"},
        now=now,
    )
    # Within the default 5-minute buffer.
    assert should_skip_live_event(
        {**base, "startTime": "2026-07-20T18:03:00Z"},
        now=now,
    )
    # politics / non-sports without live flag
    assert not should_skip_live_event(
        {"slug": "will-x-win", "startTime": "2020-01-01T00:00:00Z"},
        now=now,
    )
    # skip_live disabled
    assert not should_skip_live_event({**base, "live": True}, skip_live=False, now=now)
