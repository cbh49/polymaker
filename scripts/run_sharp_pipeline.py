#!/usr/bin/env python3
"""Production MLB/WNBA sharp-money loop.

Scrapes splits, trades only when every required source is on today's Pacific
slate, then refreshes the Polymarket watch list. Live CLOB buys require
POLYMAKER_LIVE=1.

Usage (from trading-bot/):
  uv run python scripts/run_sharp_pipeline.py
  uv run python scripts/run_sharp_pipeline.py --dry-run
  uv run python scripts/run_sharp_pipeline.py --league mlb --date 2026-08-22
"""

from __future__ import annotations

import argparse
import asyncio
import json
import subprocess
import sys
from datetime import date
from pathlib import Path
from typing import Any

_ROOT = Path(__file__).resolve().parents[1]
_AGG = _ROOT / "data-aggregation"
_SRC = _ROOT / "src"
for _p in (_SRC, _AGG, str(_ROOT / "poly-sharp-finder")):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

from find_sharp_money import (  # noqa: E402
    _frame_for_csv,
    _markets_from_arg,
    print_summary,
    process_slate,
    slate_to_json,
)
from slate_alignment import evaluate_payload, pacific_today  # noqa: E402

from polymaker.catalog.store import CatalogStore  # noqa: E402
from polymaker.config import Config  # noqa: E402
from polymaker.trading.convex_trades import (  # noqa: E402
    ConvexTradeClient,
    live_trading_enabled,
)
from polymaker.trading.execute import (  # noqa: E402
    filter_plays,
    load_configured_plays,
    run_sharp_trades,
)
from polymaker.trading.match import match_sharp_plays  # noqa: E402


def _run_scraper(script: str, extra: list[str]) -> bool:
    cmd = [sys.executable, str(_AGG / script), *extra]
    print("+", " ".join(cmd), flush=True)
    proc = subprocess.run(cmd, cwd=str(_ROOT))
    if proc.returncode != 0:
        print(f"ERROR: {script} exited {proc.returncode}", file=sys.stderr)
        return False
    return True


def _write_sharp(splits_path: Path, json_out: Path, csv_out: Path, market: str) -> dict[str, Any]:
    payload = json.loads(splits_path.read_text(encoding="utf-8"))
    markets = _markets_from_arg(market)
    frame = process_slate(payload, markets=markets)
    output = slate_to_json(payload, frame, splits_path, markets)
    json_out.parent.mkdir(parents=True, exist_ok=True)
    json_out.write_text(json.dumps(output, indent=2) + "\n", encoding="utf-8")
    _frame_for_csv(frame).to_csv(csv_out, index=False)
    print_summary(frame)
    print(f"JSON → {json_out}")
    return output


def _trade_league(cfg: Config, league: str, *, live: bool) -> list[dict[str, Any]]:
    from polymaker.cli import _refresh_sports_catalog, _sharp_trade_cfg

    try:
        plays = load_configured_plays(cfg, league=league)
    except FileNotFoundError as exc:
        print(f"skip trade {league}: {exc}")
        return []
    trade_cfg = _sharp_trade_cfg(cfg, dry_run=not live)
    plays = filter_plays(plays, trade_cfg, league=league)
    if not plays:
        print(f"No sharp plays after filters (league={league}).")
        return []
    _refresh_sports_catalog(cfg)
    store = CatalogStore(cfg.paths.db)

    async def _go() -> list[Any]:
        matched = await match_sharp_plays(plays, store=store, markets=trade_cfg.markets)
        return await run_sharp_trades(matched, cfg, trade_cfg)

    try:
        results = asyncio.run(_go())
    finally:
        store.close()

    rows: list[dict[str, Any]] = []
    for r in results:
        m = r.matched
        rows.append(
            {
                "action": r.action,
                "usd": r.usd,
                "league": m.play.league,
                "matchup": m.play.matchup,
                "side": m.token.outcome if m.token else m.play.side,
                "slug": m.slug,
                "tier": m.play.tier,
                "detail": r.detail,
            }
        )
        print(f"  {r.action:8s} ${r.usd:>6.2f} {m.play.tier} {m.play.matchup} {r.detail}")
    return rows


def _refresh_watch_list(config_dir: str) -> None:
    from export_watch_list import export_watch_list

    out = _ROOT / "poly-sharp-finder" / "watch_list.json"
    rows = export_watch_list(config_dir=config_dir, out_path=out, refresh=True, include_sharp=True)
    print(f"Watch list: {len(rows)} markets → {out}")


def main() -> int:
    parser = argparse.ArgumentParser(description="MLB/WNBA sharp-money scrape + trade")
    parser.add_argument("--date", default=None, help="Slate date YYYY-MM-DD (default: today Pacific)")
    parser.add_argument("--league", default="both", choices=["mlb", "wnba", "both"])
    parser.add_argument("--config-dir", default="config")
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Never send CLOB orders (overrides POLYMAKER_LIVE)",
    )
    args = parser.parse_args()

    day: date = date.fromisoformat(args.date) if args.date else pacific_today()
    live = live_trading_enabled() and not args.dry_run
    mode = "LIVE" if live else "DRY-RUN"
    print(f"Sharp pipeline {mode} slate={day.isoformat()} league={args.league}")

    wanted = []
    if args.league in {"mlb", "both"}:
        wanted.append("mlb")
    if args.league in {"wnba", "both"}:
        wanted.append("wnba")

    date_args = ["--date", day.isoformat()]
    scrape_ok = {
        "mlb": True,
        "wnba": True,
    }
    if "mlb" in wanted:
        scrape_ok["mlb"] = _run_scraper("scrape_mlb_betting_splits.py", date_args)
    if "wnba" in wanted:
        scrape_ok["wnba"] = _run_scraper("scrape_wnba_betting_splits.py", date_args)

    cfg = Config.load(args.config_dir)
    trade_rows: list[dict[str, Any]] = []
    alignments: dict[str, dict[str, Any]] = {}

    specs = {
        "mlb": (
            _AGG / "output" / "mlb_betting_splits.json",
            _AGG / "output" / "mlb_sharp_money.json",
            _AGG / "output" / "mlb_sharp_money.csv",
            "moneyline",
        ),
        "wnba": (
            _AGG / "output" / "wnba_betting_splits.json",
            _AGG / "output" / "wnba_sharp_money.json",
            _AGG / "output" / "wnba_sharp_money.csv",
            "both",
        ),
    }

    for league in wanted:
        splits_path, json_out, csv_out, market = specs[league]
        if not scrape_ok[league] or not splits_path.is_file():
            alignments[league] = {"aligned": False, "reason": "scrape failed"}
            print(f"Skip {league}: scrape failed")
            continue
        payload = json.loads(splits_path.read_text(encoding="utf-8"))
        result = evaluate_payload(payload, slate_day=day)
        alignments[league] = result.as_dict()
        print(result.reason)
        if not result.aligned:
            continue
        _write_sharp(splits_path, json_out, csv_out, market)
        trade_rows.extend(_trade_league(cfg, league, live=live))

    try:
        _refresh_watch_list(args.config_dir)
    except Exception as exc:  # noqa: BLE001
        print(f"Watch list refresh failed: {exc}", file=sys.stderr)

    snapshot = {
        "slate_day": day.isoformat(),
        "mode": mode.lower(),
        "alignments": alignments,
        "trades": trade_rows,
    }
    convex = ConvexTradeClient()
    if convex.configured:
        try:
            convex.publish_snapshot(prediction_date=day.isoformat(), payload=snapshot)
            print("Published polymarket_trades snapshot to Convex")
        except Exception as exc:  # noqa: BLE001
            print(f"Convex snapshot failed: {exc}", file=sys.stderr)
    else:
        print("Convex not configured; skipped polymarket_trades snapshot")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
