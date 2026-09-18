"""NFL EV trade ledger: Convex claim/complete plus fee-adjusted edge filter.

Canonical tradeKey is `nfl-ev|{venue}|{market_id}|{side}`. Polymarket also
claims `{slug}|{outcome}` as a lock-only row so sharp/monitor cannot buy the
same CLOB token.
"""

from __future__ import annotations

from typing import Any, Protocol

from polymaker.trading.convex_trades import ClaimResult, prediction_date_today, trade_key

DEFAULT_MIN_EDGE_PCT = 5.0
NFL_EV_SOURCE = "nfl-ev"
NFL_EV_LEAGUE = "nfl"


class TradeLedger(Protocol):
    configured: bool

    def claim(
        self,
        *,
        trade_key_value: str,
        league: str,
        source: str,
        matchup: str,
        side: str,
        usd: float,
        prediction_date: str,
        slug: str | None = None,
        condition_id: str | None = None,
        venue: str | None = None,
        payload: dict[str, Any] | None = None,
    ) -> ClaimResult: ...

    def complete(
        self,
        trade_key_value: str,
        payload: dict[str, Any],
        *,
        token_id: str | None = None,
        start_time: int | None = None,
        buy_price: float | None = None,
        shares: float | None = None,
        venue: str | None = None,
    ) -> None: ...

    def release(self, trade_key_value: str) -> None: ...


def nfl_ev_trade_key(venue: str, market_id: str, side: str) -> str:
    return f"nfl-ev|{venue}|{market_id.strip()}|{(side or '').strip().lower()}"


def venues_from_trade_arg(trade: str | None) -> tuple[str, ...]:
    if trade == "both":
        return ("kalshi", "polymarket")
    if trade in {"kalshi", "polymarket"}:
        return (trade,)
    return ()


def poly_slug_lock_key(slug: str | None, outcome: str | None) -> str | None:
    if not slug or not outcome:
        return None
    return trade_key(slug, outcome)


def fee_adjusted_edge(row: dict[str, Any]) -> float | None:
    try:
        if row.get("fee_adjusted_edge") is not None:
            return float(row["fee_adjusted_edge"])
        if row.get("edge_pct") is not None:
            return float(row["edge_pct"]) / 100.0
    except (TypeError, ValueError):
        return None
    return None


def meets_min_edge(row: dict[str, Any], min_edge_pct: float) -> bool:
    edge = fee_adjusted_edge(row)
    if edge is None:
        return False
    return edge >= (min_edge_pct / 100.0)


def resolve_ledger(configured: TradeLedger | None, *, dry_run: bool) -> TradeLedger | None:
    if dry_run:
        return None
    if configured is not None:
        return configured
    from polymaker.trading.convex_trades import ConvexTradeClient

    return ConvexTradeClient()


def live_ledger_block(ledger: TradeLedger | None) -> str | None:
    if ledger is None or not getattr(ledger, "configured", False):
        return "convex unavailable (fail closed)"
    return None


def claim_nfl_ev(
    ledger: TradeLedger,
    *,
    venue: str,
    market_id: str,
    side: str,
    matchup: str,
    usd: float,
    payload: dict[str, Any],
    slug: str | None = None,
    condition_id: str | None = None,
    slug_lock_key: str | None = None,
) -> tuple[list[str], str | None]:
    """Claim the canonical NFL EV key, then an optional Polymarket slug lock."""
    canonical = nfl_ev_trade_key(venue, market_id, side)
    claim = ledger.claim(
        trade_key_value=canonical,
        league=NFL_EV_LEAGUE,
        source=NFL_EV_SOURCE,
        matchup=matchup,
        side=side,
        usd=usd,
        prediction_date=prediction_date_today(),
        slug=slug,
        condition_id=condition_id,
        venue=venue,
        payload=payload,
    )
    if not claim.claimed:
        return [], claim.detail
    claimed = [canonical]
    if slug_lock_key and slug_lock_key != canonical:
        lock = ledger.claim(
            trade_key_value=slug_lock_key,
            league=NFL_EV_LEAGUE,
            source=NFL_EV_SOURCE,
            matchup=matchup,
            side=side,
            usd=0.0,
            prediction_date=prediction_date_today(),
            slug=slug,
            condition_id=condition_id,
            venue=venue,
            payload={**payload, "lockOnly": True},
        )
        if not lock.claimed:
            ledger.release(canonical)
            return [], lock.detail
        claimed.append(slug_lock_key)
    return claimed, None


def release_keys(ledger: TradeLedger | None, keys: list[str]) -> None:
    if ledger is None:
        return
    for key in keys:
        ledger.release(key)
