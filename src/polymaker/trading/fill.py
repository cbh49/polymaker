"""Parse CLOB fill price / size and compute closing-line value."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any


def parse_buy_fill(
    resp: dict[str, Any] | None,
    ask: float | None,
) -> tuple[float | None, float | None]:
    """Return (buy_price, shares) for a BUY.

    CLOB market buys typically report `makingAmount` (USDC spent) and
    `takingAmount` (shares received). Average fill is making / taking.
    Falls back to the book ask when the response has no fill amounts.
    """
    shares = _first_number(
        resp,
        "takingAmount",
        "taking_amount",
        "size",
        "shares",
        "filledSize",
    )
    making = _first_number(resp, "makingAmount", "making_amount")
    avg = _first_number(
        resp,
        "average_price",
        "avgPrice",
        "avg_price",
        "price",
    )

    buy_price: float | None = None
    if avg is not None and 0 < avg <= 1.5:
        buy_price = avg
    elif making is not None and shares is not None and shares > 0:
        ratio = making / shares
        if 0 < ratio <= 1.5:
            buy_price = ratio
        elif 0 < shares / making <= 1.5:
            buy_price = shares / making
    if buy_price is None and ask is not None and ask > 0:
        buy_price = float(ask)
    return buy_price, shares


def iso_to_unix_ms(value: str | None) -> int | None:
    if not value or not str(value).strip():
        return None
    raw = str(value).strip().replace("Z", "+00:00")
    try:
        dt = datetime.fromisoformat(raw)
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return int(dt.timestamp() * 1000)


def compute_clv(buy_price: float, closing_price: float) -> tuple[float, float]:
    """Return (clv, clv_cents). Positive = bought cheaper than the close."""
    clv = closing_price - buy_price
    clv_cents = round(clv * 100, 4)
    return clv, clv_cents


def select_closing_tick(
    history: list[dict[str, Any]],
    start_time_ms: int,
) -> tuple[float, int] | None:
    """Last history point at or before tip-off. `t` may be seconds or ms."""
    best: tuple[float, int] | None = None
    for row in history:
        ts = _tick_ms(row.get("t"))
        price = _as_float(row.get("p") if "p" in row else row.get("price"))
        if ts is None or price is None or ts > start_time_ms:
            continue
        if best is None or ts >= best[1]:
            best = (price, ts)
    return best


def enrich_fill(
    fill: dict[str, Any],
    *,
    token_id: str | None,
    start_time_iso: str | None,
    ask: float | None,
    resp: dict[str, Any] | None,
) -> dict[str, Any]:
    buy_price, shares = parse_buy_fill(resp, ask)
    start_ms = iso_to_unix_ms(start_time_iso)
    if token_id:
        fill["token_id"] = token_id
    if start_time_iso:
        fill["start_time"] = start_time_iso
    if start_ms is not None:
        fill["start_time_ms"] = start_ms
    if buy_price is not None:
        fill["buy_price"] = buy_price
    if shares is not None:
        fill["shares"] = shares
    return fill


def _first_number(resp: dict[str, Any] | None, *keys: str) -> float | None:
    if not isinstance(resp, dict):
        return None
    for key in keys:
        value = _as_float(resp.get(key))
        if value is not None:
            return value
    return None


def _as_float(value: Any) -> float | None:
    if value is None or value == "":
        return None
    try:
        n = float(value)
    except (TypeError, ValueError):
        return None
    if n != n:  # NaN
        return None
    return n


def _tick_ms(value: Any) -> int | None:
    n = _as_float(value)
    if n is None:
        return None
    ts = int(n)
    if ts < 1_000_000_000_000:
        ts *= 1000
    return ts
