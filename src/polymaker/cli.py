"""polymaker CLI — Polymarket catalog + trading foundation.

  polymaker scan                 discover politics + MLB/WNBA events -> SQLite
  polymaker markets              browse the catalog
  polymaker research             MLB daily Best Bets → sized plays JSON
  polymaker match-sharp          map sharp-money JSON → Polymarket moneylines
  polymaker trade-sharp          buy matched sharp plays (dry-run by default)
  polymaker doctor               preflight: wallet auth, balances, reachability
  polymaker buy <slug>           buy YES/NO (or outcome) on a catalog market
  polymaker status               open orders + positions
  polymaker cancel-all           cancel all open orders
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

import typer
from rich.console import Console
from rich.table import Table

from polymaker import __version__
from polymaker.config import Config

app = typer.Typer(
    name="polymaker",
    help="Polymarket client: scan sporting events and place buys.",
    no_args_is_help=True,
    add_completion=False,
)
console = Console()


@app.command()
def version() -> None:
    """Print the polymaker version."""
    console.print(f"polymaker {__version__}")


@app.command()
def scan(
    config_dir: str = typer.Option("config", help="config directory"),
    min_liquidity: float | None = typer.Option(None, help="minimum market liquidity (USDC)"),
    all_markets: bool = typer.Option(False, "--all", help="include non-rewards politics markets"),
    politics_only: bool = typer.Option(False, "--politics-only", help="skip sports discovery"),
    sports_only: bool = typer.Option(False, "--sports-only", help="skip politics; scan MLB/WNBA only"),
    series: str | None = typer.Option(
        None,
        "--series",
        help="comma-separated sports series slugs (default: mlb,wnba from config)",
    ),
    look_ahead_days: int | None = typer.Option(None, help="keep games with eventDate today..+N days UTC"),
    include_live: bool = typer.Option(
        False,
        "--include-live",
        help="do not exclude sports games that fail the pre-game startTime check",
    ),
) -> None:
    """Sweep Gamma for politics + MLB/WNBA moneylines and persist to SQLite."""
    from polymaker.catalog.scanner import ScanConfig, run_scan
    from polymaker.catalog.sports import SPORTS_SERIES_SLUGS
    from polymaker.catalog.store import CatalogStore

    if politics_only and sports_only:
        console.print("[red]Choose at most one of --politics-only / --sports-only.[/red]")
        raise typer.Exit(1)

    cfg = Config.load(config_dir)
    store = CatalogStore(cfg.paths.db)
    cat = cfg.catalog

    if series is not None:
        series_slugs = tuple(s.strip() for s in series.split(",") if s.strip())
    elif politics_only:
        series_slugs = ()
    else:
        series_slugs = tuple(cat.series_slugs) or SPORTS_SERIES_SLUGS

    scan_cfg = ScanConfig(
        include_politics=not sports_only,
        series_slugs=() if politics_only else series_slugs,
        look_ahead_days=look_ahead_days if look_ahead_days is not None else cat.look_ahead_days,
        skip_live_events=False if include_live else cat.skip_live_events,
        pregame_buffer_minutes=cat.pregame_buffer_minutes,
        min_liquidity=min_liquidity if min_liquidity is not None else cat.min_liquidity,
        rewards_only=not all_markets,
        sports_rewards_only=cat.sports_rewards_only and not all_markets,
        gamma_host=cfg.wallet.gamma_host,
        clob_host=cfg.wallet.clob_host,
    )

    async def _go() -> int:
        metas = await run_scan(store, scan_cfg)
        return len(metas)

    n = asyncio.run(_go())
    csv_path = Path(config_dir).parent / "markets.csv"
    written = store.export_csv(csv_path)
    console.print(
        f"[green]Scanned and stored {n} markets.[/green] "
        f"Wrote [bold]{csv_path}[/bold] ({written} rows)."
    )
    store.close()


@app.command()
def markets(
    config_dir: str = typer.Option("config", help="config directory"),
    limit: int = typer.Option(25, help="rows to show"),
) -> None:
    """Show the top scored markets from the catalog."""
    from polymaker.catalog.store import CatalogStore

    cfg = Config.load(config_dir)
    store = CatalogStore(cfg.paths.db)
    rows = store.top(limit)
    if not rows:
        console.print("[yellow]Catalog empty. Run `polymaker scan` first.[/yellow]")
        raise typer.Exit()

    table = Table(title="Catalog markets by score")
    for col in ("score", "liq", "spread", "vol24h", "question", "slug"):
        table.add_column(col, justify="right" if col not in ("question", "slug") else "left")
    for meta, sc in rows:
        table.add_row(
            f"{sc.score:.2f}",
            f"{meta.liquidity_num:.0f}",
            f"{sc.spread:.3f}",
            f"{meta.volume_24hr:.0f}",
            meta.question[:50],
            meta.slug[:40],
        )
    console.print(table)
    console.print("\nBuy with: [bold]polymaker buy <slug> --outcome yes --usd 10[/bold]")


@app.command(name="export-csv")
def export_csv(
    config_dir: str = typer.Option("config", help="config directory"),
    out: str = typer.Option("markets.csv", help="output CSV path"),
    limit: int = typer.Option(500, help="max rows"),
) -> None:
    """Export the scored market catalog to a CSV."""
    from polymaker.catalog.store import CatalogStore

    cfg = Config.load(config_dir)
    store = CatalogStore(cfg.paths.db)
    n = store.export_csv(out, limit)
    store.close()
    console.print(f"[green]Wrote {n} markets to {out}.[/green]")


@app.command()
def research(
    plays: str = typer.Option(
        "../MLB/static-json/llm_best_plays.json",
        "--plays",
        help="path to llm_best_plays.json",
    ),
    matchups: str = typer.Option(
        "../MLB/json/matchups.json",
        "--matchups",
        help="path to today's MLB matchups.json",
    ),
    out: str = typer.Option(
        "output/sized_plays.json",
        "--out",
        help="output path for sized plays JSON",
    ),
    hours: int = typer.Option(12, "--hours", help="only articles published within last N hours"),
    articles: int = typer.Option(15, "--articles", help="max slate-wide articles to summarize"),
    min_agreement: int = typer.Option(
        2,
        "--min-agreement",
        help="min articles needed to increase/decrease units or add consensus plays",
    ),
    when: str | None = typer.Option(
        None,
        "--when",
        help="slate date YYYY-MM-DD (default: today)",
    ),
) -> None:
    """Research MLB via daily Best Bets articles; size Breton plays + consensus extras."""
    from datetime import datetime

    from dotenv import load_dotenv

    from polymaker.research.pipeline import run_pipeline

    load_dotenv()
    plays_path = Path(plays)
    if not plays_path.is_file():
        console.print(f"[red]Plays file not found: {plays_path}[/red]")
        raise typer.Exit(1)
    matchups_path = Path(matchups)
    if not matchups_path.is_file():
        console.print(f"[red]Matchups file not found: {matchups_path}[/red]")
        raise typer.Exit(1)

    when_dt: datetime | None = None
    if when:
        try:
            when_dt = datetime.strptime(when, "%Y-%m-%d")
        except ValueError as exc:
            console.print("[red]--when must be YYYY-MM-DD[/red]")
            raise typer.Exit(1) from exc

    try:
        result = run_pipeline(
            plays_path,
            out,
            matchups_path=matchups_path,
            hours=hours,
            article_limit=articles,
            min_agreement=min_agreement,
            when=when_dt,
            log=console.print,
        )
    except RuntimeError as exc:
        console.print(f"[red]{exc}[/red]")
        raise typer.Exit(1) from exc

    table = Table(title="Sized Breton plays")
    table.add_column("units", justify="right")
    table.add_column("sup/opp", justify="right")
    table.add_column("pick")
    table.add_column("matchup")
    for p in result.ml_best_plays + result.ou_best_plays:
        table.add_row(
            f"{p.units:g}u",
            f"{p.support_count}/{p.opposite_count}",
            p.pick,
            p.matchup,
        )
    console.print(table)

    if result.additional_plays:
        extra = Table(title="Additional consensus plays")
        extra.add_column("mentions", justify="right")
        extra.add_column("pick")
        extra.add_column("matchup")
        for p in result.additional_plays:
            extra.add_row(str(p.support_count), p.pick, p.matchup)
        console.print(extra)


@app.command(name="match-sharp")
def match_sharp(
    config_dir: str = typer.Option("config", help="config directory"),
    mlb: str | None = typer.Option(None, "--mlb", help="override mlb_sharp_money.json path"),
    wnba: str | None = typer.Option(None, "--wnba", help="override wnba_sharp_money.json path"),
    ufc: str | None = typer.Option(None, "--ufc", help="override ufc_sharp_money.json path"),
    league: str = typer.Option(
        "both",
        "--league",
        help="mlb | wnba | ufc | both (which sharp file(s) / plays to use)",
    ),
    refresh: bool = typer.Option(
        False,
        "--refresh",
        help="run a sports-only catalog scan before matching",
    ),
    tier: str | None = typer.Option(None, "--tier", help="min tier: A or B (default from config)"),
) -> None:
    """Match sharp-money plays to Polymarket MLB/WNBA/UFC moneylines (no orders)."""
    from polymaker.trading.execute import filter_plays, load_configured_plays
    from polymaker.trading.match import match_sharp_plays

    league_key = _normalize_league(league)
    cfg = Config.load(config_dir)
    paths = _sharp_path_overrides(mlb, wnba, ufc, league_key)

    try:
        plays = load_configured_plays(cfg, paths, league=league_key)
    except FileNotFoundError as exc:
        console.print(f"[red]{exc}[/red]")
        raise typer.Exit(1) from exc

    trade_cfg = _sharp_trade_cfg(cfg, tier=tier, dry_run=True)
    plays = filter_plays(plays, trade_cfg, league=league_key)
    if not plays:
        console.print(
            f"[yellow]No sharp plays after filters[/yellow] "
            f"(league={league_key}; check data-aggregation output)."
        )
        raise typer.Exit()

    if refresh:
        _refresh_sports_catalog(cfg)

    from polymaker.catalog.store import CatalogStore

    store = CatalogStore(cfg.paths.db)

    async def _go() -> list[Any]:
        return await match_sharp_plays(
            plays,
            store=store,
            markets=trade_cfg.markets,
        )

    matched = asyncio.run(_go())
    store.close()
    _print_match_table(matched)


@app.command(name="trade-sharp")
def trade_sharp(
    config_dir: str = typer.Option("config", help="config directory"),
    mlb: str | None = typer.Option(None, "--mlb", help="override mlb_sharp_money.json path"),
    wnba: str | None = typer.Option(None, "--wnba", help="override wnba_sharp_money.json path"),
    ufc: str | None = typer.Option(None, "--ufc", help="override ufc_sharp_money.json path"),
    league: str = typer.Option(
        "both",
        "--league",
        help="mlb | wnba | ufc | both (which sharp file(s) / plays to use)",
    ),
    refresh: bool = typer.Option(
        True,
        "--refresh/--no-refresh",
        help="sports catalog scan before matching (default: on)",
    ),
    tier: str | None = typer.Option(None, "--tier", help="min tier: A or B (default from config)"),
    usd_a: float | None = typer.Option(None, "--usd-a", help="USDC for Tier A"),
    usd_b: float | None = typer.Option(None, "--usd-b", help="USDC for Tier B"),
    live: bool = typer.Option(
        False,
        "--live",
        help="actually send market buys (default is dry-run)",
    ),
) -> None:
    """Buy Polymarket moneylines for sharp-money plays (dry-run unless --live)."""
    from polymaker.trading.execute import (
        filter_plays,
        load_configured_plays,
        run_sharp_trades,
    )
    from polymaker.trading.match import match_sharp_plays

    league_key = _normalize_league(league)
    cfg = Config.load(config_dir)
    paths = _sharp_path_overrides(mlb, wnba, ufc, league_key)

    try:
        plays = load_configured_plays(cfg, paths, league=league_key)
    except FileNotFoundError as exc:
        console.print(f"[red]{exc}[/red]")
        raise typer.Exit(1) from exc

    trade_cfg = _sharp_trade_cfg(
        cfg,
        tier=tier,
        usd_a=usd_a,
        usd_b=usd_b,
        dry_run=not live,
    )
    plays = filter_plays(plays, trade_cfg, league=league_key)
    if not plays:
        console.print(
            f"[yellow]No sharp plays after filters[/yellow] "
            f"(league={league_key}; check data-aggregation output)."
        )
        raise typer.Exit()

    if refresh:
        _refresh_sports_catalog(cfg)

    from polymaker.catalog.store import CatalogStore

    store = CatalogStore(cfg.paths.db)

    async def _go() -> tuple[list[Any], list[Any]]:
        matched = await match_sharp_plays(plays, store=store, markets=trade_cfg.markets)
        results = await run_sharp_trades(matched, cfg, trade_cfg)
        return matched, results

    matched, results = asyncio.run(_go())
    store.close()

    _print_match_table(matched)
    table = Table(title="Sharp trades" + (" (dry-run)" if trade_cfg.dry_run else " (LIVE)"))
    table.add_column("action")
    table.add_column("usd", justify="right")
    table.add_column("tier")
    table.add_column("matchup")
    table.add_column("outcome")
    table.add_column("slug")
    table.add_column("detail")
    for r in results:
        m = r.matched
        table.add_row(
            r.action,
            f"{r.usd:.2f}" if r.usd else "",
            m.play.tier,
            m.play.matchup,
            m.token.outcome if m.token else m.play.side,
            (m.slug or "")[:36],
            (r.detail or m.detail)[:48],
        )
    console.print(table)
    if trade_cfg.dry_run:
        console.print("[dim]Re-run with --live to send market buys.[/dim]")


def _normalize_league(league: str) -> str:
    key = league.strip().lower()
    if key not in {"mlb", "wnba", "ufc", "both"}:
        console.print("[red]--league must be mlb, wnba, ufc, or both[/red]")
        raise typer.Exit(1)
    return key


def _sharp_path_overrides(
    mlb: str | None,
    wnba: str | None,
    ufc: str | None,
    league: str,
) -> list[str] | None:
    """Explicit --mlb/--wnba/--ufc paths win; otherwise None → config defaults by league."""
    if mlb is None and wnba is None and ufc is None:
        return None
    paths: list[str] = []
    if league in {"mlb", "both"} and mlb:
        paths.append(mlb)
    if league in {"wnba", "both"} and wnba:
        paths.append(wnba)
    if league in {"ufc", "both"} and ufc:
        paths.append(ufc)
    # If user passed only one override while league=both, still use it.
    if not paths:
        if mlb:
            paths.append(mlb)
        if wnba:
            paths.append(wnba)
        if ufc:
            paths.append(ufc)
    return paths or None


def _sharp_trade_cfg(
    cfg: Config,
    *,
    tier: str | None = None,
    usd_a: float | None = None,
    usd_b: float | None = None,
    dry_run: bool = True,
) -> Any:
    from polymaker.trading.execute import SharpTradeConfig

    s = cfg.sharp
    return SharpTradeConfig(
        usd_tier_a=usd_a if usd_a is not None else s.usd_tier_a,
        usd_tier_b=usd_b if usd_b is not None else s.usd_tier_b,
        min_tier=(tier or s.min_tier).upper(),
        markets=frozenset(m.lower() for m in s.markets),
        require_rlm=s.require_rlm,
        max_ask=s.max_ask,
        min_edge=s.min_edge,
        filled_log=s.filled_log,
        dry_run=dry_run,
        pregame_buffer_minutes=cfg.catalog.pregame_buffer_minutes,
    )


def _refresh_sports_catalog(cfg: Config) -> None:
    from polymaker.catalog.scanner import ScanConfig, run_scan
    from polymaker.catalog.sports import SPORTS_SERIES_SLUGS
    from polymaker.catalog.store import CatalogStore

    store = CatalogStore(cfg.paths.db)
    cat = cfg.catalog
    scan_cfg = ScanConfig(
        include_politics=False,
        series_slugs=tuple(cat.series_slugs) or SPORTS_SERIES_SLUGS,
            look_ahead_days=cat.look_ahead_days,
            skip_live_events=cat.skip_live_events,
            pregame_buffer_minutes=cat.pregame_buffer_minutes,
            min_liquidity=cat.min_liquidity,
        rewards_only=False,
        sports_rewards_only=cat.sports_rewards_only,
        gamma_host=cfg.wallet.gamma_host,
        clob_host=cfg.wallet.clob_host,
    )
    n = asyncio.run(run_scan(store, scan_cfg))
    store.close()
    console.print(f"[green]Refreshed sports catalog:[/green] {len(n)} markets")


def _print_match_table(matched: list[Any]) -> None:
    table = Table(title="Sharp → Polymarket matches")
    table.add_column("status")
    table.add_column("tier")
    table.add_column("league")
    table.add_column("matchup")
    table.add_column("side")
    table.add_column("slug")
    table.add_column("outcome")
    table.add_column("detail")
    for m in matched:
        table.add_row(
            m.status,
            m.play.tier,
            m.play.league,
            m.play.matchup,
            m.play.side,
            (m.slug or "")[:36],
            m.token.outcome if m.token else "",
            (m.detail or "")[:40],
        )
    console.print(table)


@app.command()
def doctor(config_dir: str = typer.Option("config", help="config directory")) -> None:
    """Preflight checks: config, wallet auth, balance/allowance, WS reachability."""
    from polymaker.doctor import run_doctor

    cfg = Config.load(config_dir)
    ok = asyncio.run(run_doctor(cfg, console))
    raise typer.Exit(0 if ok else 1)


@app.command()
def buy(
    slug: str = typer.Argument(..., help="market slug from the catalog"),
    outcome: str = typer.Option("yes", "--outcome", "-o", help="yes / no / outcome label"),
    usd: float | None = typer.Option(None, "--usd", help="USDC notional to spend (market buy)"),
    config_dir: str = typer.Option("config", help="config directory"),
    limit_price: float | None = typer.Option(
        None,
        "--limit",
        help="optional limit price (0-1). If set, posts a GTC BUY instead of a market order",
    ),
    size: float | None = typer.Option(
        None,
        "--size",
        help="share size for limit buys (required with --limit)",
    ),
    dry_run: bool = typer.Option(False, "--dry-run", help="show what would be bought, do not send"),
) -> None:
    """Buy an outcome on a catalog market (market or limit)."""
    from polymaker.catalog.store import CatalogStore
    from polymaker.domain import Quote, Side, round_to_tick
    from polymaker.execution.gateway import ExecutionGateway

    if limit_price is None and (usd is None or usd <= 0):
        console.print("[red]Provide --usd for a market buy, or --limit + --size for a limit buy.[/red]")
        raise typer.Exit(1)
    if limit_price is not None and (size is None or size <= 0):
        console.print("[red]--size is required with --limit[/red]")
        raise typer.Exit(1)

    cfg = Config.load(config_dir)
    store = CatalogStore(cfg.paths.db)
    meta = store.get_by_slug(slug)
    store.close()
    if meta is None:
        console.print(f"[red]No market with slug {slug!r}. Run `polymaker scan` first.[/red]")
        raise typer.Exit(1)

    from polymaker.catalog.sports import is_moneyline_slug, is_pre_game

    if is_moneyline_slug(meta.slug) and not is_pre_game(
        {"startTime": meta.start_time_iso},
        cfg.catalog.pregame_buffer_minutes,
    ):
        console.print(
            "[red]Not pre-game (startTime more than "
            f"{cfg.catalog.pregame_buffer_minutes:g} minutes in the future). Refusing to trade.[/red]"
        )
        raise typer.Exit(1)

    try:
        token = meta.token_for_outcome(outcome)
    except ValueError as exc:
        console.print(f"[red]{exc}[/red]")
        raise typer.Exit(1) from exc

    console.print(f"[bold]{meta.question}[/bold]")
    console.print(f"  outcome: {token.outcome}  token: {token.token_id[:18]}…")

    async def _go() -> dict[str, Any]:
        gw = ExecutionGateway(cfg)
        await gw.connect()
        book = await gw.get_book(token.token_id)
        if book:
            console.print(
                f"  book: bid {book.get('best_bid')} / ask {book.get('best_ask')} "
                f"(ask depth {book.get('ask_depth', 0):.0f})"
            )

        if dry_run:
            return {"dry_run": True, "book": book}

        if limit_price is not None:
            assert size is not None
            px = round_to_tick(limit_price, meta.tick_size, meta.price_decimals, up=False)
            placed = await gw.place(
                [Quote(token.token_id, Side.BUY, px, float(size))],
                meta,
            )
            return {"mode": "limit", "orders": [o.order_id for o in placed], "price": px, "size": size}

        assert usd is not None
        resp = await gw.market_order(token.token_id, Side.BUY, float(usd), meta, fak=True)
        return {"mode": "market", "resp": resp, "usd": usd}

    result = asyncio.run(_go())
    if result.get("dry_run"):
        if limit_price is not None:
            console.print(
                f"[yellow]dry-run[/yellow] would limit-buy {size:g} of {token.outcome} @ {limit_price}"
            )
        else:
            console.print(f"[yellow]dry-run[/yellow] would market-buy ${usd:.2f} of {token.outcome}")
        return
    if result.get("mode") == "limit":
        ids = result.get("orders") or []
        if ids:
            console.print(
                f"[green]Limit BUY posted[/green] {result['size']:g} @ {result['price']} "
                f"→ {ids[0]}"
            )
        else:
            console.print("[red]Limit place failed — see logs.[/red]")
            raise typer.Exit(1)
        return

    resp = result.get("resp") or {}
    status = str(resp.get("status", resp.get("error", resp))).lower()
    console.print(f"[green]Market BUY sent[/green] ${usd:.2f}  status={status}")
    console.print(f"[dim]{resp}[/dim]")


@app.command()
def status(config_dir: str = typer.Option("config", help="config directory")) -> None:
    """Show open orders and positions from the exchange / data API."""
    from polymaker.execution.gateway import ExecutionGateway

    cfg = Config.load(config_dir)
    if not cfg.secrets.has_wallet:
        console.print("[red]No wallet in .env.[/red]")
        raise typer.Exit(1)

    async def _go() -> tuple[list[Any], dict[str, tuple[float, float]], float]:
        gw = ExecutionGateway(cfg)
        await gw.connect()
        orders = await gw.open_orders()
        positions = await gw.positions()
        bal = await gw.collateral_balance()
        return orders, positions, bal

    orders, positions, bal = asyncio.run(_go())
    console.print(f"[bold]Collateral:[/bold] {bal:.4f} pUSD")
    console.print(f"[bold]Open orders:[/bold] {len(orders)}")
    if orders:
        table = Table(title="Open orders")
        table.add_column("id")
        table.add_column("side")
        table.add_column("price", justify="right")
        table.add_column("size", justify="right")
        table.add_column("token")
        for o in orders:
            table.add_row(o.order_id[:16] + "…", o.side.value, f"{o.price:.3f}",
                          f"{o.size:.2f}", o.token_id[:14] + "…")
        console.print(table)

    if not positions:
        console.print("[dim]No open positions.[/dim]")
    else:
        table = Table(title="Positions")
        table.add_column("token")
        table.add_column("size", justify="right")
        table.add_column("avg", justify="right")
        for tok, (sz, avg) in positions.items():
            table.add_row(tok[:16] + "…", f"{sz:.2f}", f"{avg:.3f}")
        console.print(table)


@app.command(name="cancel-all")
def cancel_all(config_dir: str = typer.Option("config", help="config directory")) -> None:
    """Cancel all open orders for the wallet."""
    from polymaker.execution.gateway import ExecutionGateway

    cfg = Config.load(config_dir)
    gw = ExecutionGateway(cfg)

    async def _go() -> None:
        await gw.connect()
        await gw.cancel_all()

    asyncio.run(_go())
    console.print("[green]Sent cancel-all.[/green]")


if __name__ == "__main__":
    app()
