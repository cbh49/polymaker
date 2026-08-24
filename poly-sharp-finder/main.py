"""
Entry point. Runs the websocket order-book listener and the REST trade
poller concurrently against today's watch list, feeding a shared detector,
logging every flagged signal, and optionally buying (dry-run by default).

Usage (from trading-bot/):
    uv run python poly-sharp-finder/main.py --watchlist poly-sharp-finder/watch_list.json
    uv run python poly-sharp-finder/main.py --watchlist poly-sharp-finder/watch_list.json --live
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from dataclasses import replace
from pathlib import Path

from bootstrap import ensure_paths

_ROOT = ensure_paths()
sys.path.insert(0, str(Path(__file__).resolve().parent))

import aiohttp

from config import TRADE, TradeConfig
from detector import SignalDetector
from executor import SignalExecutor
from registry import load_watch_list
from signal_logger import SignalLogger
from trade_poller import TradePoller
from wallet_store import WalletStore
from ws_client import ClobWebSocketClient

from polymaker.catalog.store import CatalogStore
from polymaker.catalog.sports import is_pre_game
from polymaker.config import Config


async def main(
    watchlist_path: str,
    *,
    config_dir: str = "config",
    trade_cfg: TradeConfig,
) -> None:
    cfg = Config.load(config_dir)
    trade_cfg = replace(
        trade_cfg,
        pregame_buffer_minutes=cfg.catalog.pregame_buffer_minutes,
        max_ask=cfg.sharp.max_ask,
    )

    markets = load_watch_list(watchlist_path)
    while not markets:
        print("No markets in watch list — waiting 60s for the sharp pipeline to export one.")
        await asyncio.sleep(60)
        markets = load_watch_list(watchlist_path)

    keep: list = []
    skipped_started = 0
    for m in markets:
        if m.start_time:
            if not is_pre_game({"startTime": m.start_time}, trade_cfg.pregame_buffer_minutes):
                skipped_started += 1
                continue
        keep.append(m)
    markets = keep
    if skipped_started:
        print(f"Dropped {skipped_started} watch-list markets that are not pre-game (startTime).")
    while not markets:
        print("No pre-game markets in watch list — waiting 60s for a refresh.")
        await asyncio.sleep(60)
        markets = [
            m
            for m in load_watch_list(watchlist_path)
            if not m.start_time
            or is_pre_game({"startTime": m.start_time}, trade_cfg.pregame_buffer_minutes)
        ]

    mode = "DRY-RUN" if trade_cfg.dry_run else "LIVE"
    if not trade_cfg.enabled:
        mode = "MONITOR-ONLY"
    print(f"Loaded {len(markets)} markets to watch ({mode}):")
    for m in markets:
        tier_note = f" [sharp {m.sharp_tier}: {m.sharp_side}]" if m.sharp_tier else ""
        print(f"  - {m.league}: {m.label}{tier_note}")

    store = CatalogStore(cfg.paths.db)

    def get_meta(condition_id: str):
        return store.get(condition_id)

    wallet_store = WalletStore(path=str(Path(__file__).resolve().parent / "wallet_stats.json"))
    detector = SignalDetector(wallet_store=wallet_store)
    logger = SignalLogger(
        out_dir=str(Path(__file__).resolve().parent / "signals"),
        persist_types=trade_cfg.persist_signal_types,
    )
    executor = SignalExecutor(cfg, markets, trade_cfg, store_get_meta=get_meta)
    markets_by_id = {m.condition_id: m for m in markets}

    async def _handle_trade(sig):
        try:
            await executor.on_signal(sig)
        except Exception as exc:  # noqa: BLE001
            print(f"[executor] error on signal: {exc}")

    async def _handle_tweet(sig):
        try:
            from whale_tweets import maybe_post_whale

            market = markets_by_id.get(sig.condition_id)
            await asyncio.to_thread(maybe_post_whale, sig, market)
        except Exception as exc:  # noqa: BLE001
            print(f"[whale-tweet] error on signal: {exc}")

    def on_signal(sig):
        # WS/poller callbacks are sync; schedule trading on the running loop.
        logger.log(sig)
        asyncio.create_task(_handle_trade(sig))
        if sig.signal_type == "whale_trade":
            asyncio.create_task(_handle_tweet(sig))

    await executor.start()

    async with aiohttp.ClientSession() as session:
        ws_client = ClobWebSocketClient(markets=markets, detector=detector, on_signal=on_signal)
        poller = TradePoller(session=session, detector=detector, markets=markets, on_signal=on_signal)

        try:
            await asyncio.gather(
                ws_client.run(),
                poller.run(),
            )
        finally:
            wallet_store.save()
            await executor.aclose()
            store.close()


def _resolve_path(p: str) -> str:
    path = Path(p)
    if path.is_file() or path.is_absolute():
        return str(path)
    alt = _ROOT / p
    if alt.exists():
        return str(alt)
    return p


def _resolve_config_dir(config_dir: str) -> str:
    path = Path(config_dir)
    if path.is_dir():
        return str(path)
    alt = _ROOT / config_dir
    if alt.is_dir():
        return str(alt)
    return config_dir


if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description="Polymarket sharp-signal monitor (+ optional dry-run/live trading)"
    )
    parser.add_argument(
        "--watchlist",
        default="poly-sharp-finder/watch_list.json",
        help="Path to today's matched Polymarket markets",
    )
    parser.add_argument("--config-dir", default="config", help="polymaker config directory")
    parser.add_argument(
        "--live",
        action="store_true",
        help="send real FAK market buys (default is dry-run)",
    )
    parser.add_argument(
        "--no-trade",
        action="store_true",
        help="monitor + log only; never buy (even dry-run intents)",
    )
    parser.add_argument("--usd", type=float, default=None, help="USDC per signal (default from config)")
    args = parser.parse_args()

    trade_cfg = replace(
        TRADE,
        dry_run=not args.live,
        enabled=not args.no_trade,
        usd_per_signal=args.usd if args.usd is not None else TRADE.usd_per_signal,
    )
    # Resolve journal paths relative to trading-bot root
    if not Path(trade_cfg.filled_log).is_absolute():
        trade_cfg = replace(trade_cfg, filled_log=str(_ROOT / trade_cfg.filled_log))
    if not Path(trade_cfg.intents_log).is_absolute():
        trade_cfg = replace(trade_cfg, intents_log=str(_ROOT / trade_cfg.intents_log))

    if args.live and args.no_trade:
        print("Cannot combine --live and --no-trade", file=sys.stderr)
        raise SystemExit(2)

    try:
        asyncio.run(
            main(
                _resolve_path(args.watchlist),
                config_dir=_resolve_config_dir(args.config_dir),
                trade_cfg=trade_cfg,
            )
        )
    except KeyboardInterrupt:
        print("\nStopped.")
    except FileNotFoundError as exc:
        print(f"Missing file: {exc}", file=sys.stderr)
        print("Export a watch list first: uv run python poly-sharp-finder/export_watch_list.py --refresh",
              file=sys.stderr)
        raise SystemExit(1)
