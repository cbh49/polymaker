"""Fill-price parsing and closing-line value math."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from polymaker.trading.fill import (
    compute_clv,
    enrich_fill,
    iso_to_unix_ms,
    parse_buy_fill,
    select_closing_tick,
)


def test_parse_buy_fill_from_making_taking() -> None:
    price, shares = parse_buy_fill(
        {"makingAmount": "24.99", "takingAmount": "56.795"},
        ask=0.50,
    )
    assert shares == pytest.approx(56.795)
    assert price == pytest.approx(24.99 / 56.795)


def test_parse_buy_fill_falls_back_to_ask() -> None:
    price, shares = parse_buy_fill({"status": "matched"}, ask=0.44)
    assert price == 0.44
    assert shares is None


def test_iso_to_unix_ms() -> None:
    expected = int(datetime(2026, 8, 23, 17, 5, tzinfo=UTC).timestamp() * 1000)
    assert iso_to_unix_ms("2026-08-23T17:05:00Z") == expected


def test_clv_positive_when_bought_below_close() -> None:
    clv, cents = compute_clv(0.44, 0.455)
    assert clv == pytest.approx(0.015)
    assert cents == 1.5


def test_select_closing_tick_last_at_or_before_start() -> None:
    start_sec = 1_700_000_000
    tick = select_closing_tick(
        [
            {"t": start_sec - 120, "p": 0.40},
            {"t": start_sec - 5, "p": 0.455},
            {"t": start_sec + 30, "p": 0.60},
        ],
        start_sec * 1000,
    )
    assert tick is not None
    assert tick[0] == 0.455
    _clv, cents = compute_clv(0.44, tick[0])
    assert cents == 1.5


def test_enrich_fill_adds_clv_fields() -> None:
    start = "2026-08-23T17:05:00Z"
    fill = enrich_fill(
        {"ask": 0.44},
        token_id="tok-jay",
        start_time_iso=start,
        ask=0.44,
        resp={"makingAmount": "24.99", "takingAmount": "56.8"},
    )
    assert fill["token_id"] == "tok-jay"
    assert fill["start_time_ms"] == iso_to_unix_ms(start)
    assert fill["buy_price"] == pytest.approx(24.99 / 56.8)
    assert fill["shares"] == 56.8
