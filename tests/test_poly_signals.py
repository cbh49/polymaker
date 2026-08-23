"""poly-sharp-finder detector: whale + convergence, no volume spikes."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

_FINDER = Path(__file__).resolve().parents[1] / "poly-sharp-finder"
if str(_FINDER) not in sys.path:
    sys.path.insert(0, str(_FINDER))

from detector import SignalDetector  # noqa: E402
from registry import WatchedMarket  # noqa: E402
from wallet_store import WalletStore  # noqa: E402


def _market() -> WatchedMarket:
    return WatchedMarket(
        condition_id="0xabc",
        league="MLB",
        label="NYY vs BOS ML",
        yes_token_id="yes",
        no_token_id="no",
        yes_outcome="New York Yankees",
        no_outcome="Boston Red Sox",
        slug="mlb-bos-nyy-2026-08-22",
    )


def test_whale_emits_and_large_notional_is_not_a_volume_spike(tmp_path: Path) -> None:
    store = WalletStore(path=str(tmp_path / "wallets.json"))
    det = SignalDetector(store)
    market = _market()
    # $60k trade is a whale; historically this also fired volume_spike.
    signals = det.on_trade(market, "yes", price=0.5, size_shares=120_000, wallet="0x1", ts=1.0)
    types = {s.signal_type for s in signals}
    assert "whale_trade" in types
    assert "volume_spike" not in types


def test_small_trade_emits_nothing(tmp_path: Path) -> None:
    store = WalletStore(path=str(tmp_path / "wallets.json"))
    det = SignalDetector(store)
    signals = det.on_trade(_market(), "yes", price=0.5, size_shares=10, wallet="0x2", ts=1.0)
    assert signals == []


def test_convergence_requires_smart_wallets(tmp_path: Path) -> None:
    store = WalletStore(path=str(tmp_path / "wallets.json"))
    for addr in ("0xa", "0xb", "0xc"):
        stats = store.get(addr)
        stats.wins = 20
        stats.losses = 5
        store._wallets[addr] = stats
    det = SignalDetector(store)
    market = _market()
    seen = []
    for i, addr in enumerate(("0xa", "0xb", "0xc")):
        seen.extend(det.on_trade(market, "yes", price=0.4, size_shares=10, wallet=addr, ts=100.0 + i))
    types = {s.signal_type for s in seen}
    assert "convergence" in types
    assert "volume_spike" not in types


@pytest.mark.asyncio
async def test_executor_skips_started_game() -> None:
    from config import TradeConfig
    from detector import Signal
    from executor import SignalExecutor

    from polymaker.config import Config

    market = _market()
    market.start_time = "2020-01-01T00:00:00Z"
    ex = SignalExecutor(Config(), [market], TradeConfig())
    sig = Signal(
        ts=1.0,
        condition_id=market.condition_id,
        league=market.league,
        label=market.label,
        signal_type="whale_trade",
        side="yes",
        detail={"size_usd": 60_000},
    )
    result = await ex.on_signal(sig)
    assert result is not None
    assert result["action"] == "skipped"
    assert "pre-game" in result["detail"]


def _future_market() -> WatchedMarket:
    market = _market()
    market.start_time = "2099-01-01T00:00:00Z"
    return market


def _whale_sig(market: WatchedMarket, *, price: float | None) -> object:
    from detector import Signal

    detail: dict = {"size_usd": 60_000}
    if price is not None:
        detail["price"] = price
    return Signal(
        ts=1.0,
        condition_id=market.condition_id,
        league=market.league,
        label=market.label,
        signal_type="whale_trade",
        side="yes",
        detail=detail,
    )


@pytest.mark.asyncio
async def test_executor_skips_price_above_max_ask(tmp_path: Path) -> None:
    from dataclasses import replace

    from config import TradeConfig
    from executor import SignalExecutor

    from polymaker.config import Config

    market = _future_market()
    trade = replace(
        TradeConfig(),
        intents_log=str(tmp_path / "intents.jsonl"),
        filled_log=str(tmp_path / "filled.jsonl"),
        max_ask=0.55,
    )
    ex = SignalExecutor(Config(), [market], trade)
    result = await ex.on_signal(_whale_sig(market, price=0.999))  # type: ignore[arg-type]
    assert result is not None
    assert result["action"] == "skipped"
    assert "max_ask" in result["detail"]


@pytest.mark.asyncio
async def test_executor_dry_run_when_price_below_max_ask(tmp_path: Path) -> None:
    from dataclasses import replace

    from config import TradeConfig
    from executor import SignalExecutor

    from polymaker.config import Config

    market = _future_market()
    trade = replace(
        TradeConfig(),
        intents_log=str(tmp_path / "intents.jsonl"),
        filled_log=str(tmp_path / "filled.jsonl"),
        max_ask=0.55,
        dry_run=True,
    )
    ex = SignalExecutor(Config(), [market], trade)
    result = await ex.on_signal(_whale_sig(market, price=0.45))  # type: ignore[arg-type]
    assert result is not None
    assert result["action"] == "dry_run"


@pytest.mark.asyncio
async def test_executor_skips_when_no_price(tmp_path: Path) -> None:
    from dataclasses import replace

    from config import TradeConfig
    from executor import SignalExecutor

    from polymaker.config import Config

    market = _future_market()
    trade = replace(
        TradeConfig(),
        intents_log=str(tmp_path / "intents.jsonl"),
        filled_log=str(tmp_path / "filled.jsonl"),
        max_ask=0.55,
    )
    ex = SignalExecutor(Config(), [market], trade)
    result = await ex.on_signal(_whale_sig(market, price=None))  # type: ignore[arg-type]
    assert result is not None
    assert result["action"] == "skipped"
    assert "no ask" in result["detail"]
