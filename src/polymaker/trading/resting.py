"""Resting (maker) order book: carry open limit buys between 30-minute runs.

Each sharp buy rests as a GTC post-only limit. The order is recorded in
`journal/resting_orders.json` (a persisted volume) and reconciled at the top of
every pipeline run:

  - filled           -> log fills, `convex.complete`, add to the dedupe log
  - partly filled and inside the cancel window
                     -> cancel only the unfilled remainder, complete what filled
  - unfilled and inside the cancel window
                     -> cancel, `convex.release` so the claim is freed
  - outside the window -> keep holding

A cancel is never assumed. If it fails the record stays and is retried next
run. A missing or unparseable start time counts as inside the window (fail
closed). Positions are otherwise held to resolution; there is no exit order.
"""

from __future__ import annotations

import json
import os
import time
from dataclasses import asdict, dataclass, field, fields
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from polymaker.catalog.sports import parse_utc
from polymaker.logging import get_logger
from polymaker.trading.event_key import sibling_side_key
from polymaker.trading.fill import iso_to_unix_ms
from polymaker.trading.maker_fills import (
    POLYMARKET_SPORTS_TAKER_RATE,
    kalshi_fill_delta,
    log_fill,
    polymarket_fills,
)

log = get_logger("trading.resting")

DEFAULT_CANCEL_WINDOW_MINUTES = 30.0
_EPS = 1e-6


# ── state ───────────────────────────────────────────────────────────────
@dataclass(slots=True)
class RestingOrder:
    venue: str  # polymarket | kalshi
    order_id: str
    canonical: str
    limit_price: float
    size: float
    claimed_keys: list[str] = field(default_factory=list)
    slug_key: str | None = None
    league: str = ""
    matchup: str = ""
    side: str = ""  # outcome label we bought
    tier: str = ""
    usd: float = 0.0
    ask: float = 0.0  # best ask when we placed
    start_time: str | None = None
    placed_ts: float = field(default_factory=time.time)
    token_id: str | None = None
    slug: str | None = None
    ticker: str | None = None
    kalshi_side: str | None = None
    client_order_id: str | None = None
    low_volume_dog_flag: bool = False
    # progress, updated every reconcile
    filled: float = 0.0
    fees: float = 0.0
    logged_trade_ids: list[str] = field(default_factory=list)
    logged_size: float = 0.0
    logged_fee: float = 0.0
    logged_taker_cost: float = 0.0
    fill_value: float = 0.0  # sum(price * size) over logged fills
    last_error: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> RestingOrder:
        names = {f.name for f in fields(cls)}
        return cls(**{k: v for k, v in data.items() if k in names})

    @property
    def avg_price(self) -> float:
        if self.logged_size > _EPS and self.fill_value > 0:
            return self.fill_value / self.logged_size
        return self.limit_price

    @property
    def unfilled(self) -> float:
        return max(self.size - self.filled, 0.0)


class RestingBook:
    """JSON file of open resting orders. Writes are atomic."""

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)

    def load(self) -> list[RestingOrder]:
        if not self.path.is_file():
            return []
        text = self.path.read_text(encoding="utf-8").strip()
        if not text:
            return []
        try:
            payload = json.loads(text)
            rows = payload["orders"] if isinstance(payload, dict) else payload
            return [RestingOrder.from_dict(r) for r in rows]
        except (ValueError, KeyError, TypeError) as exc:
            # Fail loud: silently ignoring this file would orphan live orders.
            raise ValueError(f"unreadable resting order book {self.path}: {exc}") from exc

    def save(self, orders: list[RestingOrder]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(self.path.suffix + ".tmp")
        tmp.write_text(
            json.dumps({"orders": [o.to_dict() for o in orders]}, indent=2, default=str),
            encoding="utf-8",
        )
        os.replace(tmp, self.path)

    def add(self, order: RestingOrder) -> None:
        orders = [o for o in self.load() if o.order_id != order.order_id]
        orders.append(order)
        self.save(orders)

    def keys(self) -> set[str]:
        """Trade keys held by open orders (so a play is not placed twice)."""
        return resting_keys(self.load())


def resting_keys(orders: list[RestingOrder]) -> set[str]:
    keys: set[str] = set()
    for o in orders:
        keys.add(o.canonical)
        sibling = sibling_side_key(o.canonical)
        if sibling:
            keys.add(sibling)
        if o.slug_key:
            keys.add(o.slug_key)
    return keys


# ── cancel window ───────────────────────────────────────────────────────
def in_cancel_window(
    start_time: str | None,
    window_minutes: float = DEFAULT_CANCEL_WINDOW_MINUTES,
    *,
    now: datetime | None = None,
) -> bool:
    """True when the game starts within `window_minutes` (or already started).

    Exactly at the boundary counts as inside. A missing or unparseable start
    time fails closed (inside the window) so the order is cancelled.
    """
    start = parse_utc(start_time)
    if start is None:
        return True
    clock = now or datetime.now(UTC)
    return start <= clock + timedelta(minutes=window_minutes)


# ── reconcile ───────────────────────────────────────────────────────────
@dataclass(slots=True)
class ReconcileResult:
    action: str  # held | filled | partial | cancelled | cancel_failed | error | would_cancel
    order: RestingOrder
    detail: str = ""

    @property
    def filled(self) -> float:
        return self.order.filled

    def to_row(self) -> dict[str, Any]:
        o = self.order
        return {
            "action": self.action,
            "usd": round(o.avg_price * o.filled, 4) if o.filled else o.usd,
            "league": o.league,
            "matchup": o.matchup,
            "side": o.side,
            "slug": o.slug,
            "venue": o.venue,
            "ticker": o.ticker,
            "tier": o.tier,
            "detail": self.detail,
        }


@dataclass(slots=True)
class _VenueState:
    status: str  # live | filled | canceled | unknown
    filled: float = 0.0
    fills: list[dict[str, Any]] = field(default_factory=list)
    raw: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class ReconcileConfig:
    filled_log: str = "journal/sharp_trades.jsonl"
    maker_fills_log: str = "journal/maker_fills.jsonl"
    cancel_window_minutes: float = DEFAULT_CANCEL_WINDOW_MINUTES
    polymarket_taker_rate: float = POLYMARKET_SPORTS_TAKER_RATE


async def reconcile_resting_orders(
    book: RestingBook,
    cfg: ReconcileConfig,
    *,
    gateway: Any | None = None,
    kalshi_client: Any | None = None,
    convex: Any | None = None,
    now: datetime | None = None,
    dry_run: bool = False,
) -> list[ReconcileResult]:
    """Check every resting order; fill, cancel, or hold. Never raises per order."""
    orders = book.load()
    results: list[ReconcileResult] = []
    remaining: list[RestingOrder] = []
    for rec in orders:
        inside = in_cancel_window(rec.start_time, cfg.cancel_window_minutes, now=now)
        if dry_run:
            results.append(
                ReconcileResult(
                    "would_cancel" if inside else "held",
                    rec,
                    "inside cancel window" if inside else "outside cancel window",
                )
            )
            remaining.append(rec)
            continue
        try:
            result = await _reconcile_one(
                rec, cfg, inside, gateway, kalshi_client, convex
            )
        except Exception as exc:  # noqa: BLE001 - one bad order must not strand the rest
            rec.last_error = f"reconcile error: {exc}"
            log.error("reconcile_failed", order=rec.order_id[:12], err=str(exc))
            result = ReconcileResult("error", rec, rec.last_error)
        results.append(result)
        if result.action in {"held", "cancel_failed", "error"}:
            remaining.append(rec)
        # persist after every order so a crash never re-processes a finished one
        book.save(remaining + [r for r in orders[len(results):]])
    if not dry_run:
        book.save(remaining)
    return results


async def _reconcile_one(
    rec: RestingOrder,
    cfg: ReconcileConfig,
    inside: bool,
    gateway: Any | None,
    kalshi_client: Any | None,
    convex: Any | None,
) -> ReconcileResult:
    state = await _fetch_state(rec, cfg, gateway, kalshi_client)
    _log_new_fills(rec, state, cfg)
    rec.filled = max(rec.filled, state.filled)

    if state.status == "filled":
        return await _finalize(rec, cfg, convex, "filled order")
    if state.status == "canceled":
        return await _finalize(rec, cfg, convex, "order no longer resting")

    if not inside:
        rec.last_error = "state unavailable" if state.status == "unknown" else None
        return ReconcileResult(
            "held",
            rec,
            f"resting {rec.filled:g}/{rec.size:g} filled"
            + (" (state unavailable)" if state.status == "unknown" else ""),
        )

    # Inside the cancel window: cancel the unfilled remainder, then settle.
    ok, reason, reduced_by = await cancel_resting(rec, gateway, kalshi_client)
    if not ok:
        # It may have filled or died on its own; trust a fresh read.
        state = await _fetch_state(rec, cfg, gateway, kalshi_client)
        _log_new_fills(rec, state, cfg)
        rec.filled = max(rec.filled, state.filled)
        if state.status in {"filled", "canceled"}:
            return await _finalize(rec, cfg, convex, f"cancel not needed ({reason})")
        rec.last_error = f"cancel failed: {reason}"
        log.error("cancel_failed", order=rec.order_id[:12], reason=reason)
        print(f"[resting] CANCEL FAILED {rec.venue} {rec.matchup} {rec.order_id[:12]}: {reason}",
              flush=True)
        return ReconcileResult("cancel_failed", rec, rec.last_error)

    state = await _fetch_state(rec, cfg, gateway, kalshi_client)
    _log_new_fills(rec, state, cfg)
    rec.filled = max(rec.filled, state.filled)
    if reduced_by is not None:
        # Kalshi reads can lag a write; the cancel response is authoritative on
        # how many contracts were removed, so the rest filled.
        rec.filled = max(rec.filled, rec.size - reduced_by)
    return await _finalize(rec, cfg, convex, "cancelled inside window")


async def _fetch_state(
    rec: RestingOrder,
    cfg: ReconcileConfig,
    gateway: Any | None,
    kalshi_client: Any | None,
) -> _VenueState:
    if rec.venue == "polymarket":
        return await _poly_state(rec, cfg, gateway)
    if rec.venue == "kalshi":
        return _kalshi_state(rec, kalshi_client)
    return _VenueState("unknown")


async def _poly_state(rec: RestingOrder, cfg: ReconcileConfig, gw: Any | None) -> _VenueState:
    if gw is None:
        return _VenueState("unknown")
    order = await gw.get_order(rec.order_id)
    if order is None or not order.get("status"):
        return _VenueState("unknown")
    original = float(order.get("original_size") or rec.size)
    matched = float(order.get("size_matched") or 0.0)
    status = str(order["status"]).lower()
    fills: list[dict[str, Any]] = []
    if matched > _EPS and matched - rec.logged_size > _EPS:
        trades = await gw.order_trades(rec.order_id, rec.token_id or "", after_ts=rec.placed_ts)
        fills = polymarket_fills(
            rec.order_id,
            trades,
            skip_ids=set(rec.logged_trade_ids),
            taker_rate=cfg.polymarket_taker_rate,
        )
    if original > 0 and matched >= original - _EPS:
        kind = "filled"
    elif status.startswith("cancel"):
        kind = "canceled"
    elif status in {"live", "delayed", "unmatched", "matched"}:
        kind = "live"
    else:
        kind = "unknown"
    return _VenueState(kind, matched, fills, order.get("raw") or {})


def _kalshi_state(rec: RestingOrder, client: Any | None) -> _VenueState:
    if client is None:
        return _VenueState("unknown")
    try:
        order = client.get_order(rec.order_id)
    except Exception as exc:  # noqa: BLE001 - Kalshi can 404 briefly after a write
        log.warning("kalshi_get_order_failed", order=rec.order_id[:12], err=str(exc))
        return _VenueState("unknown")
    status = str(order.get("status", "")).lower()
    filled = float(order.get("fill_count_fp") or 0.0)
    remaining = float(order.get("remaining_count_fp") or 0.0)
    fills: list[dict[str, Any]] = []
    delta = kalshi_fill_delta(
        order,
        logged_size=rec.logged_size,
        logged_fee=rec.logged_fee,
        logged_taker_cost=rec.logged_taker_cost,
        limit_price=rec.limit_price,
    )
    if delta is not None:
        fills.append(delta)
    if status == "executed" or (filled > _EPS and remaining <= _EPS and status != "resting"):
        kind = "filled"
    elif status == "canceled":
        kind = "canceled"
    elif status == "resting":
        kind = "live"
    else:
        kind = "unknown"
    return _VenueState(kind, filled, fills, order)


def _log_new_fills(rec: RestingOrder, state: _VenueState, cfg: ReconcileConfig) -> None:
    for fill in state.fills:
        log_fill(
            cfg.maker_fills_log,
            fill,
            venue=rec.venue,
            order_id=rec.order_id,
            key=rec.canonical,
            matchup=rec.matchup,
            outcome=rec.side,
        )
        size = float(fill.get("size") or 0.0)
        rec.logged_size += size
        rec.fill_value += float(fill.get("price") or 0.0) * size
        if rec.venue == "kalshi":
            rec.logged_fee = float(fill.get("cum_fee", rec.logged_fee))
            rec.logged_taker_cost = float(fill.get("cum_taker_cost", rec.logged_taker_cost))
            rec.fees = rec.logged_fee
        else:
            rec.fees += float(fill.get("fee") or 0.0)
            if fill.get("trade_id"):
                rec.logged_trade_ids.append(str(fill["trade_id"]))
    rec.filled = max(rec.filled, state.filled)
    # Matched size we could not tie to a trade row: say so rather than claim fee = 0.
    if rec.venue == "polymarket" and rec.filled - rec.logged_size > _EPS and not state.fills:
        log.warning(
            "unlogged_fill",
            order=rec.order_id[:12],
            filled=rec.filled,
            logged=rec.logged_size,
        )


async def cancel_resting(
    rec: RestingOrder, gateway: Any | None, kalshi_client: Any | None
) -> tuple[bool, str | None, float | None]:
    """Returns (cancelled, reason_if_not, kalshi_reduced_by)."""
    if rec.venue == "polymarket":
        if gateway is None:
            return False, "no polymarket gateway", None
        ok, reason = await gateway.cancel_order(rec.order_id)
        return ok, reason, None
    if rec.venue == "kalshi":
        if kalshi_client is None or not rec.ticker:
            return False, "no kalshi client/ticker", None
        try:
            resp = kalshi_client.cancel_order(rec.order_id, rec.ticker)
        except Exception as exc:  # noqa: BLE001
            return False, str(exc), None
        reduced = (resp or {}).get("reduced_by")
        try:
            return True, None, float(reduced) if reduced is not None else None
        except (TypeError, ValueError):
            return True, None, None
    return False, f"unknown venue {rec.venue}", None


async def _finalize(
    rec: RestingOrder, cfg: ReconcileConfig, convex: Any | None, why: str
) -> ReconcileResult:
    """Order is done. Record what filled, or free the claim if nothing did."""
    if rec.filled <= _EPS:
        _release(convex, rec)
        log.info("resting_cancelled", order=rec.order_id[:12], matchup=rec.matchup)
        print(f"[resting] cancelled unfilled {rec.venue} {rec.matchup} {rec.side} "
              f"{rec.size:g}@{rec.limit_price:.3f}", flush=True)
        return ReconcileResult("cancelled", rec, f"{why}; no fill, claim released")

    shares = rec.filled
    unlogged = shares - rec.logged_size
    if unlogged > _EPS:
        # Filled size with no matching trade/fee row. Record it as unverified
        # instead of claiming it was a zero-fee maker fill.
        log_fill(
            cfg.maker_fills_log,
            {"price": rec.limit_price, "size": unlogged, "fee": None, "role": "unverified"},
            venue=rec.venue,
            order_id=rec.order_id,
            key=rec.canonical,
            matchup=rec.matchup,
            outcome=rec.side,
        )
        rec.logged_size += unlogged
        rec.fill_value += rec.limit_price * unlogged
    buy_price = rec.avg_price
    partial = shares < rec.size - _EPS
    start_ms = iso_to_unix_ms(rec.start_time)
    fee_per = rec.fees / shares if shares else 0.0
    fill: dict[str, Any] = {
        "ts": time.time(),
        "key": rec.canonical,
        "venue": rec.venue,
        "slug": rec.slug,
        "ticker": rec.ticker,
        "kalshi_side": rec.kalshi_side,
        "outcome": rec.side,
        "league": rec.league,
        "matchup": rec.matchup,
        "side": rec.side,
        "tier": rec.tier,
        "usd": round(buy_price * shares, 4),
        "ask": rec.ask,
        "limit_price": rec.limit_price,
        "order_size": rec.size,
        "fee": rec.fees,
        "all_in": buy_price + fee_per,
        "maker": True,
        "partial": partial,
        "order_id": rec.order_id,
        "client_order_id": rec.client_order_id,
        "low_volume_dog_flag": rec.low_volume_dog_flag,
        "buy_price": buy_price,
        "shares": shares,
        "clv_status": "unavailable" if rec.venue == "kalshi" else "pending",
    }
    if rec.venue == "kalshi":
        fill["contracts"] = rec.size
        fill["fill_count"] = shares
    if rec.token_id:
        fill["token_id"] = rec.token_id
    if rec.start_time:
        fill["start_time"] = rec.start_time
    if start_ms is not None:
        fill["start_time_ms"] = start_ms
    if convex is not None:
        try:
            convex.complete(
                rec.canonical,
                fill,
                token_id=rec.token_id,
                start_time=start_ms,
                buy_price=buy_price,
                shares=shares,
                venue=rec.venue,
            )
        except Exception as exc:  # noqa: BLE001
            fill["convex_complete_error"] = str(exc)
            log.error("convex_complete_failed", order=rec.order_id[:12], err=str(exc))
    append_filled(cfg.filled_log, fill)
    action = "partial" if partial else "filled"
    print(
        f"[resting] {action} {rec.venue} {rec.matchup} {rec.side} "
        f"{shares:g}/{rec.size:g} @ {buy_price:.4f} fee={rec.fees:.4f} ({why})",
        flush=True,
    )
    return ReconcileResult(action, rec, f"{why}; {shares:g}/{rec.size:g} @ {buy_price:.4f}")


def _release(convex: Any | None, rec: RestingOrder) -> None:
    if convex is None:
        return
    for key in rec.claimed_keys or [rec.canonical]:
        try:
            convex.release(key)
        except Exception:  # noqa: BLE001
            continue


def append_filled(path: str | Path, payload: dict[str, Any]) -> None:
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    with p.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(payload, default=str) + "\n")


# ── wiring ──────────────────────────────────────────────────────────────
async def reconcile_from_config(
    cfg: Any,
    *,
    live: bool,
    book_path: str | Path | None = None,
    gateway: Any | None = None,
    kalshi_client: Any | None = None,
    convex: Any | None = None,
) -> list[ReconcileResult]:
    """Build venue clients only for the venues that have open orders, then reconcile."""
    from polymaker.execution.gateway import ExecutionGateway
    from polymaker.trading.convex_trades import ConvexTradeClient

    book = RestingBook(book_path or cfg.sharp.resting_log)
    rcfg = ReconcileConfig(
        filled_log=cfg.sharp.filled_log,
        maker_fills_log=cfg.sharp.maker_fills_log,
        cancel_window_minutes=cfg.maker.cancel_window_minutes,
    )
    orders = book.load()
    if not orders:
        return []
    if not live:
        return await reconcile_resting_orders(book, rcfg, dry_run=True)

    owns_gw = owns_kalshi = False
    try:
        if gateway is None and any(o.venue == "polymarket" for o in orders):
            gateway = ExecutionGateway(cfg, paper=False)
            owns_gw = True
            await gateway.connect()
        if kalshi_client is None and any(o.venue == "kalshi" for o in orders):
            from ev_trading.venues.kalshi.api import KalshiClient

            kalshi_client = KalshiClient.from_env(require_auth=True)
            owns_kalshi = True
        if convex is None:
            candidate = ConvexTradeClient()
            convex = candidate if candidate.configured else None
        return await reconcile_resting_orders(
            book,
            rcfg,
            gateway=gateway,
            kalshi_client=kalshi_client,
            convex=convex,
        )
    finally:
        if owns_gw and gateway is not None:
            gateway.close()
        if owns_kalshi and kalshi_client is not None:
            close = getattr(kalshi_client, "close", None)
            if callable(close):
                close()
