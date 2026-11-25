"""Sharp execution places resting post-only limit buys, never market orders."""

from __future__ import annotations

from dataclasses import replace
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from polymaker.domain import MarketMeta, TokenMeta
from polymaker.trading import execute as ex
from polymaker.trading.execute import SharpTradeConfig, _trade_one
from polymaker.trading.kalshi_match import KalshiMatchedPlay
from polymaker.trading.match import MatchedPlay
from polymaker.trading.resting import RestingBook
from polymaker.trading.sharp import SharpPlay
from polymaker.trading.teams import TeamRef

AWAY = TeamRef("LAL", "lal", "Los Angeles Lakers")
HOME = TeamRef("DEN", "den", "Denver Nuggets")


def _start(hours: float) -> str:
    return (datetime.now(UTC) + timedelta(hours=hours)).strftime("%Y-%m-%dT%H:%M:%SZ")


def _play(start: str) -> SharpPlay:
    return SharpPlay(
        league="NBA",
        matchup="LAL @ DEN",
        side="DEN",
        market="moneyline",
        tier="A",
        home_away="home",
        game_time_utc=start,
        implied_fair_prob=0.55,
        rlm_confirmed=True,
        composite_gap=20.0,
        source_path="x",
        raw={},
    )


def _matched(start: str, *, best_ask: float = 0.51) -> MatchedPlay:
    meta = MarketMeta(
        condition_id="0xden",
        question="Lakers vs. Nuggets",
        slug="nba-lal-den-2026-11-25",
        tokens=(TokenMeta("tok-lal", "Lakers"), TokenMeta("tok-den", "Nuggets")),
        tick_size=0.01,
        neg_risk=False,
        min_order_size=5,
        rewards_min_size=0,
        rewards_max_spread=0,
        rewards_daily_rate=0,
        maker_fee_bps=0,
        taker_fee_bps=50,
        fees_enabled=True,
        end_date_iso=None,
        event_id="1",
        best_ask=best_ask,
        start_time_iso=start,
    )
    return MatchedPlay(
        play=_play(start),
        away=AWAY,
        home=HOME,
        side_team=HOME,
        event_date=date.today(),
        slug=meta.slug,
        meta=meta,
        token=meta.tokens[1],
        status="matched",
    )


class FakeGw:
    def __init__(self, best_ask: float = 0.51, place_error: str | None = None) -> None:
        self.best_ask = best_ask
        self.place_error = place_error
        self.placed: list[dict[str, Any]] = []
        self.market_orders = 0

    async def get_book(self, token_id: str) -> dict[str, float]:
        return {"best_bid": self.best_ask - 0.02, "best_ask": self.best_ask}

    async def place_limit_buy(self, token_id, price, size, meta, *, post_only=True):
        self.placed.append(
            {"token_id": token_id, "price": price, "size": size, "post_only": post_only}
        )
        if self.place_error:
            return {"order_id": None, "status": "failed", "error": self.place_error, "raw": {}}
        return {"order_id": "0xabc123456789", "status": "live", "error": None, "raw": {}}

    async def market_order(self, *a: Any, **k: Any) -> dict[str, Any]:
        self.market_orders += 1
        raise AssertionError("market orders are forbidden")

    async def cancel_order(self, order_id: str):
        return True, None


class FakeConvex:
    configured = True
    claims: list[str] = []
    released: list[str] = []

    def __init__(self) -> None:
        FakeConvex.claims = []
        FakeConvex.released = []

    def claim(self, **kw: Any):
        FakeConvex.claims.append(kw["trade_key_value"])
        from polymaker.trading.convex_trades import ClaimResult

        return ClaimResult(claimed=True, detail="claimed")

    def release(self, key: str) -> None:
        FakeConvex.released.append(key)

    def complete(self, *a: Any, **k: Any) -> None:  # pragma: no cover - never on placement
        raise AssertionError("placement must not complete a trade")


def _cfg(tmp_path: Path, **kw: Any) -> SharpTradeConfig:
    return SharpTradeConfig(
        dry_run=False,
        filled_log=str(tmp_path / "filled.jsonl"),
        resting_log=str(tmp_path / "resting.json"),
        maker_fills_log=str(tmp_path / "fills.jsonl"),
        venues=frozenset({"polymarket"}),
        **kw,
    )


@pytest.mark.asyncio
async def test_live_buy_rests_one_cent_below_the_ask(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(ex, "ConvexTradeClient", FakeConvex)
    gw = FakeGw(best_ask=0.51)
    start = _start(5)
    result = await _trade_one(_matched(start), gw, _cfg(tmp_path), set())  # type: ignore[arg-type]

    assert result.action == "resting"
    assert gw.market_orders == 0
    [call] = gw.placed
    assert call["price"] == pytest.approx(0.50) and call["price"] < 0.51
    assert call["post_only"] is True
    assert call["size"] == pytest.approx(50.0)  # $25 / 0.50
    assert result.limit_price == pytest.approx(0.50) and result.fee == 0.0

    [rec] = RestingBook(tmp_path / "resting.json").load()
    assert rec.order_id == "0xabc123456789"
    assert rec.venue == "polymarket" and rec.start_time == start
    assert rec.token_id == "tok-den" and rec.size == pytest.approx(50.0)
    assert rec.claimed_keys  # claims stay open while the order rests
    assert FakeConvex.released == []


@pytest.mark.asyncio
async def test_resting_play_is_not_placed_twice(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(ex, "ConvexTradeClient", FakeConvex)
    gw = FakeGw()
    m = _matched(_start(5))
    cfg = _cfg(tmp_path)
    await _trade_one(m, gw, cfg, set())  # type: ignore[arg-type]
    already = RestingBook(cfg.resting_log).keys()
    again = await _trade_one(m, gw, cfg, already)  # type: ignore[arg-type]
    assert again.action == "skipped" and "already traded" in again.detail
    assert len(gw.placed) == 1


@pytest.mark.asyncio
async def test_post_only_rejection_is_skipped_and_claim_released(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(ex, "ConvexTradeClient", FakeConvex)
    gw = FakeGw(place_error="order crosses book - post-only")
    result = await _trade_one(_matched(_start(5)), gw, _cfg(tmp_path), set())  # type: ignore[arg-type]
    assert result.action == "skipped"
    assert "would cross" in result.detail
    assert FakeConvex.released  # claim freed
    assert RestingBook(tmp_path / "resting.json").load() == []
    assert gw.market_orders == 0


@pytest.mark.asyncio
async def test_new_orders_are_blocked_inside_the_cancel_window(tmp_path: Path) -> None:
    class NoBook:
        async def get_book(self, token_id: str):
            raise AssertionError("must not quote a game inside the window")

    result = await _trade_one(
        _matched(_start(0.4)),  # 24 minutes out
        NoBook(),  # type: ignore[arg-type]
        _cfg(tmp_path),
        set(),
    )
    assert result.action == "skipped"
    assert "30-min maker window" in result.detail


@pytest.mark.asyncio
async def test_game_just_outside_the_window_still_places(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(ex, "ConvexTradeClient", FakeConvex)
    gw = FakeGw()
    result = await _trade_one(_matched(_start(0.6)), gw, _cfg(tmp_path), set())  # type: ignore[arg-type]
    assert result.action == "resting"


@pytest.mark.asyncio
async def test_dry_run_shows_limit_and_effective_price_for_both_venues() -> None:
    gw = FakeGw(best_ask=0.51)
    start = _start(5)
    kalshi = KalshiMatchedPlay(
        play=_play(start),
        away=AWAY,
        home=HOME,
        side_team=HOME,
        event_date=date.today(),
        ticker="KXNBAGAME-26NOV25LALDEN-DEN",
        kalshi_side="yes",
        line=None,
        ask=0.51,
        status="matched",
        start_time=start,
    )
    result = await _trade_one(
        _matched(start),
        gw,  # type: ignore[arg-type]
        SharpTradeConfig(),  # dry run
        set(),
        kalshi=kalshi,
    )
    assert result.action == "dry_run"
    assert result.venue == "polymarket"  # Kalshi carries a maker fee at equal prices
    assert "would REST BUY" in result.detail
    assert "limit=0.500" in result.detail and "post-only" in result.detail
    assert "vs kalshi limit=0.500 fee=0.22 effective=0.5044" in result.detail
    assert gw.placed == []  # dry run never posts


@pytest.mark.asyncio
async def test_kalshi_wins_when_it_is_more_than_the_fee_cheaper(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(ex, "ConvexTradeClient", FakeConvex)

    class FakeKalshi:
        has_auth = True

        def __init__(self) -> None:
            self.orders: list[dict[str, Any]] = []

        def get_market(self, ticker: str) -> dict[str, Any]:
            return {"yes_ask_dollars": 0.48, "yes_bid_dollars": 0.46}

        def create_event_order(self, **kw: Any) -> dict[str, Any]:
            self.orders.append(kw)
            return {"order_id": "k-123", "fill_count": "0.00", "remaining_count": kw["count"]}

    start = _start(5)
    kalshi = KalshiMatchedPlay(
        play=_play(start),
        away=AWAY,
        home=HOME,
        side_team=HOME,
        event_date=date.today(),
        ticker="KXNBAGAME-26NOV25LALDEN-DEN",
        kalshi_side="yes",
        line=None,
        ask=0.48,
        status="matched",
        start_time=start,
    )
    client = FakeKalshi()
    gw = FakeGw(best_ask=0.51)
    cfg = replace(_cfg(tmp_path), venues=frozenset({"polymarket", "kalshi"}))
    result = await _trade_one(
        _matched(start),
        gw,  # type: ignore[arg-type]
        cfg,
        set(),
        kalshi=kalshi,
        kalshi_client=client,
    )
    assert result.action == "resting" and result.venue == "kalshi"
    [order] = client.orders
    assert order["time_in_force"] == "good_till_canceled"
    assert order["post_only"] is True
    assert order["side"] == "bid" and order["price"] == "0.4700"  # 1c under the 48c ask
    [rec] = RestingBook(tmp_path / "resting.json").load()
    assert rec.venue == "kalshi" and rec.order_id == "k-123"
    assert rec.ticker == "KXNBAGAME-26NOV25LALDEN-DEN"
    assert gw.placed == []
