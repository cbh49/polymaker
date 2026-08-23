"""Core domain types for Polymarket catalog + trading."""

from __future__ import annotations

import math
import time
from dataclasses import dataclass, field
from enum import Enum


class Side(str, Enum):
    """Order side. Values match the CLOB API's string form."""

    BUY = "BUY"
    SELL = "SELL"

    @property
    def opposite(self) -> Side:
        return Side.SELL if self is Side.BUY else Side.BUY


class OrderState(str, Enum):
    """Lifecycle of one of our orders."""

    DRAFT = "DRAFT"
    POSTED = "POSTED"
    LIVE = "LIVE"
    PARTIALLY_FILLED = "PARTIALLY_FILLED"
    CANCELED = "CANCELED"
    REJECTED = "REJECTED"
    DONE = "DONE"


@dataclass(frozen=True, slots=True)
class TokenMeta:
    token_id: str
    outcome: str  # e.g. "Yes" / "No" / team name


@dataclass(frozen=True, slots=True)
class MarketMeta:
    """Static-ish metadata for a tradable market, sourced from Gamma/CLOB."""

    condition_id: str
    question: str
    slug: str
    tokens: tuple[TokenMeta, TokenMeta]
    tick_size: float
    neg_risk: bool
    min_order_size: float
    rewards_min_size: float
    rewards_max_spread: float
    rewards_daily_rate: float
    maker_fee_bps: int
    taker_fee_bps: int
    fees_enabled: bool
    end_date_iso: str | None
    event_id: str | None
    rebate_rate: float = 0.0
    best_bid: float = 0.0
    best_ask: float = 0.0
    liquidity_num: float = 0.0
    volume_num: float = 0.0
    volume_24hr: float = 0.0
    one_hour_price_change: float = 0.0
    one_day_price_change: float = 0.0
    start_time_iso: str | None = None  # Gamma event.startTime (game tip-off, UTC)

    @property
    def yes(self) -> TokenMeta:
        return self.tokens[0]

    @property
    def no(self) -> TokenMeta:
        return self.tokens[1]

    def token_for_outcome(self, outcome: str) -> TokenMeta:
        """Resolve 'yes'/'no' or an outcome label to a token."""
        key = outcome.strip().lower()
        if key in ("yes", "y", "0"):
            return self.yes
        if key in ("no", "n", "1"):
            return self.no
        for t in self.tokens:
            if t.outcome.lower() == key:
                return t
        raise ValueError(f"unknown outcome {outcome!r}; tokens={[t.outcome for t in self.tokens]}")

    def other_token(self, token_id: str) -> str:
        a, b = self.tokens
        return b.token_id if token_id == a.token_id else a.token_id

    @property
    def price_decimals(self) -> int:
        s = f"{self.tick_size:f}".rstrip("0")
        return len(s.split(".")[1]) if "." in s else 0


@dataclass(slots=True)
class Position:
    token_id: str
    size: float = 0.0
    avg_price: float = 0.0

    @property
    def is_flat(self) -> bool:
        return self.size <= 0.0


@dataclass(slots=True)
class OpenOrder:
    """One of our resting orders as we currently believe it exists."""

    order_id: str
    token_id: str
    side: Side
    price: float
    size: float
    state: OrderState = OrderState.LIVE
    created_ts: float = field(default_factory=time.time)

    @property
    def notional(self) -> float:
        return self.price * self.size


@dataclass(frozen=True, slots=True)
class Quote:
    """One intended order to place."""

    token_id: str
    side: Side
    price: float
    size: float


def round_to_tick(price: float, tick: float, decimals: int, *, up: bool = False) -> float:
    """Round a price to the market tick."""
    if tick <= 0:
        return round(price, decimals)
    n = price / tick
    stepped = math.ceil(n - 1e-12) if up else math.floor(n + 1e-12)
    return round(stepped * tick, decimals)
