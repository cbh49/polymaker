"""Market attractiveness scoring for the scanner.

Combines the v1 reward-density intuition with the new maker-rebate income
stream and penalizes spread/extremes/gap risk. Higher score = more attractive
to make. Pure functions over MarketMeta.
"""

from __future__ import annotations

from dataclasses import dataclass

from polymaker.domain import MarketMeta


@dataclass(frozen=True, slots=True)
class MarketScore:
    condition_id: str
    reward_density: float  # est. reward $/day per $100 of two-sided liquidity
    rebate_potential: float  # est. daily rebate $ available to makers
    spread: float
    extremity: float  # 0 = mid ~0.5 (good), 1 = near 0/1 (bad payoff asymmetry)
    score: float
    # [0,1] thin/gappy/manipulable risk from trailing moves + book thinness;
    # default 0 keeps old score_json rows loadable
    gap_risk: float = 0.0


def _mid(m: MarketMeta) -> float:
    if m.best_bid > 0 and m.best_ask > 0:
        return (m.best_bid + m.best_ask) / 2.0
    return 0.5


def reward_density(m: MarketMeta, quote_size_usdc: float = 100.0) -> float:
    """Rough reward $/day if we hold ~quote_size two-sided in-band.

    The exact per-order S((v-s)/v)^2 scoring depends on live competition; for
    ranking we use daily_rate scaled by how much of the (small) market our
    typical size represents, capped. This mirrors v1's gm_reward_per_100 as a
    relative ranking signal, not an absolute forecast.
    """
    if m.rewards_daily_rate <= 0 or m.rewards_max_spread <= 0:
        return 0.0
    liq = max(m.liquidity_num, quote_size_usdc)
    our_share = min(1.0, quote_size_usdc / liq)
    return m.rewards_daily_rate * our_share


def rebate_potential(m: MarketMeta) -> float:
    """Estimated daily maker-rebate POOL for the market, using the exact V2 fee
    formula (per-market rate + rebate rate, no hardcoding).

    Per-share taker fee = fee_rate * p*(1-p)  (py_clob_client_v2/fees.py).
    Daily taker shares ~ vol_24h / mid, so:
        daily fees   = (vol/mid) * fee_rate * mid*(1-mid) = vol * fee_rate * (1-mid)
        rebate pool  = daily fees * rebate_rate
    This is the whole-market pool; your take is (your maker-fill share) x pool.
    It's a trailing-volume estimate — actual depends on future flow + fill share.
    """
    if not m.fees_enabled or m.rebate_rate <= 0 or m.taker_fee_bps <= 0:
        return 0.0
    vol24 = m.volume_24hr
    if vol24 <= 0:
        return 0.0
    fee_rate = m.taker_fee_bps / 10000.0
    mid = _mid(m)
    daily_fees = vol24 * fee_rate * (1.0 - mid)
    return round(daily_fees * m.rebate_rate, 2)


def extremity(m: MarketMeta) -> float:
    """0 near 0.5 (balanced), ->1 near the 0/1 boundary (skip these)."""
    mid = _mid(m)
    return min(1.0, abs(mid - 0.5) / 0.5)


def gap_risk(m: MarketMeta) -> float:
    """[0, 1] thin/gappy/manipulable risk — selection-time cousin of live depth_scale.

    The scanner has no L2 history, so this blends Gamma trailing price changes
    (realized gap / event moves) with book thinness relative to the reward-
    qualifying fill size and spread-in-ticks (air pockets behind the touch).
    Higher → discount harder in ``score_market`` so Romania-style books never
    rank as if they were deep.
    """
    mid = _mid(m)

    # Realized moves: 10¢/day or 5¢/hour already in "newsy / gap-prone" territory
    day = abs(m.one_day_price_change)
    hour = abs(m.one_hour_price_change)
    move_risk = min(1.0, max(day / 0.10, hour / 0.05))

    # Forced fill notional vs book: large reward-min on a thin book = one
    # gap-through leaves an oversized directional bag (TIPS.md Romania).
    fill_usdc = max(m.rewards_min_size, m.min_order_size, 1.0) * mid
    thin_risk = min(1.0, (fill_usdc * 25.0) / max(m.liquidity_num, 1.0))

    # Wide touch (in ticks) often means sparse depth behind the best.
    if m.best_bid > 0 and m.best_ask > 0 and m.tick_size > 0:
        ticks = (m.best_ask - m.best_bid) / m.tick_size
        spread_risk = min(1.0, max(0.0, (ticks - 2.0) / 10.0))
    else:
        spread_risk = 1.0  # no usable touch → treat as fragile

    return round(min(1.0, 0.45 * move_risk + 0.40 * thin_risk + 0.15 * spread_risk), 4)


def score_market(m: MarketMeta) -> MarketScore:
    rd = reward_density(m)  # our estimated reward income (share-adjusted)
    rp = rebate_potential(m)  # total daily rebate POOL (for display)
    ext = extremity(m)
    gr = gap_risk(m)
    spread = max(0.0, m.best_ask - m.best_bid) if (m.best_bid and m.best_ask) else 1.0

    # our estimated income = reward share + (rebate pool * our fill/liquidity share);
    # extremity and wide spreads discount the score
    ref = 100.0
    our_share = min(0.5, ref / max(m.liquidity_num, ref))  # you won't own a whole pool
    income = rd + rp * our_share
    penalty = (1.0 - 0.5 * ext) * (1.0 / (1.0 + spread * 20.0))
    # viability: a market needs real book depth to actually quote — otherwise a
    # near-zero-liquidity market games "our share" to the top of the ranking
    viability = min(1.0, m.liquidity_num / 2000.0)
    # stability: heavy discount for gap-prone / thin books so they rank below
    # deep quiet markets even when the reward pool looks attractive
    stability = 1.0 - 0.85 * gr
    return MarketScore(
        condition_id=m.condition_id,
        reward_density=round(rd, 3),
        rebate_potential=round(rp, 3),  # the market's total daily rebate pool
        spread=round(spread, 4),
        extremity=round(ext, 3),
        gap_risk=gr,
        score=round(income * penalty * viability * stability, 4),
    )
