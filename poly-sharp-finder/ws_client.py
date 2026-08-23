"""
Polymarket CLOB websocket client.

Subscribes to the `market` channel for a set of token_ids and pushes book
snapshots into the detector for imbalance / fast-move checks.

NOTE ON SCHEMA: the message shapes below (event_type "book" / "price_change")
match Polymarket's documented CLOB websocket format as of writing, but
websocket message schemas are exactly the kind of thing that drifts --
run this against a live connection and print raw messages once before
trusting the parsing.
"""

import asyncio
import json
import time
from typing import Callable, Dict, List

import websockets

from config import ENDPOINTS
from registry import WatchedMarket
from detector import SignalDetector


class ClobWebSocketClient:
    def __init__(self, markets: List[WatchedMarket], detector: SignalDetector,
                 on_signal: Callable):
        self.markets = markets
        self.detector = detector
        self.on_signal = on_signal
        # Only map the YES token for book/price detection. Binary NO mids are
        # ~1 - yes_mid; mixing both into one condition_id series caused fake
        # fast_move signals (e.g. 0.40 → 0.60) on every book snapshot pair.
        self._token_to_market: Dict[str, WatchedMarket] = {}
        for m in markets:
            self._token_to_market[m.yes_token_id] = m

    async def run(self):
        asset_ids = list(self._token_to_market.keys())
        backoff = 1
        while True:
            try:
                async with websockets.connect(ENDPOINTS.clob_ws, ping_interval=10) as ws:
                    await ws.send(json.dumps({
                        "type": "market",
                        "assets_ids": asset_ids,
                    }))
                    backoff = 1  # reset after a successful connect
                    async for raw_msg in ws:
                        self._handle_message(raw_msg)
            except (websockets.ConnectionClosed, OSError) as e:
                print(f"[ws_client] connection dropped ({e}), reconnecting in {backoff}s")
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, 60)

    def _handle_message(self, raw_msg: str):
        try:
            payload = json.loads(raw_msg)
        except json.JSONDecodeError:
            return

        messages = payload if isinstance(payload, list) else [payload]
        for msg in messages:
            if not isinstance(msg, dict):
                continue
            event_type = msg.get("event_type")
            asset_id = msg.get("asset_id") or msg.get("asset")
            market = self._token_to_market.get(asset_id) if asset_id else None
            if market is None:
                continue

            if event_type == "book":
                self._handle_book(market, msg)

    def _handle_book(self, market: WatchedMarket, msg: dict):
        bids = msg.get("bids", [])
        asks = msg.get("asks", [])
        if not bids or not asks:
            return

        best_bid = max(float(b["price"]) for b in bids)
        best_ask = min(float(a["price"]) for a in asks)
        bid_depth_usd = sum(float(b["price"]) * float(b["size"]) for b in bids)
        ask_depth_usd = sum(float(a["price"]) * float(a["size"]) for a in asks)

        signals = self.detector.on_book_update(
            market=market,
            best_bid=best_bid,
            best_ask=best_ask,
            bid_depth_usd=bid_depth_usd,
            ask_depth_usd=ask_depth_usd,
            ts=time.time(),
        )
        for sig in signals:
            self.on_signal(sig)
