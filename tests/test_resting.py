"""Resting maker orders: cancel window, fills, partial fills, failures, fee logging."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from polymaker.trading.maker_fills import kalshi_fill_delta, polymarket_fills
from polymaker.trading.resting import (
    ReconcileConfig,
    RestingBook,
    RestingOrder,
    in_cancel_window,
    reconcile_resting_orders,
)

NOW = datetime(2026, 11, 25, 15, 45, tzinfo=UTC)  # "3:45"


def _iso(dt: datetime) -> str:
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


def _order(**kw: Any) -> RestingOrder:
    base: dict[str, Any] = {
        "venue": "polymarket",
        "order_id": "0xorder1",
        "canonical": "sharp|NBA|2026-11-25|LAL|DEN|moneyline|DEN",
        "limit_price": 0.50,
        "size": 50.0,
        "claimed_keys": ["sharp|NBA|2026-11-25|LAL|DEN|moneyline|DEN", "slug|den"],
        "slug_key": "nba-lal-den-2026-11-25|denver",
        "league": "NBA",
        "matchup": "LAL @ DEN",
        "side": "Denver",
        "tier": "A",
        "usd": 25.0,
        "ask": 0.51,
        "start_time": _iso(NOW + timedelta(minutes=45)),  # starts 4:30
        "token_id": "tok-den",
        "slug": "nba-lal-den-2026-11-25",
    }
    base.update(kw)
    return RestingOrder(**base)


class FakeGateway:
    def __init__(self, orders: dict[str, dict[str, Any]], trades: list[dict[str, Any]] | None = None):
        self.orders = orders
        self.trades = trades or []
        self.cancelled: list[str] = []
        self.cancel_ok = True
        self.cancel_reason = "boom"

    async def get_order(self, order_id: str) -> dict[str, Any] | None:
        return self.orders.get(order_id)

    async def order_trades(self, order_id: str, token_id: str, *, after_ts=None):
        return self.trades

    async def cancel_order(self, order_id: str) -> tuple[bool, str | None]:
        if not self.cancel_ok:
            return False, self.cancel_reason
        self.cancelled.append(order_id)
        self.orders[order_id]["status"] = "canceled"
        return True, None


class FakeConvex:
    def __init__(self) -> None:
        self.completed: list[tuple[str, dict[str, Any], dict[str, Any]]] = []
        self.released: list[str] = []

    def complete(self, key: str, payload: dict[str, Any], **kw: Any) -> None:
        self.completed.append((key, payload, kw))

    def release(self, key: str) -> None:
        self.released.append(key)


def _cfg(tmp_path: Path) -> ReconcileConfig:
    return ReconcileConfig(
        filled_log=str(tmp_path / "sharp_trades.jsonl"),
        maker_fills_log=str(tmp_path / "maker_fills.jsonl"),
    )


def _live(size_matched: float = 0.0, original: float = 50.0, status: str = "live") -> dict[str, Any]:
    return {"status": status, "original_size": original, "size_matched": size_matched, "price": 0.5}


def _maker_trade(order_id: str, size: float, price: float = 0.50, tid: str = "t1") -> dict[str, Any]:
    return {
        "id": tid,
        "status": "MATCHED",
        "taker_order_id": "someone-else",
        "price": str(price),
        "size": str(size),
        "maker_orders": [
            {"order_id": order_id, "matched_amount": str(size), "price": str(price)},
        ],
    }


# ── window ──────────────────────────────────────────────────────────────
def test_cancel_window_matches_the_spec_example() -> None:
    start = _iso(datetime(2026, 11, 25, 16, 30, tzinfo=UTC))  # event starts at 4:30
    assert not in_cancel_window(start, 30, now=datetime(2026, 11, 25, 15, 45, tzinfo=UTC))
    assert in_cancel_window(start, 30, now=datetime(2026, 11, 25, 16, 15, tzinfo=UTC))
    assert in_cancel_window(start, 30, now=datetime(2026, 11, 25, 16, 0, tzinfo=UTC))  # exactly 30
    assert not in_cancel_window(start, 30, now=datetime(2026, 11, 25, 15, 59, 59, tzinfo=UTC))
    assert in_cancel_window(start, 30, now=datetime(2026, 11, 25, 16, 45, tzinfo=UTC))  # started


def test_missing_or_bad_start_fails_closed() -> None:
    assert in_cancel_window(None, 30, now=NOW)
    assert in_cancel_window("not a time", 30, now=NOW)


# ── book ────────────────────────────────────────────────────────────────
def test_book_round_trip_and_keys(tmp_path: Path) -> None:
    book = RestingBook(tmp_path / "j" / "resting.json")
    assert book.load() == []
    rec = _order()
    book.add(rec)
    loaded = book.load()
    assert len(loaded) == 1 and loaded[0].order_id == "0xorder1"
    assert rec.canonical in book.keys()  # noqa: SIM118
    assert "nba-lal-den-2026-11-25|denver" in book.keys()  # noqa: SIM118


def test_corrupt_book_fails_loud(tmp_path: Path) -> None:
    path = tmp_path / "resting.json"
    path.write_text("{not json")
    with pytest.raises(ValueError, match="unreadable resting order book"):
        RestingBook(path).load()


# ── reconcile ───────────────────────────────────────────────────────────
@pytest.mark.asyncio
async def test_unfilled_order_is_held_outside_the_window(tmp_path: Path) -> None:
    book = RestingBook(tmp_path / "r.json")
    book.add(_order())  # starts in 45 min, window 30 -> keep
    gw = FakeGateway({"0xorder1": _live()})
    convex = FakeConvex()
    res = await reconcile_resting_orders(book, _cfg(tmp_path), gateway=gw, convex=convex, now=NOW)
    assert [r.action for r in res] == ["held"]
    assert gw.cancelled == []
    assert convex.released == [] and convex.completed == []
    assert len(book.load()) == 1


@pytest.mark.asyncio
async def test_unfilled_order_is_cancelled_inside_the_window(tmp_path: Path) -> None:
    book = RestingBook(tmp_path / "r.json")
    book.add(_order())
    gw = FakeGateway({"0xorder1": _live()})
    convex = FakeConvex()
    later = NOW + timedelta(minutes=30)  # "4:15", game at 4:30
    res = await reconcile_resting_orders(book, _cfg(tmp_path), gateway=gw, convex=convex, now=later)
    assert [r.action for r in res] == ["cancelled"]
    assert gw.cancelled == ["0xorder1"]
    assert convex.released == ["sharp|NBA|2026-11-25|LAL|DEN|moneyline|DEN", "slug|den"]
    assert convex.completed == []
    assert book.load() == []
    assert not (tmp_path / "sharp_trades.jsonl").exists()  # nothing filled, nothing deduped


@pytest.mark.asyncio
async def test_full_fill_logs_maker_fill_with_zero_fee(tmp_path: Path) -> None:
    book = RestingBook(tmp_path / "r.json")
    book.add(_order())
    gw = FakeGateway(
        {"0xorder1": _live(50.0, 50.0, "matched")},
        trades=[_maker_trade("0xorder1", 50.0)],
    )
    convex = FakeConvex()
    res = await reconcile_resting_orders(book, _cfg(tmp_path), gateway=gw, convex=convex, now=NOW)
    assert [r.action for r in res] == ["filled"]
    assert gw.cancelled == []
    key, payload, kw = convex.completed[0]
    assert kw["shares"] == pytest.approx(50.0) and kw["buy_price"] == pytest.approx(0.50)
    assert payload["maker"] is True and payload["fee"] == 0.0 and payload["partial"] is False

    fills = [json.loads(x) for x in (tmp_path / "maker_fills.jsonl").read_text().splitlines()]
    assert len(fills) == 1
    assert fills[0]["role"] == "maker"
    assert fills[0]["fee"] == 0.0
    assert fills[0]["price"] == pytest.approx(0.50)
    assert fills[0]["size"] == pytest.approx(50.0)
    deduped = [json.loads(x) for x in (tmp_path / "sharp_trades.jsonl").read_text().splitlines()]
    assert deduped[0]["key"] == key
    assert book.load() == []


@pytest.mark.asyncio
async def test_partial_fill_only_cancels_unfilled_remainder(tmp_path: Path) -> None:
    book = RestingBook(tmp_path / "r.json")
    book.add(_order())
    gw = FakeGateway(
        {"0xorder1": _live(20.0, 50.0)},
        trades=[_maker_trade("0xorder1", 20.0)],
    )
    convex = FakeConvex()
    later = NOW + timedelta(minutes=30)
    res = await reconcile_resting_orders(book, _cfg(tmp_path), gateway=gw, convex=convex, now=later)
    assert [r.action for r in res] == ["partial"]
    assert gw.cancelled == ["0xorder1"]
    _key, payload, kw = convex.completed[0]
    assert kw["shares"] == pytest.approx(20.0)  # only what filled is recorded
    assert payload["partial"] is True and payload["order_size"] == 50.0
    assert convex.released == []  # the claim stays: we hold a position
    assert book.load() == []


@pytest.mark.asyncio
async def test_partial_fill_is_tracked_across_runs_while_held(tmp_path: Path) -> None:
    book = RestingBook(tmp_path / "r.json")
    book.add(_order())
    gw = FakeGateway({"0xorder1": _live(10.0, 50.0)}, trades=[_maker_trade("0xorder1", 10.0, tid="t1")])
    convex = FakeConvex()
    res = await reconcile_resting_orders(book, _cfg(tmp_path), gateway=gw, convex=convex, now=NOW)
    assert res[0].action == "held"
    held = book.load()[0]
    assert held.filled == pytest.approx(10.0) and held.logged_size == pytest.approx(10.0)

    gw.orders["0xorder1"] = _live(35.0, 50.0)
    gw.trades = [_maker_trade("0xorder1", 10.0, tid="t1"), _maker_trade("0xorder1", 25.0, tid="t2")]
    await reconcile_resting_orders(book, _cfg(tmp_path), gateway=gw, convex=convex, now=NOW)
    rows = [json.loads(x) for x in (tmp_path / "maker_fills.jsonl").read_text().splitlines()]
    assert [r["size"] for r in rows] == [10.0, 25.0]  # t1 is not logged twice
    assert book.load()[0].filled == pytest.approx(35.0)
    assert convex.completed == []


@pytest.mark.asyncio
async def test_cancel_failure_keeps_the_record_and_retries(tmp_path: Path) -> None:
    book = RestingBook(tmp_path / "r.json")
    book.add(_order())
    gw = FakeGateway({"0xorder1": _live()})
    gw.cancel_ok = False
    convex = FakeConvex()
    later = NOW + timedelta(minutes=30)
    res = await reconcile_resting_orders(book, _cfg(tmp_path), gateway=gw, convex=convex, now=later)
    assert res[0].action == "cancel_failed"
    assert convex.released == [] and convex.completed == []
    kept = book.load()
    assert len(kept) == 1 and "cancel failed" in (kept[0].last_error or "")

    gw.cancel_ok = True  # next run succeeds
    res = await reconcile_resting_orders(book, _cfg(tmp_path), gateway=gw, convex=convex, now=later)
    assert res[0].action == "cancelled"
    assert book.load() == []


@pytest.mark.asyncio
async def test_unreadable_state_is_held_outside_window_and_cancelled_inside(tmp_path: Path) -> None:
    book = RestingBook(tmp_path / "r.json")
    book.add(_order())
    gw = FakeGateway({})  # get_order returns None -> unknown
    res = await reconcile_resting_orders(book, _cfg(tmp_path), gateway=gw, convex=FakeConvex(), now=NOW)
    assert res[0].action == "held" and gw.cancelled == []

    gw.orders["0xorder1"] = _live()
    gw2 = FakeGateway({})

    async def _cancel(order_id: str):
        gw2.cancelled.append(order_id)
        return True, None

    gw2.cancel_order = _cancel  # type: ignore[method-assign]
    later = NOW + timedelta(minutes=40)
    res = await reconcile_resting_orders(
        book, _cfg(tmp_path), gateway=gw2, convex=FakeConvex(), now=later
    )
    assert gw2.cancelled == ["0xorder1"]  # still attempts a cancel when unsure
    assert res[0].action == "cancelled"


@pytest.mark.asyncio
async def test_started_game_with_no_start_time_is_cancelled(tmp_path: Path) -> None:
    book = RestingBook(tmp_path / "r.json")
    book.add(_order(start_time=None))
    gw = FakeGateway({"0xorder1": _live()})
    res = await reconcile_resting_orders(book, _cfg(tmp_path), gateway=gw, convex=FakeConvex(), now=NOW)
    assert res[0].action == "cancelled" and gw.cancelled == ["0xorder1"]


@pytest.mark.asyncio
async def test_dry_run_reports_without_touching_anything(tmp_path: Path) -> None:
    book = RestingBook(tmp_path / "r.json")
    book.add(_order(start_time=_iso(NOW + timedelta(minutes=10))))
    res = await reconcile_resting_orders(book, _cfg(tmp_path), now=NOW, dry_run=True)
    assert res[0].action == "would_cancel"
    assert len(book.load()) == 1


@pytest.mark.asyncio
async def test_one_failing_order_does_not_strand_the_rest(tmp_path: Path) -> None:
    class Boom(FakeGateway):
        async def get_order(self, order_id: str):
            if order_id == "bad":
                raise RuntimeError("api down")
            return await super().get_order(order_id)

    book = RestingBook(tmp_path / "r.json")
    book.add(_order(order_id="bad", canonical="k-bad"))
    book.add(_order(order_id="good", canonical="k-good"))
    gw = Boom({"good": _live()})
    later = NOW + timedelta(minutes=40)
    res = await reconcile_resting_orders(book, _cfg(tmp_path), gateway=gw, convex=FakeConvex(), now=later)
    assert sorted(r.action for r in res) == ["cancelled", "error"]
    assert [o.order_id for o in book.load()] == ["bad"]


# ── kalshi ──────────────────────────────────────────────────────────────
class FakeKalshi:
    def __init__(self, order: dict[str, Any], reduced_by: str = "10.00") -> None:
        self.order = order
        self.reduced_by = reduced_by
        self.cancelled: list[tuple[str, str]] = []

    def get_order(self, order_id: str) -> dict[str, Any]:
        return self.order

    def cancel_order(self, order_id: str, market_ticker: str) -> dict[str, Any]:
        self.cancelled.append((order_id, market_ticker))
        return {"order_id": order_id, "reduced_by": self.reduced_by}


def _k_order(fill: str, remaining: str, status: str = "resting", maker_fee: str = "0.0000") -> dict[str, Any]:
    return {
        "status": status,
        "fill_count_fp": fill,
        "remaining_count_fp": remaining,
        "initial_count_fp": "50.00",
        "maker_fees_dollars": maker_fee,
        "taker_fees_dollars": "0.0000",
        "maker_fill_cost_dollars": "0",
        "taker_fill_cost_dollars": "0",
    }


@pytest.mark.asyncio
async def test_kalshi_partial_cancel_uses_remaining_and_logs_maker_fee(tmp_path: Path) -> None:
    book = RestingBook(tmp_path / "r.json")
    book.add(
        _order(
            venue="kalshi",
            order_id="k1",
            ticker="KXNBAGAME-26NOV25LALDEN-DEN",
            kalshi_side="yes",
            token_id=None,
            size=50.0,
        )
    )
    client = FakeKalshi(_k_order("40.00", "10.00", maker_fee="0.1800"), reduced_by="10.00")
    convex = FakeConvex()
    later = NOW + timedelta(minutes=30)
    res = await reconcile_resting_orders(
        book, _cfg(tmp_path), kalshi_client=client, convex=convex, now=later
    )
    assert [r.action for r in res] == ["partial"]
    assert client.cancelled == [("k1", "KXNBAGAME-26NOV25LALDEN-DEN")]
    _key, payload, kw = convex.completed[0]
    assert kw["shares"] == pytest.approx(40.0)
    assert payload["fee"] == pytest.approx(0.18)
    rows = [json.loads(x) for x in (tmp_path / "maker_fills.jsonl").read_text().splitlines()]
    assert rows[0]["role"] == "maker" and rows[0]["size"] == pytest.approx(40.0)
    assert rows[0]["fee"] == pytest.approx(0.18)


@pytest.mark.asyncio
async def test_kalshi_full_fill_without_cancel(tmp_path: Path) -> None:
    book = RestingBook(tmp_path / "r.json")
    book.add(_order(venue="kalshi", order_id="k1", ticker="KXA-1", size=50.0, token_id=None))
    client = FakeKalshi(_k_order("50.00", "0.00", status="executed"))
    res = await reconcile_resting_orders(
        book, _cfg(tmp_path), kalshi_client=client, convex=FakeConvex(), now=NOW
    )
    assert [r.action for r in res] == ["filled"]
    assert client.cancelled == []


# ── fill parsing ────────────────────────────────────────────────────────
def test_polymarket_taker_side_is_flagged_with_a_fee() -> None:
    row = {
        "id": "t9",
        "status": "MATCHED",
        "taker_order_id": "mine",
        "price": "0.50",
        "size": "100",
        "maker_orders": [{"order_id": "other", "matched_amount": "100", "price": "0.50"}],
    }
    [fill] = polymarket_fills("mine", [row])
    assert fill["role"] == "taker"
    assert fill["fee"] == pytest.approx(1.25)  # 100 * 0.05 * 0.25

    [maker] = polymarket_fills("other", [row])
    assert maker["role"] == "maker" and maker["fee"] == 0.0


def test_polymarket_fills_skip_failed_and_logged_trades() -> None:
    rows = [
        {**_maker_trade("o", 5.0, tid="a"), "status": "FAILED"},
        _maker_trade("o", 5.0, tid="b"),
        _maker_trade("o", 5.0, tid="c"),
    ]
    out = polymarket_fills("o", rows, skip_ids={"c"})
    assert [f["trade_id"] for f in out] == ["b"]


def test_kalshi_taker_fee_is_reported_as_a_taker_fill() -> None:
    order = _k_order("10.00", "0.00", status="executed")
    order["taker_fees_dollars"] = "0.1700"
    order["taker_fill_cost_dollars"] = "5.1000"
    fill = kalshi_fill_delta(
        order, logged_size=0.0, logged_fee=0.0, logged_taker_cost=0.0, limit_price=0.50
    )
    assert fill is not None
    assert fill["role"] == "taker" and fill["fee"] == pytest.approx(0.17)
    assert fill["price"] == pytest.approx(0.51)


def test_taker_fill_logs_a_warning(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    from polymaker.trading.maker_fills import log_fill

    log_fill(
        tmp_path / "f.jsonl",
        {"price": 0.5, "size": 10.0, "fee": 0.12, "role": "taker"},
        venue="polymarket",
        order_id="abc",
        key="k",
        alert=False,
    )
    assert "TAKER_FILL" in capsys.readouterr().out
    log_fill(
        tmp_path / "f.jsonl",
        {"price": 0.5, "size": 10.0, "fee": 0.0, "role": "maker"},
        venue="polymarket",
        order_id="abc",
        key="k",
        alert=False,
    )
    assert "TAKER_FILL" not in capsys.readouterr().out
