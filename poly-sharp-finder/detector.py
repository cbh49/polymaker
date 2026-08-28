"""
Signal detector: takes raw trade / order-book events and turns them into
flagged "sharp signal" candidates, using the thresholds in config.py.

This module classifies and logs signals. Auto-trading is gated in
`executor.py` / `TradeConfig.signal_types` (whale_trade is tweet-only;
book_imbalance and fast_move are log-only by default).
"""

import time
from dataclasses import dataclass, asdict
from typing import Optional, List
from collections import defaultdict

from config import THRESHOLDS
from wallet_store import WalletStore
from registry import WatchedMarket


@dataclass
class Signal:
    ts: float
    condition_id: str
    league: str
    label: str
    signal_type: str          # "whale_trade" | "book_imbalance" | "convergence" | "fast_move"
    side: str                 # "yes" or "no"
    detail: dict
    sharp_tier: Optional[str] = None
    sharp_side: Optional[str] = None
    sharp_composite_gap: Optional[float] = None


class SignalDetector:
    def __init__(self, wallet_store: WalletStore):
        self.wallets = wallet_store
        # recent smart-money trades per market, for convergence checks
        self._recent_smart_trades: dict[str, list] = defaultdict(list)
        # recent prices per market, for fast-move detection
        self._price_history: dict[str, list] = defaultdict(list)

    # ---------- trade-level detection (from REST poller) ----------

    def on_trade(self, market: WatchedMarket, side: str, price: float,
                 size_shares: float, wallet: str, ts: float) -> List[Signal]:
        signals = []
        size_usd = price * size_shares

        self.wallets.record_trade(wallet, size_usd, ts)

        # 1. raw whale-size flag
        if size_usd >= THRESHOLDS.whale_trade_usd:
            signals.append(Signal(
                ts=ts, condition_id=market.condition_id, league=market.league,
                label=market.label, signal_type="whale_trade", side=side,
                detail={"size_usd": size_usd, "price": price, "wallet": wallet,
                        "tier": self._trade_tier(size_usd)},
                sharp_tier=market.sharp_tier, sharp_side=market.sharp_side,
                sharp_composite_gap=market.sharp_composite_gap,
            ))

        # 2. smart-money wallet check + convergence
        if self.wallets.is_smart_money(
            wallet, THRESHOLDS.min_wallet_trades_for_track_record, THRESHOLDS.min_wallet_win_rate
        ):
            self._recent_smart_trades[market.condition_id].append((ts, side, wallet))
            conv = self._check_convergence(market, side, ts)
            if conv:
                signals.append(conv)

        return signals

    def _trade_tier(self, size_usd: float) -> str:
        if size_usd >= THRESHOLDS.major_trade_usd:
            return "major"
        if size_usd >= THRESHOLDS.whale_trade_usd:
            return "whale"
        if size_usd >= THRESHOLDS.notable_trade_usd:
            return "notable"
        return "small"

    def _check_convergence(self, market: WatchedMarket, side: str, ts: float) -> Optional[Signal]:
        window_start = ts - THRESHOLDS.convergence_window_minutes * 60
        recent = [t for t in self._recent_smart_trades[market.condition_id] if t[0] >= window_start]
        same_side_wallets = {w for (t, s, w) in recent if s == side}

        if len(same_side_wallets) >= THRESHOLDS.convergence_wallet_count:
            return Signal(
                ts=ts, condition_id=market.condition_id, league=market.league,
                label=market.label, signal_type="convergence", side=side,
                detail={"wallet_count": len(same_side_wallets),
                        "wallets": list(same_side_wallets)},
                sharp_tier=market.sharp_tier, sharp_side=market.sharp_side,
                sharp_composite_gap=market.sharp_composite_gap,
            )
        return None

    # ---------- book-level detection (from websocket) ----------

    def on_book_update(self, market: WatchedMarket, best_bid: float, best_ask: float,
                        bid_depth_usd: float, ask_depth_usd: float, ts: float) -> List[Signal]:
        """Book updates must be for the YES token only (see ws_client)."""
        signals = []
        mid = (best_bid + best_ask) / 2
        spread_cents = (best_ask - best_bid) * 100

        # order book imbalance (logged; not in default TradeConfig.signal_types)
        if spread_cents <= THRESHOLDS.max_spread_cents and (
            bid_depth_usd >= THRESHOLDS.min_depth_usd_for_signal
            or ask_depth_usd >= THRESHOLDS.min_depth_usd_for_signal
        ):
            ratio = (bid_depth_usd + 1e-6) / (ask_depth_usd + 1e-6)
            if ratio >= THRESHOLDS.imbalance_ratio_alert or ratio <= 1 / THRESHOLDS.imbalance_ratio_alert:
                lean_side = "yes" if ratio > 1 else "no"
                signals.append(Signal(
                    ts=ts, condition_id=market.condition_id, league=market.league,
                    label=market.label, signal_type="book_imbalance", side=lean_side,
                    detail={"ratio": round(ratio, 2), "bid_depth_usd": bid_depth_usd,
                            "ask_depth_usd": ask_depth_usd, "spread_cents": round(spread_cents, 2)},
                    sharp_tier=market.sharp_tier, sharp_side=market.sharp_side,
                    sharp_composite_gap=market.sharp_composite_gap,
                ))

        # fast price move on YES mid only
        hist = self._price_history[market.condition_id]
        hist.append((ts, mid))
        cutoff = ts - THRESHOLDS.fast_move_window_seconds
        while hist and hist[0][0] < cutoff:
            hist.pop(0)

        if len(hist) >= 2:
            oldest_price = hist[0][1]
            move_cents = abs(mid - oldest_price) * 100
            if move_cents >= THRESHOLDS.fast_move_cents:
                signals.append(Signal(
                    ts=ts, condition_id=market.condition_id, league=market.league,
                    label=market.label, signal_type="fast_move",
                    side="yes" if mid > oldest_price else "no",
                    detail={"move_cents": round(move_cents, 2),
                            "window_seconds": THRESHOLDS.fast_move_window_seconds,
                            "from_price": oldest_price, "to_price": mid},
                    sharp_tier=market.sharp_tier, sharp_side=market.sharp_side,
                    sharp_composite_gap=market.sharp_composite_gap,
                ))

        return signals
