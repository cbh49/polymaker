# Polymarket Sharp-Signal Monitor

Websocket + polling monitor for MLB/WNBA Polymarket moneylines. Flags whale
trades, smart-wallet convergence, order-book imbalance, and fast price
moves — then optionally buys whale / convergence signals.

**Default is dry-run** (logs `would BUY`, no orders). Pass `--live` to send
real FAK market buys via the parent `polymaker` wallet (`.env` + `config/`).

## Quick start (from `trading-bot/`)

```bash
# 1. Install deps (includes aiohttp)
uv sync

# 2. Preflight wallet / CLOB (needed before --live)
uv run polymaker doctor

# 3. Build today's watch list from the sports catalog
uv run python poly-sharp-finder/export_watch_list.py --refresh

# 4. Optional: verify live trade + WS schemas
uv run python poly-sharp-finder/probe_feeds.py --watchlist poly-sharp-finder/watch_list.json

# 5. Monitor + dry-run trading (default)
uv run python poly-sharp-finder/main.py --watchlist poly-sharp-finder/watch_list.json

# Monitor only (no buy intents)
uv run python poly-sharp-finder/main.py --watchlist poly-sharp-finder/watch_list.json --no-trade

# LIVE buys ($10 default; override with --usd)
uv run python poly-sharp-finder/main.py --watchlist poly-sharp-finder/watch_list.json --live
```

## Files

| File | Role |
|---|---|
| `config.py` | Thresholds + `TradeConfig` (size, max_ask, signal types) |
| `registry.py` | Loads watch list (`WatchedMarket`) |
| `export_watch_list.py` | Catalog → `watch_list.json` (+ sharp annotations) |
| `wallet_store.py` | Persisted per-wallet trade count / win rate |
| `baseline.py` | Rolling per-market volume baseline for z-score |
| `detector.py` | Turns raw trades/book updates into flagged signals |
| `ws_client.py` | CLOB websocket — order book / price |
| `trade_poller.py` | REST poll of Data API `/trades` |
| `executor.py` | Signal → dry-run / live FAK buy via `ExecutionGateway` |
| `signal_logger.py` | Writes every signal to JSONL + CSV |
| `probe_feeds.py` | One-shot live schema smoke-check |
| `main.py` | Runs WS + poller + executor concurrently |
| `watch_list.example.json` | Format for the daily market list |

## Trading behavior

- **Dry-run by default**; `--live` sends orders
- **$10 USDC** per signal (`TradeConfig.usd_per_signal` / `--usd`)
- **max_ask 0.85** — skip if best ask is higher
- **Dedupe** — one fill per market (`condition_id`) per UTC day
- **Would-buys / fills:** `poly-sharp-finder/intents/poly_sharp_intents.jsonl`
  - `side` — team ML to buy (e.g. `"New York Mets"`)
  - `token_side` — Polymarket token (`"yes"` / `"no"`)
  - `signal` — trigger metrics (`size_usd`, `tier`, `wallet` for whales)
- **Live fills also:** `journal/poly_sharp_signals.jsonl`
- **Actionable signals file:** `poly-sharp-finder/signals/polymarket_signals_*.jsonl`
  (whale / convergence only — not book noise)
- **Tradable signals:** `whale_trade`, `convergence`
- **Not tracked:** volume spikes
- **Not traded / not persisted by default:** `book_imbalance`, `fast_move`
- Uses parent secrets: `POLY_PRIVATE_KEY`, `POLY_FUNDER` in `.env` and
  `signature_type` in `config/config.toml`

## Wallet win-rate / convergence

`wallet_store.py` starts every wallet at 0 resolved trades, so
`is_smart_money()` returns `False` until you add a resolution-tracking job.
Until then `convergence` will not fire; other signal types still will.

## Tuning

Edit thresholds and trade knobs in `config.py`. Starting values are first
guesses — adjust after you've logged signals against outcomes.
