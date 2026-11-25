"""Maker pricing, fees, and venue comparison."""

from __future__ import annotations

import pytest

from polymaker.config import Config, KalshiMakerFeeConfig
from polymaker.trading.maker import (
    assert_below_ask,
    effective_price,
    floor_to_tick,
    kalshi_maker_fee,
    maker_buy_price,
    pick_maker_venue,
    polymarket_maker_fee,
    price_maker_venue,
)


@pytest.mark.parametrize(
    ("ask", "tick", "expected"),
    [
        (0.51, 0.01, 0.50),
        (0.48, 0.01, 0.47),
        (0.60, 0.01, 0.59),
        (0.02, 0.01, 0.01),
        (0.515, 0.001, 0.505),
        (0.515, 0.01, 0.50),  # off-tick ask still rests strictly below it
    ],
)
def test_maker_price_is_one_tick_below_ask(ask: float, tick: float, expected: float) -> None:
    price = maker_buy_price(ask, tick)
    assert price == pytest.approx(expected)
    assert price is not None and price < ask


@pytest.mark.parametrize("ask", [None, 0.0, 1.0, 1.2, 0.01, float("nan")])
def test_maker_price_none_when_no_valid_price(ask: float | None) -> None:
    assert maker_buy_price(ask) is None


def test_maker_price_never_crosses_across_the_grid() -> None:
    for cents in range(2, 100):
        for tick in (0.01, 0.001):
            ask = cents / 100
            price = maker_buy_price(ask, tick)
            assert price is not None
            assert price < ask


def test_tick_floor_and_cross_guard() -> None:
    assert floor_to_tick(0.5049, 0.01) == pytest.approx(0.50)
    with pytest.raises(ValueError, match="not strictly below"):
        assert_below_ask(0.51, 0.51)
    assert_below_ask(0.50, 0.51)


def test_offset_larger_than_one_tick() -> None:
    assert maker_buy_price(0.55, 0.01, offset=0.03) == pytest.approx(0.52)


def test_kalshi_maker_fee_rounds_up_to_the_cent() -> None:
    # 0.0175 * 50 * 0.5 * 0.5 = 0.21875 -> $0.22
    assert kalshi_maker_fee(50, 0.50) == pytest.approx(0.22)
    assert kalshi_maker_fee(1, 0.50) == pytest.approx(0.01)  # never rounds down to 0
    assert kalshi_maker_fee(100, 0.30) == pytest.approx(0.37)  # 0.3675 -> 0.37


def test_kalshi_maker_fee_can_be_disabled() -> None:
    assert kalshi_maker_fee(50, 0.50, enabled=False) == 0.0


def test_polymarket_maker_fee_is_zero() -> None:
    assert polymarket_maker_fee(500, 0.50) == 0.0


def test_effective_price_example_from_the_spec() -> None:
    assert effective_price(0.50, 0.0, 50) == pytest.approx(0.5000)
    assert effective_price(0.50, 0.22, 50) == pytest.approx(0.5044)


def test_kalshi_fee_config_per_series() -> None:
    cfg = KalshiMakerFeeConfig(default_enabled=True, series={"KXNBAGAME": False})
    assert cfg.enabled_for("KXNBAGAME-26NOV25LALDEN-LAL") is False
    assert cfg.enabled_for("kxnbagame-x") is False
    assert cfg.enabled_for("KXMLBGAME-26AUG20-ATL") is True
    assert KalshiMakerFeeConfig(default_enabled=False).enabled_for("KXFOO-1") is False


def test_toml_config_loads_maker_blocks(tmp_path) -> None:
    (tmp_path / "config.toml").write_text(
        "[maker]\noffset = 0.02\ncancel_window_minutes = 45\n"
        "[kalshi_maker_fees]\nrate = 0.02\ndefault_enabled = false\n"
        "[kalshi_maker_fees.series]\nKXNHLGAME = true\n"
    )
    cfg = Config.load(tmp_path, load_env=False)
    assert cfg.maker.offset == 0.02
    assert cfg.maker.cancel_window_minutes == 45
    assert cfg.maker.post_only is True
    assert cfg.kalshi_maker_fees.rate == 0.02
    assert cfg.kalshi_maker_fees.enabled_for("KXNHLGAME-1") is True
    assert cfg.kalshi_maker_fees.enabled_for("KXNBAGAME-1") is False
    assert cfg.execution.post_only is True


def test_price_maker_venue_sizes_and_prices_both_venues() -> None:
    poly, why = price_maker_venue("polymarket", 0.51, 25.0, min_size=5)
    assert why is None and poly is not None
    assert poly.limit_price == pytest.approx(0.50)
    assert poly.size == pytest.approx(50.0)
    assert poly.fee == 0.0
    assert poly.effective_price == pytest.approx(0.50)

    kalshi, why = price_maker_venue("kalshi", 0.51, 25.0, ticker="KXNBAGAME-1")
    assert why is None and kalshi is not None
    assert kalshi.limit_price == pytest.approx(0.50)
    assert kalshi.size == 50
    assert kalshi.fee == pytest.approx(0.22)
    assert kalshi.effective_price == pytest.approx(0.5044)


def test_price_maker_venue_respects_min_size_and_series_flag() -> None:
    quote, why = price_maker_venue("polymarket", 0.51, 2.0, min_size=5)
    assert quote is None and "cannot size" in (why or "")
    free = KalshiMakerFeeConfig(series={"KXNBAGAME": False})
    kalshi, _ = price_maker_venue("kalshi", 0.51, 25.0, ticker="KXNBAGAME-1", kalshi_fees=free)
    assert kalshi is not None and kalshi.fee == 0.0
    assert kalshi.effective_price == pytest.approx(0.50)


def test_kalshi_needs_about_half_a_cent_better_to_win() -> None:
    poly, _ = price_maker_venue("polymarket", 0.51, 25.0, min_size=5)
    kalshi_same, _ = price_maker_venue("kalshi", 0.51, 25.0, ticker="KXA-1")
    assert pick_maker_venue([kalshi_same, poly]).venue == "polymarket"  # type: ignore[union-attr]
    kalshi_better, _ = price_maker_venue("kalshi", 0.50, 25.0, ticker="KXA-1")
    # limit 0.49 + fee/C beats polymarket 0.50
    assert pick_maker_venue([kalshi_better, poly]).venue == "kalshi"  # type: ignore[union-attr]


def test_tie_goes_to_polymarket() -> None:
    free = KalshiMakerFeeConfig(default_enabled=False)
    poly, _ = price_maker_venue("polymarket", 0.51, 25.0, min_size=5)
    kalshi, _ = price_maker_venue("kalshi", 0.51, 25.0, ticker="KXA-1", kalshi_fees=free)
    assert pick_maker_venue([kalshi, poly]).venue == "polymarket"  # type: ignore[union-attr]
    assert pick_maker_venue([]) is None
