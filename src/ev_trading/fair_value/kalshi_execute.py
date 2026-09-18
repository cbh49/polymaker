"""Place Kalshi IOC buys for high-confidence tradable NFL fair-value rows.

Only the `tradable` bucket is eligible (not low-liquidity or TD yes/no lists).
A row must be `venue=kalshi` with `confidence >= min_confidence`. Stake is a
fixed USD amount per contract side (default $10), converted to whole contracts
at the ask.
"""

from __future__ import annotations

import json
import uuid
from dataclasses import dataclass, field
from decimal import ROUND_DOWN, ROUND_HALF_UP, Decimal
from pathlib import Path
from typing import Any, Protocol

from ev_trading.fair_value.models import PricedOpportunity
from ev_trading.fair_value.report import FairValueReport
from ev_trading.fair_value.tradable_pricer import kalshi_no_ask, kalshi_yes_ask

YES_SIDES = frozenset({"yes", "over", "home"})
NO_SIDES = frozenset({"no", "under", "away"})

DEFAULT_USD = 10.0
DEFAULT_MIN_CONFIDENCE = 0.80
DEFAULT_MAX_ASK_SLIPPAGE = 0.01
_BOT_ROOT = Path(__file__).resolve().parents[3]
DEFAULT_FILLED_LOG = _BOT_ROOT / "journal" / "nfl_kalshi_trades.jsonl"


class KalshiTradingClient(Protocol):
    def get_market(self, ticker: str) -> dict[str, Any]: ...

    def create_event_order(
        self,
        *,
        ticker: str,
        side: str,
        count: str,
        price: str,
        time_in_force: str = "immediate_or_cancel",
        self_trade_prevention_type: str = "taker_at_cross",
        client_order_id: str | None = None,
    ) -> dict[str, Any]: ...


@dataclass(frozen=True, slots=True)
class KalshiTradeConfig:
    usd: float = DEFAULT_USD
    min_confidence: float = DEFAULT_MIN_CONFIDENCE
    dry_run: bool = True
    filled_log: Path = DEFAULT_FILLED_LOG
    time_in_force: str = "immediate_or_cancel"
    max_ask_slippage: float = DEFAULT_MAX_ASK_SLIPPAGE
    refresh_quote: bool = True


@dataclass(slots=True)
class KalshiTradeResult:
    market: str
    ticker: str
    side: str
    action: str  # bought | dry_run | skipped | failed
    detail: str = ""
    usd: float = 0.0
    contracts: int = 0
    limit_price: float | None = None
    book_side: str | None = None
    yes_leg_price: float | None = None
    client_order_id: str | None = None
    response: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "market": self.market,
            "ticker": self.ticker,
            "side": self.side,
            "action": self.action,
            "detail": self.detail,
            "usd": self.usd,
            "contracts": self.contracts,
            "limit_price": self.limit_price,
            "book_side": self.book_side,
            "yes_leg_price": self.yes_leg_price,
            "client_order_id": self.client_order_id,
            "response": self.response,
        }


def kalshi_outcome_side(side: str) -> str:
    """Map a fair-value row side onto Kalshi YES/NO."""
    key = (side or "").strip().lower()
    if key in YES_SIDES:
        return "yes"
    if key in NO_SIDES:
        return "no"
    raise ValueError(f"unmapped Kalshi side: {side!r}")


def v2_book_order(side: str, ask: float) -> tuple[str, Decimal]:
    """V2 book is YES-only: bid = buy YES, ask = sell YES (buy NO at 1 - price)."""
    outcome = kalshi_outcome_side(side)
    px = _as_decimal(ask)
    if px <= 0 or px >= 1:
        raise ValueError(f"ask must be in (0, 1), got {ask}")
    if outcome == "yes":
        return "bid", _dollars(px)
    return "ask", _dollars(Decimal("1") - px)


def contracts_for_usd(usd: float, ask: float) -> int:
    """Whole contracts whose cost at `ask` does not exceed `usd`."""
    price = _as_decimal(ask)
    if price <= 0 or price >= 1:
        return 0
    n = int(Decimal(str(usd)) // price)
    return n if n >= 1 else 0


def send_kalshi_ioc(
    client: KalshiTradingClient,
    *,
    ticker: str,
    side: str,
    ask: float,
    usd: float,
    time_in_force: str = "immediate_or_cancel",
) -> dict[str, Any]:
    """Place a YES-book IOC sized to `usd`. Returns fill metadata.

    `side` is a fair-value / YES-NO label (`yes|no|over|under|home|away`).
    """
    contracts = contracts_for_usd(usd, ask)
    if contracts < 1:
        raise ValueError(f"cannot size ${usd:.2f} at ask {ask:.4f}")
    book_side, yes_leg = v2_book_order(side, ask)
    client_order_id = str(uuid.uuid4())
    resp = client.create_event_order(
        ticker=ticker,
        side=book_side,
        count=_count_fp(contracts),
        price=_dollars_fp(yes_leg),
        time_in_force=time_in_force,
        client_order_id=client_order_id,
    )
    fill_count = _as_decimal((resp or {}).get("fill_count") or 0)
    cost = float(_as_decimal(ask) * contracts)
    return {
        "response": resp if isinstance(resp, dict) else {"raw": resp},
        "contracts": contracts,
        "usd": round(cost, 4),
        "limit_price": float(_dollars(_as_decimal(ask))),
        "book_side": book_side,
        "yes_leg_price": float(yes_leg),
        "client_order_id": client_order_id,
        "fill_count": float(fill_count),
    }


def select_tradable_kalshi(
    rows: list[PricedOpportunity] | list[dict[str, Any]],
    *,
    min_confidence: float = DEFAULT_MIN_CONFIDENCE,
) -> list[dict[str, Any]]:
    """Keep `tradable` Kalshi rows at or above the confidence floor."""
    picked: list[dict[str, Any]] = []
    for raw in rows:
        row = _as_row(raw)
        if str(row.get("venue") or "").lower() != "kalshi":
            continue
        try:
            conf = float(row.get("confidence") or 0.0)
        except (TypeError, ValueError):
            continue
        if conf < min_confidence:
            continue
        ticker = str(row.get("ticker") or row.get("market_id") or "").strip()
        if not ticker:
            continue
        try:
            kalshi_outcome_side(str(row.get("side") or ""))
        except ValueError:
            continue
        picked.append(row)
    return picked


def load_tradable_rows(source: FairValueReport | dict[str, Any] | Path | str) -> list[dict[str, Any]]:
    """Read the `tradable` list from a report object, dict, or JSON path."""
    if isinstance(source, FairValueReport):
        return [r.to_dict() for r in source.tradable]
    if isinstance(source, (str, Path)):
        payload = json.loads(Path(source).read_text(encoding="utf-8"))
        if not isinstance(payload, dict):
            raise ValueError(f"expected JSON object in {source}")
        source = payload
    rows = source.get("tradable") or []
    if not isinstance(rows, list):
        raise ValueError("report['tradable'] is not a list")
    return [row for row in rows if isinstance(row, dict)]


def run_kalshi_trades(
    source: FairValueReport | dict[str, Any] | Path | str,
    cfg: KalshiTradeConfig | None = None,
    *,
    client: KalshiTradingClient | None = None,
) -> list[KalshiTradeResult]:
    """Filter + size + (optionally) send IOC buys. Dry-run unless cfg.dry_run is False."""
    cfg = cfg or KalshiTradeConfig()
    rows = select_tradable_kalshi(
        load_tradable_rows(source),
        min_confidence=cfg.min_confidence,
    )
    already = _load_filled_keys(cfg.filled_log)
    results: list[KalshiTradeResult] = []
    owns_client = False
    trader = client
    if trader is None and not cfg.dry_run:
        from ev_trading.venues.kalshi.api import KalshiClient

        trader = KalshiClient.from_env(require_auth=True)
        owns_client = True
    try:
        for row in rows:
            results.append(_trade_one(row, cfg, already, trader))
    finally:
        if owns_client and trader is not None:
            close = getattr(trader, "close", None)
            if callable(close):
                close()
    return results


def render_trade_results(results: list[KalshiTradeResult]) -> None:
    if not results:
        print("kalshi trades: none matched (tradable + venue=kalshi + confidence floor)")
        return
    print(f"kalshi trades: {len(results)}")
    for row in results:
        px = f"{row.limit_price:.2f}" if row.limit_price is not None else "—"
        print(
            f"  {row.action:<8} ${row.usd:<6.2f} {row.contracts:>4}ct @{px}  "
            f"{row.side:<6} {row.ticker}  {row.market}  {row.detail}"
        )


def _trade_one(
    row: dict[str, Any],
    cfg: KalshiTradeConfig,
    already: set[str],
    client: KalshiTradingClient | None,
) -> KalshiTradeResult:
    ticker = str(row.get("ticker") or row.get("market_id") or "").strip()
    side = str(row.get("side") or "")
    market = str(row.get("market") or ticker)
    key = _trade_key(ticker, side)
    base = KalshiTradeResult(market=market, ticker=ticker, side=side, action="skipped")
    if key in already:
        base.detail = "already traded (dedupe log)"
        return base
    try:
        ask = float(row.get("market_price"))
    except (TypeError, ValueError):
        base.detail = "missing market_price"
        return base

    if client is not None and cfg.refresh_quote and not cfg.dry_run:
        try:
            live = client.get_market(ticker)
        except Exception as exc:  # noqa: BLE001
            base.action = "failed"
            base.detail = f"quote refresh failed: {exc}"
            return base
        live_ask = _live_ask(live, side)
        if live_ask is None:
            base.detail = "live ask missing"
            return base
        if live_ask > ask + cfg.max_ask_slippage:
            base.detail = f"ask moved {ask:.2f} → {live_ask:.2f}"
            return base
        ask = live_ask

    contracts = contracts_for_usd(cfg.usd, ask)
    if contracts < 1:
        base.detail = f"cannot size ${cfg.usd:.2f} at ask {ask:.4f}"
        return base
    try:
        book_side, yes_leg = v2_book_order(side, ask)
    except ValueError as exc:
        base.detail = str(exc)
        return base
    cost = float(_as_decimal(ask) * contracts)
    client_order_id = str(uuid.uuid4())
    planned = KalshiTradeResult(
        market=market,
        ticker=ticker,
        side=side,
        action="dry_run" if cfg.dry_run else "bought",
        detail=f"{kalshi_outcome_side(side)} IOC",
        usd=round(cost, 4),
        contracts=contracts,
        limit_price=float(_dollars(_as_decimal(ask))),
        book_side=book_side,
        yes_leg_price=float(yes_leg),
        client_order_id=client_order_id,
    )
    if cfg.dry_run:
        return planned
    if client is None:
        planned.action = "failed"
        planned.detail = "no Kalshi client"
        return planned
    try:
        resp = client.create_event_order(
            ticker=ticker,
            side=book_side,
            count=_count_fp(contracts),
            price=_dollars_fp(yes_leg),
            time_in_force=cfg.time_in_force,
            client_order_id=client_order_id,
        )
    except Exception as exc:  # noqa: BLE001
        planned.action = "failed"
        planned.detail = str(exc)
        return planned
    planned.response = resp if isinstance(resp, dict) else {"raw": resp}
    fill_count = _as_decimal((resp or {}).get("fill_count") or 0)
    if fill_count <= 0:
        planned.action = "skipped"
        planned.detail = "IOC no fill"
        return planned
    planned.action = "bought"
    planned.detail = f"filled {fill_count}"
    _append_filled(
        cfg.filled_log,
        {
            "key": key,
            "ticker": ticker,
            "side": side,
            "usd": planned.usd,
            "contracts": contracts,
            "limit_price": planned.limit_price,
            "client_order_id": client_order_id,
            "order_id": (resp or {}).get("order_id"),
            "fill_count": str(fill_count),
        },
    )
    already.add(key)
    return planned


def _live_ask(market: dict[str, Any], side: str) -> float | None:
    blob = market.get("market") if isinstance(market.get("market"), dict) else market
    outcome = kalshi_outcome_side(side)
    if outcome == "yes":
        return kalshi_yes_ask(blob)
    return kalshi_no_ask(blob)


def _as_row(row: PricedOpportunity | dict[str, Any]) -> dict[str, Any]:
    if isinstance(row, PricedOpportunity):
        return row.to_dict()
    return dict(row)


def _trade_key(ticker: str, side: str) -> str:
    return f"{ticker}:{(side or '').strip().lower()}"


def _load_filled_keys(path: Path | str) -> set[str]:
    log_path = Path(path)
    if not log_path.is_file():
        return set()
    keys: set[str] = set()
    for line in log_path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            row = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(row, dict) and row.get("key"):
            keys.add(str(row["key"]))
    return keys


def _append_filled(path: Path | str, row: dict[str, Any]) -> None:
    log_path = Path(path)
    log_path.parent.mkdir(parents=True, exist_ok=True)
    with log_path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(row) + "\n")


def _as_decimal(value: Any) -> Decimal:
    return Decimal(str(value))


def _dollars(value: Decimal) -> Decimal:
    return value.quantize(Decimal("0.0001"), rounding=ROUND_HALF_UP)


def _dollars_fp(value: Decimal) -> str:
    return f"{_dollars(value):.4f}"


def _count_fp(contracts: int) -> str:
    return f"{Decimal(contracts).quantize(Decimal('0.01'), rounding=ROUND_DOWN):.2f}"
