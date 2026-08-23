# polymaker

Polymarket client foundation for this repo: **scan sporting (and political)
events** via Gamma, and **buy outcomes** via the CLOB with your wallet.

Market-making / quoting / sell-exit strategy logic has been removed. Add your
own decision logic on top of the catalog + `ExecutionGateway`.

## Install

Uses [uv](https://docs.astral.sh/uv/) and Python 3.12+.

```bash
cd trading-bot
uv sync --extra dev
uv run polymaker --help
```

## Configure

Create a `.env` in `trading-bot/`:

```bash
POLY_PRIVATE_KEY=0x...
POLY_FUNDER=0x...          # deposit/proxy address (or same as key for EOA)
POLYGON_RPC_URL=https://... # optional

# Required for `polymaker research`
ANTHROPIC_API_KEY=...
# ANTHROPIC_MODEL=claude-haiku-4-5-20251001
```

Wallet / catalog settings live in [`config/config.toml`](config/config.toml).
Set `signature_type` correctly for your Polymarket account (see comments there).

## Use

```bash
# discover MLB/WNBA moneylines (+ politics by default) → state.db + markets.csv
uv run polymaker scan
uv run polymaker scan --sports-only
uv run polymaker markets

# MLB research: one slate search → summarize → size Breton plays + consensus extras
uv run polymaker research \
  --plays ../MLB/static-json/llm_best_plays.json \
  --matchups ../MLB/json/matchups.json \
  --out output/sized_plays.json \
  --hours 12 \
  --articles 15

# Sharp money → Polymarket (after data-aggregation/find_sharp_money.py)
uv run polymaker match-sharp --refresh
uv run polymaker match-sharp --league wnba
uv run polymaker trade-sharp --league wnba --refresh   # dry-run
uv run polymaker trade-sharp --league wnba --live
uv run polymaker trade-sharp --tier A --usd-a 25 --no-refresh

# wallet preflight (no orders)
uv run polymaker doctor

# buy (market) — spends real USDC
uv run polymaker buy <slug> --outcome yes --usd 10
uv run polymaker buy <slug> --outcome yes --usd 10 --dry-run

# buy (limit GTC)
uv run polymaker buy <slug> --outcome yes --limit 0.42 --size 20

# ops
uv run polymaker status
uv run polymaker cancel-all
```

## Sharp-money auto trading

Pipeline:

1. Scrape splits → `data-aggregation/output/{mlb,wnba}_betting_splits.json`
2. `find_sharp_money.py` → `{mlb,wnba}_sharp_money.json`
3. `polymaker trade-sharp` maps plays to Polymarket moneylines
   (`mlb-ari-atl-…` / `wnba-dal-gsv-…`) and market-buys the sharp side

Matching uses betting abbrs → Polymarket codes (e.g. `AZ`→`ari`, `LV`→`las`,
`GS`→`gsv`), then Gamma/catalog lookup by away/home + game date. Spreads are
skipped until the catalog includes them (`[sharp] markets = ["moneyline"]`).

Use `--league mlb|wnba|both` to scope which sharp file(s) are loaded. Today's
`wnba_sharp_money.json` can have `play_count: 0` when no gaps clear the
thresholds — re-run `find_sharp_money.py` on a fresh splits scrape.

Defaults in [`config/config.toml`](config/config.toml) `[sharp]`: Tier A/B USDC sizes, `max_ask`, optional `min_edge` vs `implied_fair_prob`, and a dedupe log at `journal/sharp_trades.jsonl`. **`trade-sharp` is dry-run unless you pass `--live`.**

Suggested daily loop once tomorrow's slate is live:

```bash
# refresh splits + sharp JSON (your existing scripts), then:
uv run polymaker trade-sharp --refresh          # inspect dry-run
uv run polymaker trade-sharp --refresh --live   # send buys
```

Production (EC2 eu-west-1) is documented in [`infra/README.md`](infra/README.md): a 30-minute systemd timer runs `scripts/run_sharp_pipeline.py` (trades only when all scrape sources share today's Pacific slate) and `scripts/run_monitor.py` stays up for whale + smart-wallet signals. Live buys require `POLYMAKER_LIVE=1` and Convex claim/complete so the two bots cannot fill the same market twice.

## Layout

```
catalog/     Gamma scan (politics + sports moneylines) → SQLite
execution/   ExecutionGateway — connect wallet, place/cancel, market buy, balances
research/    MLB article research agents → sized_plays.json
trading/     sharp-money → market match + auto-buy
domain.py    MarketMeta, Quote, Side, …
config.py    TOML + .env
cli.py       scan / markets / research / match-sharp / trade-sharp / doctor / buy / …
```

### Research pipeline

`polymaker research` searches once for `MLB {Month} {Day} Best Bets` (up to 15
recent articles), summarizes each against today's `matchups.json` slate, then:

1. **Sizes Breton plays** from `llm_best_plays.json` by comparing support vs
   opposite-side mentions:
   - **≥2 articles** support our side more than the other → **2.0u**
   - **≥2 articles** back the opposite side more → **0.5u**
   - otherwise → **1.0u**
2. **Adds consensus plays** for slate games *not* already in
   `llm_best_plays.json` when ≥2 articles agree on the same moneyline / run
   line / total.

Output lands in `output/sized_plays.json` (`ml_best_plays`, `ou_best_plays`,
and `additional_plays`).

Hook new strategy code by calling `CatalogStore` / `run_scan` for events and
`ExecutionGateway.market_order` / `place` for buys.

## Develop

```bash
uv run pytest
uv run ruff check src tests
uv run mypy src
```

## License

MIT — see [LICENSE](LICENSE).
