"""
Fast-polling loop against Polymarket's Data API /trades endpoint.

Why polling instead of websocket for this part: the CLOB websocket's
`market` channel gives you book/price updates but does not reliably carry
wallet-level trade attribution. The Data API /trades endpoint does. Polling
every few seconds is a reasonable tradeoff -- MLB/WNBA markets don't need
sub-second trade attribution the way a live order-matching engine would.

NOTE ON SCHEMA: verify the exact JSON field names against a live response
before running this for real -- Polymarket's Data API has changed field
names before (e.g. takerAmount/makerAmount vs size/price). The parsing
below (`_parse_trade`) is the one function to double check against actual
output.
"""

import asyncio
import time
from typing import Callable, List

import aiohttp

from config import ENDPOINTS
from registry import WatchedMarket
from detector import SignalDetector


class TradePoller:
    def __init__(self, session: aiohttp.ClientSession, detector: SignalDetector,
                 markets: List[WatchedMarket], on_signal: Callable,
                 poll_interval_sec: float = 3.0):
        self.session = session
        self.detector = detector
        self.markets = markets
        self.on_signal = on_signal
        self.poll_interval_sec = poll_interval_sec
        self._seen_trade_ids: set[str] = set()

    async def run(self):
        while True:
            for market in self.markets:
                try:
                    await self._poll_market(market)
                except Exception as e:
                    print(f"[trade_poller] error polling {market.label}: {e}")
            await asyncio.sleep(self.poll_interval_sec)

    async def _poll_market(self, market: WatchedMarket):
        params = {"market": market.condition_id, "limit": 50}
        async with self.session.get(ENDPOINTS.data_api_trades, params=params) as resp:
            if resp.status != 200:
                return
            trades = await resp.json()

        for raw in trades:
            trade_id = raw.get("id") or raw.get("transactionHash")
            if not trade_id or trade_id in self._seen_trade_ids:
                continue
            self._seen_trade_ids.add(trade_id)

            parsed = self._parse_trade(raw, market)
            if parsed is None:
                continue

            signals = self.detector.on_trade(
                market=market,
                side=parsed["side"],
                price=parsed["price"],
                size_shares=parsed["size"],
                wallet=parsed["wallet"],
                ts=parsed["ts"],
            )
            for sig in signals:
                self.on_signal(sig)

    def _parse_trade(self, raw: dict, market: WatchedMarket):
        """Parse Data API /trades row; field names verified via probe_feeds.py."""
        try:
            price = float(raw["price"])
            # size may be shares; some payloads use size / amount / takerAmount
            if "size" in raw:
                size = float(raw["size"])
            elif "amount" in raw:
                size = float(raw["amount"])
            else:
                return None
            wallet = (
                raw.get("proxyWallet")
                or raw.get("taker")
                or raw.get("maker")
                or raw.get("name")
                or ""
            )
            outcome = str(raw.get("outcome") or "")
            side = market.side_for_outcome(outcome)
            if side is None:
                # asset/token id fallback
                asset = str(raw.get("asset") or raw.get("asset_id") or raw.get("token_id") or "")
                if asset and asset == market.yes_token_id:
                    side = "yes"
                elif asset and asset == market.no_token_id:
                    side = "no"
                else:
                    return None
            ts_raw = raw.get("timestamp") or raw.get("match_time") or raw.get("createdAt")
            if ts_raw is None:
                ts = time.time()
            else:
                ts = float(ts_raw)
                # ms → s if needed
                if ts > 1e12:
                    ts /= 1000.0
            return {"price": price, "size": size, "wallet": wallet, "side": side, "ts": ts}
        except (KeyError, TypeError, ValueError):
            return None
