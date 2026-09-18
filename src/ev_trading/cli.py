"""ev-trading CLI — fetch/compare odds across Polymarket, Kalshi, ProphetX, SX Bet.

  ev-trading fetch      pull quotes from each venue adapter (stubs until docs land)
  ev-trading compare    group a quotes.json snapshot by match key
  ev-trading run        fetch + compare → output/ev/
  ev-trading nfl-ev     sportsbook consensus vs Kalshi/Polymarket NFL prices
                        add --refresh to re-scrape books + Kalshi + Polymarket first
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import typer
from rich.console import Console

from ev_trading.config import EvConfig
from ev_trading.edge import cross_venue_rows
from ev_trading.models import Venue, VenueQuote
from ev_trading.pipeline import run_once

app = typer.Typer(
    name="ev-trading",
    help="Cross-venue prediction-market odds (Polymarket, Kalshi, ProphetX, SX Bet).",
    no_args_is_help=True,
    add_completion=False,
)
console = Console()


@app.command()
def fetch(
    config_dir: str = typer.Option("config", help="config directory"),
    query: str | None = typer.Option(None, help="optional text/series filter"),
    out: str | None = typer.Option(None, help="quotes.json path (default: output/ev/quotes.json)"),
) -> None:
    """Pull quotes from each venue. Adapters return [] until API wiring lands."""
    import asyncio

    from ev_trading.pipeline import fetch_quotes, write_snapshot

    cfg = EvConfig.load(config_dir)
    quotes = asyncio.run(fetch_quotes(cfg, query=query))
    out_dir = Path(out).parent if out else Path(cfg.out_dir)
    quotes_path, _ = write_snapshot(quotes, [], out_dir)
    if out:
        Path(out).write_text(quotes_path.read_text(encoding="utf-8"), encoding="utf-8")
        quotes_path = Path(out)
    console.print(f"wrote {len(quotes)} quotes → {quotes_path}")


@app.command()
def compare(
    quotes_path: str = typer.Option("output/ev/quotes.json", "--in", help="quotes snapshot"),
    min_spread: float | None = typer.Option(None, help="minimum mid gap to keep"),
    config_dir: str = typer.Option("config", help="config directory"),
) -> None:
    """Analyze a saved snapshot. Import `ev_trading.edge` from your own scripts too."""
    cfg = EvConfig.load(config_dir)
    raw = json.loads(Path(quotes_path).read_text(encoding="utf-8"))
    quotes = [_quote_from_dict(row) for row in raw]
    rows = cross_venue_rows(quotes, min_spread=min_spread or cfg.min_spread)
    console.print(f"{len(rows)} cross-venue gaps from {len(quotes)} quotes")
    for row in rows[:25]:
        console.print(
            f"  {row.spread:.3f}  {row.cheap_venue.value} {row.cheap_mid:.3f} → "
            f"{row.rich_venue.value} {row.rich_mid:.3f}  {row.question[:80]}"
        )


@app.command("run")
def run_pipeline(
    config_dir: str = typer.Option("config", help="config directory"),
    query: str | None = typer.Option(None, help="optional text/series filter"),
) -> None:
    """Fetch every venue and write output/ev/quotes.json + compare.json."""
    import asyncio

    cfg = EvConfig.load(config_dir)
    stats = asyncio.run(run_once(cfg, query=query))
    console.print(f"quotes={stats['quotes']} matches={stats['matches']} → {cfg.out_dir}")


@app.command("nfl-ev")
def nfl_ev(
    odds: str = typer.Option(
        str(Path(__file__).resolve().parent / "supporting-lines" / "nfl_aggregated_odds.json"),
        help="aggregated NFL odds JSON (books + Kalshi + Polymarket)",
    ),
    config: str = typer.Option("config/fair_value.json", help="fair-value JSON config"),
    out: str = typer.Option("output/ev/nfl_fair_value.json", help="JSON report path"),
    quiet: bool = typer.Option(False, help="skip the console tables"),
    refresh: bool = typer.Option(
        False,
        "--refresh",
        help="re-scrape RotoWire + Kalshi + Polymarket, rebuild the aggregate, then score",
    ),
    sequential: bool = typer.Option(False, help="with --refresh, scrape sources one at a time"),
) -> None:
    """Sportsbook consensus vs Kalshi/Polymarket — tradable EV report only."""
    from ev_trading.fair_value.config import FairValueConfig
    from ev_trading.fair_value.pipeline import run_fair_value
    from ev_trading.fair_value.report import render_report

    if refresh:
        from ev_trading.nfl_refresh import run_nfl_refresh

        result = run_nfl_refresh(
            parallel=not sequential,
            config_path=config,
            report_out=out,
            quiet=quiet,
        )
        console.print(f"done in {result.elapsed_s:.1f}s  aggregated={result.aggregated}")
        return

    cfg = FairValueConfig.load(config)
    report = run_fair_value(odds, cfg=cfg, out_path=out)
    if not quiet:
        render_report(report, console=console)
    console.print(
        f"tradable={len(report.tradable)} low_liq={len(report.tradable_low_liquidity)} "
        f"td={len(report.tradable_td)} td_low_liq={len(report.tradable_td_low_liquidity)} "
        f"info={len(report.informational)} → {out}"
    )


def _quote_from_dict(row: dict[str, Any]) -> VenueQuote:
    return VenueQuote(
        venue=Venue(row["venue"]),
        market_id=str(row["market_id"]),
        event_id=str(row["event_id"]),
        question=str(row["question"]),
        outcome=str(row["outcome"]),
        bid=row.get("bid"),
        ask=row.get("ask"),
        last=row.get("last"),
        volume=row.get("volume"),
        liquidity=row.get("liquidity"),
        url=row.get("url"),
        end_date_iso=row.get("end_date_iso"),
        fetched_at=float(row.get("fetched_at") or 0),
    )


if __name__ == "__main__":
    app()
