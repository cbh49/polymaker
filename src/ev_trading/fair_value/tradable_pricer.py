"""Price Kalshi and Polymarket contracts against sportsbook-implied fair probs."""

from __future__ import annotations

from decimal import ROUND_CEILING, Decimal
from typing import Any

from ev_trading.fair_value.config import FairValueConfig, KalshiFeeConfig, PolymarketFeeConfig
from ev_trading.fair_value.models import PricedOpportunity, SigmaSource, VenueName


def kalshi_taker_fee(
    price: float,
    *,
    contracts: int = 1,
    cfg: KalshiFeeConfig | None = None,
) -> float:
    """Kalshi taker fee: round_up(M × 0.07 × C × P × (1 − P)) to a centicent ($0.0001).

    July 2026 general schedule. Fee is highest near 50¢ and symmetric in P and 1−P.
    """
    fee_cfg = cfg or KalshiFeeConfig()
    p = Decimal(str(min(max(float(price), 0.0), 1.0)))
    raw = (
        Decimal(str(fee_cfg.multiplier))
        * Decimal(str(fee_cfg.taker_rate))
        * Decimal(int(contracts))
        * p
        * (Decimal("1") - p)
    )
    return float(raw.quantize(Decimal("0.0001"), rounding=ROUND_CEILING))


def kalshi_fee_per_contract(price: float, *, cfg: KalshiFeeConfig | None = None) -> float:
    fee_cfg = cfg or KalshiFeeConfig()
    n = max(int(fee_cfg.contracts), 1)
    return kalshi_taker_fee(price, contracts=n, cfg=fee_cfg) / n


def polymarket_fee_per_contract(
    price: float,
    *,
    cfg: PolymarketFeeConfig | None = None,
) -> float:
    fee_cfg = cfg or PolymarketFeeConfig()
    taker = float(price) * (fee_cfg.taker_bps / 10_000.0)
    n = max(int(fee_cfg.assumed_contracts), 1)
    gas = fee_cfg.gas_usd / n
    return taker + gas


# Shown odds use a flat 1% of the contract price. Trading still uses
# polymarket_fee_per_contract (taker bps + gas).
POLYMARKET_ODDS_FEE = Decimal("0.01")


def venue_traded_price(
    price: float,
    venue: str,
    *,
    cfg: FairValueConfig | None = None,
) -> float:
    """All-in price paid to buy, before that price is turned into American odds.

    Kalshi's taker fee is added, then the cash cost is rounded up to the next
    cent (a 46¢ ask trades at 48¢). Polymarket adds 1% of the price.
    """
    px = Decimal(str(min(max(float(price), 0.0), 1.0)))
    name = (venue or "").strip().lower()
    if name == "kalshi":
        fee_cfg = cfg.kalshi_fee if cfg is not None else None
        fee = Decimal(str(kalshi_fee_per_contract(float(px), cfg=fee_cfg)))
        traded = (px + fee).quantize(Decimal("0.01"), rounding=ROUND_CEILING)
    elif name == "polymarket":
        traded = px * (Decimal("1") + POLYMARKET_ODDS_FEE)
    else:
        return float(px)
    if traded <= 0:
        return float(px)
    if traded >= 1:
        return 0.9999
    return float(traded)


def _edge_block(fair_prob: float, market_price: float, fee: float) -> dict[str, float]:
    raw = fair_prob - market_price
    cost = market_price + fee
    fee_adj = fair_prob - cost
    ev = fair_prob * 1.0 - cost
    return {
        "raw_edge": raw,
        "fee_adjusted_edge": fee_adj,
        "expected_value_per_contract": ev,
    }


def price_side(
    *,
    fair_prob: float,
    market_price: float,
    venue: VenueName,
    cfg: FairValueConfig | None = None,
) -> dict[str, float]:
    """Compare fair P(side) to the actual ask. Never uses the bid/ask midpoint."""
    cfg = cfg or FairValueConfig()
    px = min(max(float(market_price), 0.0), 1.0)
    fp = min(max(float(fair_prob), 0.0), 1.0)
    if venue == "kalshi":
        fee = kalshi_fee_per_contract(px, cfg=cfg.kalshi_fee)
    elif venue == "polymarket":
        fee = polymarket_fee_per_contract(px, cfg=cfg.polymarket_fee)
    else:
        raise ValueError(f"unknown venue: {venue}")
    return _edge_block(fp, px, fee)


def opportunity(
    *,
    market: str,
    matchup: str,
    stat: str,
    venue: VenueName,
    side: str,
    fair_prob: float,
    market_price: float,
    cfg: FairValueConfig,
    player: str | None = None,
    fair_line: float | None = None,
    market_line: float | None = None,
    volume: float | None = None,
    liquidity: float | None = None,
    volume_24hr: float | None = None,
    line_delta: float | None = None,
    low_liquidity: bool = False,
    low_confidence: bool = False,
    n_books: int = 0,
    fit_r2: float | None = None,
    market_id: str | None = None,
    ticker: str | None = None,
    confidence: float = 0.0,
    is_extrapolation: bool | None = None,
    extrapolation_distance: float | None = None,
    sigma_source: SigmaSource | None = None,
    role_bucket: str | None = None,
    veto_reason: str | None = None,
) -> PricedOpportunity:
    edges = price_side(fair_prob=fair_prob, market_price=market_price, venue=venue, cfg=cfg)
    rank = edges["fee_adjusted_edge"] * confidence
    return PricedOpportunity(
        market=market,
        matchup=matchup,
        player=player,
        stat=stat,
        venue=venue,
        side=side,
        fair_prob=fair_prob,
        fair_line=fair_line,
        market_line=market_line,
        market_price=market_price,
        raw_edge=edges["raw_edge"],
        fee_adjusted_edge=edges["fee_adjusted_edge"],
        expected_value_per_contract=edges["expected_value_per_contract"],
        confidence=confidence,
        volume=volume,
        liquidity=liquidity,
        volume_24hr=volume_24hr,
        line_delta=line_delta,
        low_liquidity=low_liquidity,
        low_confidence=low_confidence,
        n_books=n_books,
        fit_r2=fit_r2,
        market_id=market_id,
        ticker=ticker,
        rank_score=rank,
        is_extrapolation=is_extrapolation,
        extrapolation_distance=extrapolation_distance,
        sigma_source=sigma_source,
        role_bucket=role_bucket,
        veto_reason=veto_reason,
    )


def kalshi_no_ask(raw: dict[str, Any]) -> float | None:
    """Best price to buy NO. Synthesize from yes_bid when the feed omits it."""
    no_ask = _kalshi_px(raw, "no_ask_dollars", "no_ask")
    if no_ask is not None:
        return no_ask
    yes_bid = _kalshi_px(raw, "yes_bid_dollars", "yes_bid")
    if yes_bid is not None:
        return max(0.0, min(1.0, round(1.0 - yes_bid, 4)))
    return None


def kalshi_yes_ask(raw: dict[str, Any]) -> float | None:
    """Best price to buy YES. Live GET /markets uses yes_ask_dollars, not yes_ask."""
    return _kalshi_px(raw, "yes_ask_dollars", "yes_ask", "implied_prob")


def _kalshi_px(raw: dict[str, Any], *keys: str) -> float | None:
    """Read a Kalshi price as a (0, 1) dollar fraction.

    The current Trade API quotes in fixed-point dollars (`yes_ask_dollars`).
    Older payloads and our scraped JSON use `yes_ask` already in dollars.
    Integer 1–99 values are treated as cents.
    """
    for key in keys:
        parsed = _f(raw.get(key))
        if parsed is None:
            continue
        if parsed > 1.0 and parsed <= 99.0:
            parsed /= 100.0
        if parsed <= 0.0 or parsed >= 1.0:
            continue
        return parsed
    return None


def _unit_price(value: Any) -> float | None:
    parsed = _f(value)
    if parsed is None or parsed <= 0.0 or parsed >= 1.0:
        return None
    return parsed


def polymarket_side_ask(raw: dict[str, Any] | None, side: str) -> float | None:
    """Taker ask for a Polymarket side. Never the Gamma outcomePrices mid.

    Nested game quotes use `{side: {ask, bid}}`. Player props use `over_ask` /
    `under_ask`. Missing asks synthesize from the complementary bid
    (`under_ask = 1 - over_bid`).
    """
    if not isinstance(raw, dict):
        return None
    key = (side or "").strip().lower()
    nested = raw.get(key)
    if isinstance(nested, dict):
        ask = _unit_price(nested.get("ask"))
        if ask is not None:
            return ask
        other = {"home": "away", "away": "home", "over": "under", "under": "over"}.get(key)
        other_blob = raw.get(other) if other else None
        if isinstance(other_blob, dict):
            other_bid = _unit_price(other_blob.get("bid"))
            if other_bid is not None:
                return _unit_price(round(1.0 - other_bid, 4))
        return None
    ask = _unit_price(raw.get(f"{key}_ask"))
    if ask is not None:
        return ask
    other_bid_key = {
        "over": "under_bid",
        "under": "over_bid",
        "yes": "no_bid",
        "no": "yes_bid",
        "home": "away_bid",
        "away": "home_bid",
    }.get(key)
    if other_bid_key:
        other_bid = _unit_price(raw.get(other_bid_key))
        if other_bid is not None:
            return _unit_price(round(1.0 - other_bid, 4))
    return None


def _f(value: Any) -> float | None:
    if value is None or value == "":
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None
