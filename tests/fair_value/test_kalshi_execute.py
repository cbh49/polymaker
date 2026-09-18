"""Kalshi $10 IOC buys from the NFL fair-value `tradable` bucket."""

from __future__ import annotations

import json
from decimal import Decimal
from pathlib import Path

import pytest

from ev_trading.fair_value.kalshi_execute import (
    KalshiTradeConfig,
    contracts_for_usd,
    kalshi_outcome_side,
    run_kalshi_trades,
    select_tradable_kalshi,
    v2_book_order,
)
from ev_trading.fair_value.models import PricedOpportunity
from ev_trading.fair_value.report import FairValueReport


def _opp(
    *,
    venue: str = "kalshi",
    side: str = "over",
    confidence: float = 0.85,
    price: float = 0.57,
    ticker: str | None = "KXNFLRECYDS-TEST-15",
    market: str = "Jonathan Taylor receiving_yards",
) -> PricedOpportunity:
    return PricedOpportunity(
        market=market,
        matchup="BAL @ IND",
        player="Jonathan Taylor",
        stat="receiving_yards",
        venue=venue,  # type: ignore[arg-type]
        side=side,
        fair_prob=0.65,
        fair_line=18.5,
        market_line=14.5,
        market_price=price,
        raw_edge=0.08,
        fee_adjusted_edge=0.06,
        expected_value_per_contract=0.06,
        confidence=confidence,
        volume=2000.0,
        liquidity=None,
        volume_24hr=None,
        line_delta=-4.0,
        low_liquidity=False,
        low_confidence=True,
        n_books=7,
        fit_r2=0.78,
        market_id=ticker,
        ticker=ticker,
        rank_score=0.05,
    )


def test_selects_only_liquid_kalshi_above_confidence() -> None:
    rows = [
        _opp(confidence=0.85),
        _opp(confidence=0.79, ticker="LOWCONF"),
        _opp(venue="polymarket", ticker="POLY-1"),
        _opp(ticker=None, market="no ticker"),
    ]
    picked = select_tradable_kalshi(rows, min_confidence=0.80)
    assert [r["ticker"] for r in picked] == ["KXNFLRECYDS-TEST-15"]


def test_side_maps_to_yes_no_and_v2_book() -> None:
    assert kalshi_outcome_side("over") == "yes"
    assert kalshi_outcome_side("home") == "yes"
    assert kalshi_outcome_side("under") == "no"
    assert kalshi_outcome_side("away") == "no"
    assert v2_book_order("over", 0.57) == ("bid", Decimal("0.5700"))
    assert v2_book_order("under", 0.87) == ("ask", Decimal("0.1300"))


def test_unmapped_side_is_dropped() -> None:
    picked = select_tradable_kalshi([_opp(side="draw")], min_confidence=0.80)
    assert picked == []


def test_ten_dollars_sizes_whole_contracts() -> None:
    assert contracts_for_usd(10.0, 0.57) == 17
    assert contracts_for_usd(10.0, 0.87) == 11
    assert contracts_for_usd(10.0, 0.99) == 10
    assert contracts_for_usd(10.0, 0.0) == 0


def test_dry_run_does_not_call_kalshi(tmp_path: Path) -> None:
    client = _FakeClient()
    report = FairValueReport(tradable=[_opp()])
    results = run_kalshi_trades(
        report,
        KalshiTradeConfig(dry_run=True, filled_log=tmp_path / "fills.jsonl", refresh_quote=False),
        client=client,
    )
    assert len(results) == 1
    assert results[0].action == "dry_run"
    assert results[0].contracts == 17
    assert results[0].usd == pytest.approx(9.69)
    assert results[0].book_side == "bid"
    assert client.orders == []
    assert not (tmp_path / "fills.jsonl").exists()


def test_live_ioc_buy_yes_and_no(tmp_path: Path) -> None:
    client = _FakeClient(fill_count="17.00")
    log = tmp_path / "fills.jsonl"
    report = FairValueReport(
        tradable=[
            _opp(side="over", price=0.57, ticker="YES-TICK"),
            _opp(side="under", price=0.87, ticker="NO-TICK", market="under row"),
        ]
    )
    results = run_kalshi_trades(
        report,
        KalshiTradeConfig(dry_run=False, filled_log=log, refresh_quote=False),
        client=client,
    )
    assert [r.action for r in results] == ["bought", "bought"]
    assert client.orders[0]["ticker"] == "YES-TICK"
    assert client.orders[0]["side"] == "bid"
    assert client.orders[0]["count"] == "17.00"
    assert client.orders[0]["price"] == "0.5700"
    assert client.orders[0]["time_in_force"] == "immediate_or_cancel"
    assert client.orders[1]["ticker"] == "NO-TICK"
    assert client.orders[1]["side"] == "ask"
    assert client.orders[1]["count"] == "11.00"
    assert client.orders[1]["price"] == "0.1300"
    assert log.is_file()

    again = run_kalshi_trades(
        report,
        KalshiTradeConfig(dry_run=False, filled_log=log, refresh_quote=False),
        client=client,
    )
    assert all(r.action == "skipped" for r in again)
    assert "already traded" in again[0].detail
    assert len(client.orders) == 2


def test_live_skips_when_ask_moves(tmp_path: Path) -> None:
    client = _FakeClient(market={"yes_ask": 0.70, "yes_bid": 0.68})
    results = run_kalshi_trades(
        FairValueReport(tradable=[_opp(price=0.57)]),
        KalshiTradeConfig(dry_run=False, filled_log=tmp_path / "fills.jsonl", refresh_quote=True),
        client=client,
    )
    assert results[0].action == "skipped"
    assert "ask moved" in results[0].detail
    assert client.orders == []


def test_live_reads_ask_dollars_from_get_market(tmp_path: Path) -> None:
    """GET /markets/{ticker} quotes yes_ask_dollars, not yes_ask."""
    client = _FakeClient(
        fill_count="17.00",
        market={
            "ticker": "KXNFLRECYDS-TEST-15",
            "yes_bid_dollars": "0.5500",
            "yes_ask_dollars": "0.5700",
            "no_bid_dollars": "0.4300",
            "no_ask_dollars": "0.4500",
        },
    )
    results = run_kalshi_trades(
        FairValueReport(tradable=[_opp(price=0.57)]),
        KalshiTradeConfig(dry_run=False, filled_log=tmp_path / "fills.jsonl", refresh_quote=True),
        client=client,
    )
    assert results[0].action == "bought"
    assert results[0].limit_price == 0.57
    assert client.orders[0]["price"] == "0.5700"


def test_live_under_uses_no_ask_dollars(tmp_path: Path) -> None:
    client = _FakeClient(
        fill_count="11.00",
        market={
            "yes_bid_dollars": "0.1300",
            "yes_ask_dollars": "0.1500",
            "no_ask_dollars": "0.8700",
        },
    )
    results = run_kalshi_trades(
        FairValueReport(tradable=[_opp(side="under", price=0.87, ticker="NO-TICK")]),
        KalshiTradeConfig(dry_run=False, filled_log=tmp_path / "fills.jsonl", refresh_quote=True),
        client=client,
    )
    assert results[0].action == "bought"
    assert client.orders[0]["side"] == "ask"
    assert client.orders[0]["price"] == "0.1300"


def test_ioc_no_fill_is_not_deduped(tmp_path: Path) -> None:
    client = _FakeClient(fill_count="0.00")
    log = tmp_path / "fills.jsonl"
    cfg = KalshiTradeConfig(dry_run=False, filled_log=log, refresh_quote=False)
    report = FairValueReport(tradable=[_opp()])
    first = run_kalshi_trades(report, cfg, client=client)
    assert first[0].action == "skipped"
    assert first[0].detail == "IOC no fill"
    assert not log.exists()
    run_kalshi_trades(report, cfg, client=client)
    assert len(client.orders) == 2


def test_json_report_only_uses_tradable_bucket(tmp_path: Path) -> None:
    payload = {
        "tradable": [_opp(confidence=0.90, ticker="KEEP").to_dict()],
        "tradable_low_liquidity": [_opp(confidence=0.99, ticker="THIN").to_dict()],
        "tradable_td": [_opp(confidence=0.99, ticker="TD").to_dict()],
    }
    path = tmp_path / "nfl_fair_value.json"
    path.write_text(json.dumps(payload), encoding="utf-8")
    results = run_kalshi_trades(
        path,
        KalshiTradeConfig(dry_run=True, filled_log=tmp_path / "fills.jsonl"),
        client=_FakeClient(),
    )
    assert [r.ticker for r in results] == ["KEEP"]


class _FakeClient:
    def __init__(
        self,
        *,
        fill_count: str = "1.00",
        market: dict | None = None,
    ) -> None:
        self.fill_count = fill_count
        self.market = market or {"yes_ask": 0.57, "yes_bid": 0.55, "no_ask": 0.45}
        self.orders: list[dict] = []

    def get_market(self, ticker: str) -> dict:
        return dict(self.market)

    def create_event_order(self, **kwargs) -> dict:
        self.orders.append(kwargs)
        return {
            "order_id": "ord-1",
            "fill_count": self.fill_count,
            "remaining_count": "0.00",
        }
