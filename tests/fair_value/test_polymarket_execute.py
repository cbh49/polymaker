"""Polymarket $10 FAK buys from the NFL fair-value `tradable` bucket."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from ev_trading.fair_value.models import PricedOpportunity
from ev_trading.fair_value.polymarket_execute import (
    PolyTradeConfig,
    poly_trade_side,
    run_polymarket_trades_async,
    select_tradable_polymarket,
    token_for_poly_side,
)
from ev_trading.fair_value.report import FairValueReport
from polymaker.domain import MarketMeta, Side, TokenMeta
from polymaker.trading.convex_trades import ClaimResult


def _opp(
    *,
    venue: str = "polymarket",
    side: str = "home",
    confidence: float = 0.85,
    price: float = 0.46,
    market_id: str | None = "3340142",
    market: str = "DAL @ NYG spread",
    matchup: str = "DAL @ NYG",
    player: str | None = None,
    stat: str = "spread",
    fee_adjusted_edge: float = 0.06,
) -> PricedOpportunity:
    return PricedOpportunity(
        market=market,
        matchup=matchup,
        player=player,
        stat=stat,
        venue=venue,  # type: ignore[arg-type]
        side=side,
        fair_prob=0.50,
        fair_line=3.0,
        market_line=3.0,
        market_price=price,
        raw_edge=0.04,
        fee_adjusted_edge=fee_adjusted_edge,
        expected_value_per_contract=fee_adjusted_edge,
        confidence=confidence,
        volume=None,
        liquidity=10000.0,
        volume_24hr=350.0,
        line_delta=0.0,
        low_liquidity=False,
        low_confidence=False,
        n_books=9,
        fit_r2=None,
        market_id=market_id,
        ticker=None,
        rank_score=0.04,
    )


def _spread_meta() -> MarketMeta:
    return MarketMeta(
        condition_id="0xcond",
        question="Spread: DAL @ NYG",
        slug="nfl-dal-nyg-2026-09-14-spread-home-3pt5",
        tokens=(
            TokenMeta("tok-dal", "Dallas Cowboys"),
            TokenMeta("tok-nyg", "New York Giants"),
        ),
        tick_size=0.01,
        neg_risk=False,
        min_order_size=5.0,
        rewards_min_size=0.0,
        rewards_max_spread=0.0,
        rewards_daily_rate=0.0,
        maker_fee_bps=0,
        taker_fee_bps=0,
        fees_enabled=False,
        end_date_iso="2029-09-14T00:00:00Z",
        event_id="evt-1",
        start_time_iso="2029-09-14T17:00:00Z",
    )


def test_selects_only_liquid_polymarket_above_confidence() -> None:
    rows = [
        _opp(confidence=0.85),
        _opp(confidence=0.79, market_id="LOWCONF"),
        _opp(venue="kalshi", market_id="KXNFL-1"),
        _opp(market_id=None, market="no id"),
    ]
    picked = select_tradable_polymarket(rows, min_confidence=0.80)
    assert [r["market_id"] for r in picked] == ["3340142"]


def test_selects_drops_rows_below_five_point_edge() -> None:
    picked = select_tradable_polymarket(
        [
            _opp(market_id="KEEP", fee_adjusted_edge=0.06),
            _opp(market_id="THIN-EDGE", fee_adjusted_edge=0.049),
        ],
        min_confidence=0.80,
        min_edge_pct=5.0,
    )
    assert [r["market_id"] for r in picked] == ["KEEP"]


def test_ml_yes_side_is_home_or_away_from_market_name() -> None:
    assert poly_trade_side(_opp(side="yes", market="DAL @ NYG ML home").to_dict()) == "home"
    assert poly_trade_side(_opp(side="yes", market="DAL @ NYG ML away").to_dict()) == "away"
    assert poly_trade_side(_opp(side="over").to_dict()) == "over"


def test_spread_token_maps_home_away_to_nfl_team() -> None:
    meta = _spread_meta()
    home = token_for_poly_side(meta, "home", "DAL @ NYG")
    away = token_for_poly_side(meta, "away", "DAL @ NYG")
    assert home.token_id == "tok-nyg"
    assert away.token_id == "tok-dal"


def test_over_under_token_maps_by_outcome_label() -> None:
    meta = MarketMeta(
        condition_id="0xcond",
        question="Receiving yards",
        slug="nfl-mia-lv-recyd-jack-bech-14pt5",
        tokens=(TokenMeta("tok-over", "Over"), TokenMeta("tok-under", "Under")),
        tick_size=0.01,
        neg_risk=False,
        min_order_size=5.0,
        rewards_min_size=0.0,
        rewards_max_spread=0.0,
        rewards_daily_rate=0.0,
        maker_fee_bps=0,
        taker_fee_bps=0,
        fees_enabled=False,
        end_date_iso=None,
        event_id=None,
    )
    assert token_for_poly_side(meta, "over", "MIA @ LV").token_id == "tok-over"
    assert token_for_poly_side(meta, "under", "MIA @ LV").token_id == "tok-under"


@pytest.mark.asyncio
async def test_dry_run_does_not_call_polymarket(tmp_path: Path) -> None:
    gamma = _FakeGamma()
    gw = _FakeGateway()
    results = await run_polymarket_trades_async(
        FairValueReport(tradable=[_opp()]),
        PolyTradeConfig(dry_run=True, filled_log=tmp_path / "fills.jsonl"),
        gamma=gamma,
        gateway=gw,
    )
    assert len(results) == 1
    assert results[0].action == "dry_run"
    assert results[0].usd == 10.0
    assert gamma.calls == []
    assert gw.orders == []
    assert not (tmp_path / "fills.jsonl").exists()


@pytest.mark.asyncio
async def test_live_fak_buy_and_dedupe(tmp_path: Path) -> None:
    log = tmp_path / "fills.jsonl"
    gamma = _FakeGamma(raw=_gamma_spread())
    gw = _FakeGateway()
    convex = _FakeConvex()
    report = FairValueReport(tradable=[_opp()])
    cfg = PolyTradeConfig(
        dry_run=False, filled_log=log, refresh_quote=True, convex=convex
    )
    results = await run_polymarket_trades_async(report, cfg, gamma=gamma, gateway=gw)
    assert results[0].action == "bought"
    assert results[0].outcome == "New York Giants"
    assert gw.orders[0]["token_id"] == "tok-nyg"
    assert gw.orders[0]["amount"] == 10.0
    assert gw.orders[0]["side"] is Side.BUY
    assert log.is_file()
    assert len(convex.claims) == 2
    assert convex.claims[0]["trade_key_value"].startswith("nfl-ev|polymarket|")
    assert convex.claims[1]["payload"]["lockOnly"] is True
    assert len(convex.completes) == 1

    again = await run_polymarket_trades_async(report, cfg, gamma=gamma, gateway=gw)
    assert again[0].action == "skipped"
    assert "already traded" in again[0].detail
    assert len(gw.orders) == 1


@pytest.mark.asyncio
async def test_live_skips_when_ask_moves(tmp_path: Path) -> None:
    gw = _FakeGateway(book={"best_bid": 0.60, "best_ask": 0.61})
    results = await run_polymarket_trades_async(
        FairValueReport(tradable=[_opp(price=0.46)]),
        PolyTradeConfig(
            dry_run=False,
            filled_log=tmp_path / "fills.jsonl",
            convex=_FakeConvex(),
        ),
        gamma=_FakeGamma(raw=_gamma_spread()),
        gateway=gw,
    )
    assert results[0].action == "skipped"
    assert "ask moved" in results[0].detail
    assert gw.orders == []


@pytest.mark.asyncio
async def test_json_report_only_uses_tradable_bucket(tmp_path: Path) -> None:
    payload = {
        "tradable": [_opp(confidence=0.90, market_id="KEEP").to_dict()],
        "tradable_low_liquidity": [_opp(confidence=0.99, market_id="THIN").to_dict()],
        "tradable_td": [_opp(confidence=0.99, market_id="TD").to_dict()],
    }
    path = tmp_path / "nfl_fair_value.json"
    path.write_text(json.dumps(payload), encoding="utf-8")
    results = await run_polymarket_trades_async(
        path,
        PolyTradeConfig(dry_run=True, filled_log=tmp_path / "fills.jsonl"),
        gamma=_FakeGamma(),
        gateway=_FakeGateway(),
    )
    assert [r.market_id for r in results] == ["KEEP"]


@pytest.mark.asyncio
async def test_live_skips_when_convex_already_claimed(tmp_path: Path) -> None:
    gw = _FakeGateway()
    results = await run_polymarket_trades_async(
        FairValueReport(tradable=[_opp()]),
        PolyTradeConfig(
            dry_run=False,
            filled_log=tmp_path / "fills.jsonl",
            convex=_FakeConvex(claimed=False, detail="already traded (convex ledger)"),
        ),
        gamma=_FakeGamma(raw=_gamma_spread()),
        gateway=gw,
    )
    assert results[0].action == "skipped"
    assert "already traded" in results[0].detail
    assert gw.orders == []


@pytest.mark.asyncio
async def test_live_skips_when_convex_unconfigured(tmp_path: Path) -> None:
    gw = _FakeGateway()
    results = await run_polymarket_trades_async(
        FairValueReport(tradable=[_opp()]),
        PolyTradeConfig(
            dry_run=False,
            filled_log=tmp_path / "fills.jsonl",
            convex=_FakeConvex(configured=False),
        ),
        gamma=_FakeGamma(raw=_gamma_spread()),
        gateway=gw,
    )
    assert results[0].action == "skipped"
    assert "convex unavailable" in results[0].detail
    assert gw.orders == []


def _gamma_spread() -> dict[str, Any]:
    return {
        "conditionId": "0xcond",
        "question": "Spread: DAL @ NYG",
        "slug": "nfl-dal-nyg-2026-09-14-spread-home-3pt5",
        "acceptingOrders": True,
        "clobTokenIds": ["tok-dal", "tok-nyg"],
        "outcomes": ["Dallas Cowboys", "New York Giants"],
        "orderPriceMinTickSize": 0.01,
        "negRisk": False,
        "orderMinSize": 5,
        "endDate": "2029-09-14T00:00:00Z",
        "startTime": "2029-09-14T17:00:00Z",
    }


class _FakeGamma:
    def __init__(self, raw: dict[str, Any] | None = None) -> None:
        self.raw = raw
        self.calls: list[str] = []

    async def market_by_id(self, market_id: str) -> dict[str, Any] | None:
        self.calls.append(market_id)
        return self.raw

    async def aclose(self) -> None:
        return None


class _FakeGateway:
    def __init__(self, *, book: dict[str, float] | None = None) -> None:
        self.book = book or {"best_bid": 0.45, "best_ask": 0.46}
        self.orders: list[dict[str, Any]] = []

    async def connect(self) -> None:
        return None

    def close(self) -> None:
        return None

    async def get_book(self, token_id: str) -> dict[str, float]:
        return dict(self.book)

    async def market_order(
        self,
        token_id: str,
        side: Side,
        amount: float,
        meta: MarketMeta,
        *,
        fak: bool = True,
    ) -> dict[str, Any]:
        self.orders.append(
            {"token_id": token_id, "side": side, "amount": amount, "fak": fak, "slug": meta.slug}
        )
        return {"status": "matched", "takingAmount": "21.74", "makingAmount": "10"}


class _FakeConvex:
    def __init__(self, *, configured: bool = True, claimed: bool = True, detail: str = "claimed") -> None:
        self.configured = configured
        self._claimed = claimed
        self._detail = detail
        self.claims: list[dict] = []
        self.completes: list[tuple] = []
        self.releases: list[str] = []

    def claim(self, **kwargs):
        self.claims.append(kwargs)
        return ClaimResult(claimed=self._claimed, detail=self._detail)

    def complete(self, trade_key_value: str, payload: dict, **kwargs) -> None:
        self.completes.append((trade_key_value, payload, kwargs))

    def release(self, trade_key_value: str) -> None:
        self.releases.append(trade_key_value)
