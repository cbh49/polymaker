"""Maker (resting limit) pricing, fees, and venue comparison for sharp buys.

Every sharp buy rests one tick below the best ask so it never crosses the
spread. Polymarket makers pay no fee. Kalshi makers pay
`ceil_to_cent(rate * C * p * (1 - p))` per order, unless the series is
configured as fee-free. Venues are compared on

    effective_price = p + fee / C

where `p` is that venue's limit price and `C` its contract count.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import ROUND_CEILING, ROUND_FLOOR, Decimal
from typing import Literal

from polymaker.config import KalshiMakerFeeConfig

VenueName = Literal["polymarket", "kalshi"]

DEFAULT_OFFSET = 0.01
DEFAULT_TICK = 0.01
# Effective prices this close are a tie (the tie venue wins).
TIE_EPSILON = 0.0001


def _d(value: float | str | Decimal) -> Decimal:
    return Decimal(str(value))


def floor_to_tick(price: float, tick: float = DEFAULT_TICK) -> float:
    """Round `price` down to a multiple of `tick`."""
    t = _d(tick)
    if t <= 0:
        raise ValueError(f"tick must be positive, got {tick}")
    return float((_d(price) / t).to_integral_value(rounding=ROUND_FLOOR) * t)


def maker_buy_price(
    best_ask: float | None,
    tick: float = DEFAULT_TICK,
    offset: float = DEFAULT_OFFSET,
) -> float | None:
    """Resting buy price strictly below `best_ask`, or None if none is valid.

    Default is one cent under the ask (a 51c ask rests at 50c). The result is
    tick-aligned and is stepped down another tick if rounding left it at or
    above the ask. A price at or below zero (or an empty book) returns None.
    """
    if best_ask is None:
        return None
    try:
        ask = float(best_ask)
    except (TypeError, ValueError):
        return None
    if not 0 < ask < 1:
        return None
    t = _d(tick)
    step = max(_d(offset), t)
    price = _d(floor_to_tick(float(_d(ask) - step), float(t)))
    if price >= _d(ask):
        price -= t
    if price <= 0 or price >= _d(ask):
        return None
    return float(price)


def assert_below_ask(price: float, best_ask: float) -> None:
    """Raise if `price` could fill immediately as a taker."""
    if not price < best_ask:
        raise ValueError(f"limit {price} is not strictly below best ask {best_ask}")


def polymarket_maker_fee(contracts: float, price: float) -> float:  # noqa: ARG001
    """Polymarket makers are never charged."""
    return 0.0


def kalshi_maker_fee(
    contracts: int | float,
    price: float,
    *,
    enabled: bool = True,
    rate: float = 0.0175,
) -> float:
    """`ceil_to_cent(rate * C * p * (1 - p))`, or 0 when the series has no maker fee."""
    if not enabled or contracts <= 0:
        return 0.0
    p = _d(min(max(float(price), 0.0), 1.0))
    raw = _d(rate) * _d(contracts) * p * (Decimal("1") - p)
    cents = (raw * 100).to_integral_value(rounding=ROUND_CEILING)
    return float(cents / 100)


def effective_price(price: float, fee: float, contracts: float) -> float:
    """Per-contract cost including the order's maker fee: p + fee / C."""
    if contracts <= 0:
        raise ValueError("contracts must be positive")
    return float(price) + float(fee) / float(contracts)


@dataclass(frozen=True, slots=True)
class MakerQuote:
    venue: VenueName
    ask: float
    limit_price: float
    size: float  # shares (Polymarket, 2dp) or whole contracts (Kalshi)
    fee: float  # total order fee in dollars
    effective_price: float
    usd: float  # limit_price * size

    @property
    def fee_per_contract(self) -> float:
        return self.fee / self.size if self.size else 0.0


def polymarket_size(usd: float, price: float, min_size: float = 0.0) -> float:
    """Whole cents of shares (2dp) that fit in `usd` at `price`; 0 if below `min_size`."""
    if price <= 0 or usd <= 0:
        return 0.0
    shares = float((_d(usd) / _d(price)).quantize(Decimal("0.01"), rounding=ROUND_FLOOR))
    return shares if shares >= max(min_size, 0.01) else 0.0


def kalshi_size(usd: float, price: float) -> int:
    """Whole contracts whose cost at `price` does not exceed `usd`."""
    if price <= 0 or usd <= 0:
        return 0
    return int(_d(usd) // _d(price))


def price_maker_venue(
    venue: VenueName,
    ask: float,
    usd: float,
    *,
    tick: float = DEFAULT_TICK,
    offset: float = DEFAULT_OFFSET,
    min_size: float = 0.0,
    ticker: str | None = None,
    kalshi_fees: KalshiMakerFeeConfig | None = None,
) -> tuple[MakerQuote | None, str | None]:
    """Quote a resting buy on one venue. Returns (quote, None) or (None, reason)."""
    limit = maker_buy_price(ask, tick, offset)
    if limit is None:
        return None, f"no valid maker price below ask {ask}"
    assert_below_ask(limit, ask)
    if venue == "polymarket":
        size: float = polymarket_size(usd, limit, min_size)
        if size <= 0:
            return None, f"cannot size ${usd:.2f} at {limit:.4f} (min {min_size:g} shares)"
        fee = polymarket_maker_fee(size, limit)
    elif venue == "kalshi":
        size = float(kalshi_size(usd, limit))
        if size < 1:
            return None, f"cannot size ${usd:.2f} at {limit:.4f}"
        cfg = kalshi_fees or KalshiMakerFeeConfig()
        fee = kalshi_maker_fee(
            int(size), limit, enabled=cfg.enabled_for(ticker), rate=cfg.rate
        )
    else:
        raise ValueError(f"unknown venue: {venue}")
    return (
        MakerQuote(
            venue=venue,
            ask=float(ask),
            limit_price=limit,
            size=size,
            fee=fee,
            effective_price=effective_price(limit, fee, size),
            usd=round(limit * size, 4),
        ),
        None,
    )


def pick_maker_venue(
    quotes: list[MakerQuote],
    *,
    tie_venue: VenueName = "polymarket",
    epsilon: float = TIE_EPSILON,
) -> MakerQuote | None:
    """Lowest effective price wins. Ties within `epsilon` go to `tie_venue`."""
    usable = [q for q in quotes if 0 < q.limit_price < 1 and q.size > 0]
    if not usable:
        return None
    best = min(q.effective_price for q in usable)
    near = [q for q in usable if q.effective_price - best <= epsilon]
    for q in near:
        if q.venue == tie_venue:
            return q
    return min(near, key=lambda q: q.effective_price)
