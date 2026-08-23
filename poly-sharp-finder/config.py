"""
Config / thresholds for the Polymarket sharp-signal monitor.

Tradable by default: whale_trade + smart-wallet convergence.
Book imbalance + fast_move are logged for research but are NOT in the default
tradable set. Volume spikes are not tracked.
"""

from dataclasses import dataclass


@dataclass
class Thresholds:
    # --- Trade size tiers (USDC notional) ---
    notable_trade_usd: float = 10_000
    whale_trade_usd: float = 50_000
    major_trade_usd: float = 100_000

    # --- Order book microstructure (log-only by default; see TradeConfig) ---
    max_spread_cents: float = 3.0        # wider than this → ignore imbalance
    imbalance_ratio_alert: float = 5.0   # resting size one side / other side
    min_depth_usd_for_signal: float = 5_000  # ignore near-empty books

    # --- Wallet quality filter ---
    min_wallet_trades_for_track_record: int = 15
    min_wallet_win_rate: float = 0.55

    # --- Convergence ---
    convergence_wallet_count: int = 3
    convergence_window_minutes: int = 180

    # --- Price velocity (log-only by default; yes-token mid only) ---
    fast_move_cents: float = 8.0
    fast_move_window_seconds: int = 120


@dataclass
class Endpoints:
    clob_ws: str = "wss://ws-subscriptions-clob.polymarket.com/ws/market"
    data_api_trades: str = "https://data-api.polymarket.com/trades"
    gamma_markets: str = "https://gamma-api.polymarket.com/markets"


@dataclass
class TradeConfig:
    """Signal → order execution knobs. Dry-run by default; --live flips dry_run."""

    enabled: bool = True
    dry_run: bool = True
    usd_per_signal: float = 10.0
    max_ask: float = 0.55  # never buy if best ask / last price is above this
    filled_log: str = "journal/poly_sharp_signals.jsonl"
    # Would-buys (dry-run) + successful live fills — sit next to signals/ for easy review.
    intents_log: str = "poly-sharp-finder/intents/poly_sharp_intents.jsonl"
    # book_imbalance / fast_move stay log-only — too noisy on sports books.
    signal_types: tuple[str, ...] = (
        "whale_trade",
        "convergence",
    )
    # What gets written to signals/*.jsonl (default = actionable types only).
    # Set to include book_imbalance/fast_move if you want research dumps.
    persist_signal_types: tuple[str, ...] = (
        "whale_trade",
        "convergence",
    )
    # One buy per market per UTC day (not per side) — avoids buying both outcomes.
    dedupe_per_market: bool = True
    # Only trade when event.startTime is more than this many minutes in the future.
    pregame_buffer_minutes: float = 5.0


THRESHOLDS = Thresholds()
ENDPOINTS = Endpoints()
TRADE = TradeConfig()
