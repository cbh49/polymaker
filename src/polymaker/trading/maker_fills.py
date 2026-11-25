"""Per-fill logging for resting orders: price, size, fee, and maker/taker role.

Each fill is one row in `journal/maker_fills.jsonl` and one console line, so a
maker fill is easy to confirm (`role=maker fee=0.0000`). A fill that reports a
taker role or a fee above zero logs a TAKER_FILL warning (and posts to Discord
when DISCORD_SHARP_WEBHOOK_URL is set).
"""

from __future__ import annotations

import json
import os
import time
from pathlib import Path
from typing import Any

from polymaker.logging import get_logger

log = get_logger("trading.maker_fills")

# Polymarket sports taker fee: C * rate * p * (1 - p). Only used to price a fill
# that came back as a taker; makers are never charged.
POLYMARKET_SPORTS_TAKER_RATE = 0.05


def polymarket_taker_fee(
    size: float, price: float, rate: float = POLYMARKET_SPORTS_TAKER_RATE
) -> float:
    return round(float(size) * rate * float(price) * (1.0 - float(price)), 5)


def _f(value: Any, default: float = 0.0) -> float:
    try:
        n = float(value)
    except (TypeError, ValueError):
        return default
    return n if n == n else default


def polymarket_fills(
    order_id: str,
    trades: list[dict[str, Any]],
    *,
    skip_ids: set[str] | None = None,
    taker_rate: float = POLYMARKET_SPORTS_TAKER_RATE,
) -> list[dict[str, Any]]:
    """Turn raw CLOB trade rows into fill dicts for `order_id`.

    Maker fills (our order was resting) cost no fee. If our order was the taker
    side of a trade, the fee is estimated from the sports taker rate and the
    row is flagged `role=taker`.
    """
    skip_ids = skip_ids or set()
    out: list[dict[str, Any]] = []
    for row in trades:
        trade_id = str(row.get("id") or "")
        if trade_id and trade_id in skip_ids:
            continue
        if str(row.get("status", "")).upper() == "FAILED":
            continue
        if row.get("taker_order_id") == order_id:
            role = "taker"
            price = _f(row.get("price"))
            size = _f(row.get("size"))
        else:
            mine = next(
                (
                    mo
                    for mo in row.get("maker_orders") or []
                    if isinstance(mo, dict) and mo.get("order_id") == order_id
                ),
                None,
            )
            if mine is None:
                continue
            role = "maker"
            price = _f(mine.get("price"), _f(row.get("price")))
            size = _f(mine.get("matched_amount"))
        if size <= 0:
            continue
        fee = 0.0 if role == "maker" else polymarket_taker_fee(size, price, taker_rate)
        out.append(
            {
                "trade_id": trade_id or None,
                "price": price,
                "size": size,
                "fee": fee,
                "role": role,
                "status": row.get("status"),
                "match_time": row.get("match_time"),
            }
        )
    return out


def kalshi_fill_delta(
    order: dict[str, Any],
    *,
    logged_size: float,
    logged_fee: float,
    logged_taker_cost: float,
    limit_price: float,
) -> dict[str, Any] | None:
    """New Kalshi fill since the last log, from cumulative order fields.

    Maker fills execute at the resting limit price, so that is the logged price
    for a maker row. The fee comes from Kalshi's own `maker_fees_dollars` and
    `taker_fees_dollars`, so a fee above zero is visible exactly as charged.
    """
    filled = _f(order.get("fill_count_fp"))
    delta = filled - logged_size
    if delta <= 1e-9:
        return None
    maker_fee = _f(order.get("maker_fees_dollars"))
    taker_fee = _f(order.get("taker_fees_dollars"))
    taker_cost = _f(order.get("taker_fill_cost_dollars"))
    fee_total = maker_fee + taker_fee
    role = "taker" if taker_cost - logged_taker_cost > 1e-9 or taker_fee > 0 else "maker"
    price = limit_price
    if role == "taker":
        cost = _f(order.get("maker_fill_cost_dollars")) + taker_cost
        if filled > 0 and cost > 0:
            price = cost / filled
    return {
        "trade_id": None,
        "price": price,
        "size": delta,
        "fee": round(fee_total - logged_fee, 6),
        "role": role,
        "status": order.get("status"),
        "match_time": order.get("last_update_time"),
        "cum_fee": fee_total,
        "cum_taker_cost": taker_cost,
    }


def is_taker_fill(fill: dict[str, Any]) -> bool:
    return fill.get("role") == "taker" or _f(fill.get("fee")) > 0


def log_fill(
    path: str | Path,
    fill: dict[str, Any],
    *,
    venue: str,
    order_id: str,
    key: str,
    matchup: str = "",
    outcome: str = "",
    alert: bool = True,
) -> dict[str, Any]:
    """Append one fill row and print a confirmation line (and warn on taker fills)."""
    row = {
        "ts": time.time(),
        "venue": venue,
        "order_id": order_id,
        "key": key,
        "matchup": matchup,
        "outcome": outcome,
        **fill,
    }
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    with p.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(row, default=str) + "\n")
    fee_text = "n/a" if row.get("fee") is None else f"{_f(row.get('fee')):.4f}"
    line = (
        f"[fill] {venue} {matchup} {outcome} price={_f(row.get('price')):.4f} "
        f"size={_f(row.get('size')):.2f} fee={fee_text} "
        f"role={row.get('role')} order={order_id[:12]}"
    )
    print(line, flush=True)
    if row.get("role") == "unverified":
        print(f"UNVERIFIED_FILL {line} (no trade row to confirm maker/fee)", flush=True)
        log.warning("unverified_fill", venue=venue, order=order_id[:12])
        if alert:
            _discord_alert(f"UNVERIFIED_FILL {line}")
    elif is_taker_fill(row):
        warning = f"TAKER_FILL {line}"
        print(warning, flush=True)
        log.warning("taker_fill", venue=venue, order=order_id[:12], role=row.get("role"),
                    fee=row.get("fee"))
        if alert:
            _discord_alert(warning)
    return row


def _discord_alert(text: str) -> None:
    url = os.environ.get("DISCORD_SHARP_WEBHOOK_URL", "").strip()
    if not url:
        return
    try:
        import requests

        requests.post(url, json={"content": text[:1900]}, timeout=10)
    except Exception:  # noqa: BLE001 - alerting must never break reconcile
        return
