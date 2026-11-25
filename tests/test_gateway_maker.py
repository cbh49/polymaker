"""Gateway and Kalshi client plumbing for resting orders."""

from __future__ import annotations

from typing import Any

import pytest

from ev_trading.fair_value.kalshi_execute import send_kalshi_maker
from ev_trading.venues.kalshi.api import KalshiClient
from polymaker.config import Config
from polymaker.execution.gateway import ExecutionGateway, _parse_post_order


class FakeClob:
    def __init__(self, post_resp: Any = None, cancel_resp: Any = None) -> None:
        self.post_resp = post_resp if post_resp is not None else {
            "success": True, "orderID": "0xlive", "status": "live",
        }
        self.cancel_resp = cancel_resp
        self.created: list[Any] = []
        self.posted: list[tuple[Any, Any, bool]] = []

    def create_order(self, args: Any, options: Any = None) -> str:
        self.created.append(args)
        return "signed"

    def post_order(self, order: Any, order_type: Any = "GTC", post_only: bool = False) -> Any:
        self.posted.append((order, order_type, post_only))
        return self.post_resp

    def cancel_orders(self, ids: list[str]) -> Any:
        return self.cancel_resp

    def get_order(self, order_id: str) -> Any:
        return {"status": "LIVE", "original_size": "50", "size_matched": "12.5", "price": "0.5"}


def _gw(client: FakeClob, meta) -> ExecutionGateway:
    gw = ExecutionGateway(Config(), paper=False)
    gw._client = client
    return gw


@pytest.mark.asyncio
async def test_place_limit_buy_is_gtc_post_only(meta) -> None:
    client = FakeClob()
    gw = _gw(client, meta)
    out = await gw.place_limit_buy("tok", 0.50, 50.0, meta)
    assert out["order_id"] == "0xlive" and out["error"] is None
    [(_, order_type, post_only)] = client.posted
    assert order_type == "GTC" and post_only is True
    args = client.created[0]
    assert args.price == 0.5 and args.size == 50.0 and args.side == "BUY"


@pytest.mark.asyncio
async def test_place_limit_buy_surfaces_post_only_rejection(meta) -> None:
    client = FakeClob(post_resp={"success": False, "errorMsg": "order would cross the book"})
    gw = _gw(client, meta)
    out = await gw.place_limit_buy("tok", 0.50, 50.0, meta)
    assert out["order_id"] is None
    assert "cross" in out["error"]


@pytest.mark.asyncio
async def test_place_limit_buy_catches_client_exceptions(meta) -> None:
    class Boom(FakeClob):
        def post_order(self, *a: Any, **k: Any) -> Any:
            raise RuntimeError("network down")

    out = await _gw(Boom(), meta).place_limit_buy("tok", 0.50, 5.0, meta)
    assert out["error"] == "network down"


def test_parse_post_order_variants() -> None:
    assert _parse_post_order({"orderID": "a", "status": "LIVE"})["status"] == "live"
    assert _parse_post_order({"status": "live"})["error"] == "no order id in response"
    assert _parse_post_order("nope")["error"]


@pytest.mark.asyncio
async def test_cancel_order_reads_the_per_order_result(meta) -> None:
    ok = await _gw(FakeClob(cancel_resp={"canceled": ["o1"], "not_canceled": {}}), meta).cancel_order("o1")
    assert ok == (True, None)
    bad = await _gw(
        FakeClob(cancel_resp={"canceled": [], "not_canceled": {"o1": "order already matched"}}), meta
    ).cancel_order("o1")
    assert bad == (False, "order already matched")
    empty = await _gw(FakeClob(cancel_resp={"canceled": [], "not_canceled": {}}), meta).cancel_order("o1")
    assert empty[0] is False


@pytest.mark.asyncio
async def test_get_order_normalizes_state(meta) -> None:
    state = await _gw(FakeClob(), meta).get_order("o1")
    assert state is not None
    assert state["status"] == "live" and state["size_matched"] == 12.5 and state["original_size"] == 50.0


def test_kalshi_create_order_sends_post_only_and_expiry(monkeypatch) -> None:
    client = KalshiClient(api_key_id="k", private_key_path=None)
    sent: dict[str, Any] = {}
    monkeypatch.setattr(client, "post", lambda path, body: sent.update(path=path, body=body) or {})
    client.create_event_order(
        ticker="KXA-1", side="bid", count="50.00", price="0.5000",
        time_in_force="good_till_canceled", post_only=True, expiration_time=1_800_000_000,
    )
    assert sent["path"] == "/portfolio/events/orders"
    assert sent["body"]["post_only"] is True and sent["body"]["expiration_time"] == 1_800_000_000
    client.create_event_order(ticker="KXA-1", side="bid", count="1", price="0.5")
    assert "post_only" not in sent["body"]


def test_kalshi_cancel_and_get_order_routes(monkeypatch) -> None:
    client = KalshiClient(api_key_id="k", private_key_path=None)
    calls: list[tuple[str, str, dict[str, Any]]] = []

    def fake_request(method: str, path: str, **kw: Any) -> dict[str, Any]:
        calls.append((method, path, kw))
        return {"order": {"status": "resting"}} if method == "GET" else {"reduced_by": "5.00"}

    monkeypatch.setattr(client, "request", fake_request)
    assert client.get_order("o1")["status"] == "resting"
    assert client.cancel_order("o1", "KXA-1")["reduced_by"] == "5.00"
    assert calls[0][:2] == ("GET", "/portfolio/orders/o1")
    method, path, kw = calls[1]
    assert (method, path) == ("DELETE", "/portfolio/events/orders/o1")
    assert kw["params"] == {"market_ticker": "KXA-1"} and kw["auth"] is True


class RecordingKalshi:
    def __init__(self) -> None:
        self.kw: dict[str, Any] = {}

    def get_market(self, ticker: str) -> dict[str, Any]:  # pragma: no cover
        return {}

    def create_event_order(self, **kw: Any) -> dict[str, Any]:
        self.kw = kw
        return {"order_id": "ord-1", "fill_count": "0.00", "remaining_count": kw["count"]}


def test_send_kalshi_maker_never_uses_ioc_and_maps_no_side() -> None:
    client = RecordingKalshi()
    out = send_kalshi_maker(client, ticker="KXA-1", side="yes", limit_price=0.50, contracts=50)
    assert client.kw["time_in_force"] == "good_till_canceled"
    assert client.kw["post_only"] is True
    assert client.kw["side"] == "bid" and client.kw["price"] == "0.5000"
    assert client.kw["count"] == "50.00"
    assert out["order_id"] == "ord-1" and out["fill_count"] == 0.0

    # Buying NO at 47c rests as a YES-book ask at 53c, above the YES bid.
    send_kalshi_maker(client, ticker="KXA-1", side="no", limit_price=0.47, contracts=10)
    assert client.kw["side"] == "ask" and client.kw["price"] == "0.5300"

    with pytest.raises(ValueError):
        send_kalshi_maker(client, ticker="KXA-1", side="yes", limit_price=0.5, contracts=0)
