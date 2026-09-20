#!/usr/bin/env python3
"""Refresh NFL odds from every source, then print tradable Kalshi/Polymarket +EV.

  uv run python scripts/run_nfl_ev_pipeline.py

With execution (dry-run until --live or POLYMAKER_LIVE=1):

  uv run python scripts/run_nfl_ev_pipeline.py --trade kalshi
  uv run python scripts/run_nfl_ev_pipeline.py --trade polymarket
  uv run python scripts/run_nfl_ev_pipeline.py --trade both
  uv run python scripts/run_nfl_ev_pipeline.py --trade both --live

The AWS `ev` container runs `--trade both --min-edge-pct 5` and goes live when
POLYMAKER_LIVE=1. Rows need fee-adjusted edge >= 5 points and confidence >= 0.80.

Steps:
  1. RotoWire game odds  → supporting-lines/nfl_game_odds.json
  2. RotoWire player props → supporting-lines/nfl_player_props.json
  3. Kalshi week markets → venues/kalshi/nfl_week_markets.json
  4. Polymarket NFL odds → data-aggregation/output/polymarket_nfl_odds.json
  5. Join               → supporting-lines/nfl_aggregated_odds.json
  6. Fair-value report  → output/ev/nfl_fair_value.json
  7. Sportsbook/Kalshi/Polymarket +EV >= 5pp → Discord cards (all) + at most one
     X graphic if all 8 odds are filled (DISCORD_EV_WEBHOOK_URL / X_EV_POSTS)
     and Convex `evOpportunities` (CONVEX_HTTP_URL) for the dashboard board.
  8. Optional: buy `tradable` rows on Kalshi and/or Polymarket ($10 each)

1–4 run in parallel. Pass --sequential to run them one at a time.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from ev_trading.fair_value.ev_ledger import (
    DEFAULT_MIN_EDGE_PCT,
    venues_from_trade_arg,
)
from ev_trading.fair_value.kalshi_execute import (
    DEFAULT_FILLED_LOG as KALSHI_FILLED_LOG,
)
from ev_trading.fair_value.kalshi_execute import (
    DEFAULT_MIN_CONFIDENCE,
    DEFAULT_USD,
    KalshiTradeConfig,
    run_kalshi_trades,
)
from ev_trading.fair_value.kalshi_execute import (
    render_trade_results as render_kalshi_results,
)
from ev_trading.fair_value.polymarket_execute import (
    DEFAULT_FILLED_LOG as POLY_FILLED_LOG,
)
from ev_trading.fair_value.polymarket_execute import (
    PolyTradeConfig,
    run_polymarket_trades,
)
from ev_trading.fair_value.polymarket_execute import (
    render_trade_results as render_poly_results,
)
from ev_trading.nfl_refresh import REPORT_OUT, run_nfl_refresh
from polymaker.trading.convex_trades import live_trading_enabled


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Refresh NFL sportsbook + Kalshi + Polymarket odds and find +EV"
    )
    parser.add_argument("--skip-books", action="store_true", help="keep existing RotoWire JSON")
    parser.add_argument("--skip-kalshi", action="store_true", help="keep existing Kalshi JSON")
    parser.add_argument("--skip-poly", action="store_true", help="keep existing Polymarket JSON")
    parser.add_argument("--report-only", action="store_true", help="skip all scrapes; score current aggregate")
    parser.add_argument("--no-report", action="store_true", help="refresh + aggregate only, skip EV tables")
    parser.add_argument("--sequential", action="store_true", help="scrape sources one at a time")
    parser.add_argument("--config", default="config/fair_value.json")
    parser.add_argument("--out", default=str(REPORT_OUT), help="JSON report path")
    parser.add_argument("--quiet", action="store_true", help="skip console tables")
    parser.add_argument(
        "--trade",
        choices=("kalshi", "polymarket", "both"),
        metavar="VENUE",
        help="after the report, buy tradable rows on kalshi, polymarket, or both "
        f"($10, confidence >= 0.80, edge >= {DEFAULT_MIN_EDGE_PCT:g} pts)",
    )
    parser.add_argument(
        "--live",
        action="store_true",
        help="send orders on --trade venue(s); also on when POLYMAKER_LIVE=1",
    )
    parser.add_argument("--trade-usd", type=float, default=DEFAULT_USD, help="stake per row (default $10)")
    parser.add_argument(
        "--min-confidence",
        type=float,
        default=DEFAULT_MIN_CONFIDENCE,
        help="minimum tradable confidence (default 0.80)",
    )
    parser.add_argument(
        "--min-edge-pct",
        type=float,
        default=DEFAULT_MIN_EDGE_PCT,
        help="minimum fee-adjusted edge in percentage points (default 5)",
    )
    parser.add_argument(
        "--filled-log",
        default=None,
        help="JSONL of filled trades used for dedupe (single-venue --trade only)",
    )
    parser.add_argument(
        "--no-alerts",
        action="store_true",
        help="skip Discord/X +EV cards (sportsbooks, Kalshi, Polymarket)",
    )
    parser.add_argument(
        "--alerts-dry-run",
        action="store_true",
        help="render +EV cards and print Discord/X payloads without posting",
    )
    args = parser.parse_args()

    venues = venues_from_trade_arg(args.trade)
    live = bool(venues) and bool(args.live or live_trading_enabled())
    if args.live and not venues:
        parser.error("--live requires --trade kalshi|polymarket|both")
    if args.trade and args.no_report and not Path(args.out).is_file():
        parser.error("--trade --no-report needs an existing report at --out")
    if args.filled_log and len(venues) > 1:
        parser.error("--filled-log only applies to a single --trade venue, not both")

    result = run_nfl_refresh(
        sportsbooks=not args.report_only and not args.skip_books,
        kalshi=not args.report_only and not args.skip_kalshi,
        polymarket=not args.report_only and not args.skip_poly,
        aggregate=not args.report_only,
        report=not args.no_report,
        parallel=not args.sequential,
        config_path=args.config,
        report_out=args.out,
        quiet=args.quiet,
    )
    print(f"done in {result.elapsed_s:.1f}s  aggregated={result.aggregated}")

    if not args.no_alerts and result.report is not None:
        from ev_trading.fair_value.ev_alerts import post_ev_alerts

        try:
            summary = post_ev_alerts(
                result.report,
                result.aggregated,
                dry_run=args.alerts_dry_run,
                min_edge_pct=DEFAULT_MIN_EDGE_PCT,
            )
            print(
                f"EV alerts: posted={summary.get('posted', 0)} "
                f"skipped={summary.get('skipped', 0)} "
                f"reason={summary.get('reason')}"
            )
        except Exception as exc:  # noqa: BLE001
            print(f"EV alerts failed (continuing): {type(exc).__name__}: {exc}", flush=True)

    if not venues:
        return 0

    source = result.report if result.report is not None else Path(args.out)
    mode = "LIVE" if live else "dry-run"
    venue_label = "+".join(venues)
    print(
        f"{venue_label} {mode}: tradable ∩ venue={venue_label} ∩ "
        f"confidence>={args.min_confidence:.2f} ∩ "
        f"edge>={args.min_edge_pct:g}pts @ ${args.trade_usd:.2f}/row"
    )

    failed = 0
    if "kalshi" in venues:
        kalshi_results = run_kalshi_trades(
            source,
            KalshiTradeConfig(
                usd=args.trade_usd,
                min_confidence=args.min_confidence,
                min_edge_pct=args.min_edge_pct,
                dry_run=not live,
                filled_log=Path(args.filled_log) if args.filled_log else KALSHI_FILLED_LOG,
            ),
        )
        render_kalshi_results(kalshi_results)
        failed += sum(1 for row in kalshi_results if row.action == "failed")
    if "polymarket" in venues:
        poly_results = run_polymarket_trades(
            source,
            PolyTradeConfig(
                usd=args.trade_usd,
                min_confidence=args.min_confidence,
                min_edge_pct=args.min_edge_pct,
                dry_run=not live,
                filled_log=Path(args.filled_log) if args.filled_log else POLY_FILLED_LOG,
            ),
        )
        render_poly_results(poly_results)
        failed += sum(1 for row in poly_results if row.action == "failed")

    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
