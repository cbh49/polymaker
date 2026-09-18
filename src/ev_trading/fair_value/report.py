"""Ranked report: tradable Kalshi/Polymarket first, sportsbooks informational only."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from rich.console import Console
from rich.table import Table

from ev_trading.fair_value.models import InformationalRow, PricedOpportunity


def is_td_yesno(stat: str | None) -> bool:
    """Yes/no TD contracts with no over/under: anytime, first, 2+, etc.

    O/U TD counts (passing_tds, rushing_tds, receiving_tds) stay in the
    main tradable lists.
    """
    key = (stat or "").strip().lower()
    return key.endswith("_td") and not key.endswith("_tds")


@dataclass
class FairValueReport:
    tradable: list[PricedOpportunity] = field(default_factory=list)
    tradable_low_liquidity: list[PricedOpportunity] = field(default_factory=list)
    tradable_td: list[PricedOpportunity] = field(default_factory=list)
    tradable_td_low_liquidity: list[PricedOpportunity] = field(default_factory=list)
    informational: list[InformationalRow] = field(default_factory=list)
    rejected: list[PricedOpportunity] = field(default_factory=list)
    generated_at: str = ""
    source: str | None = None
    n_markets: int = 0

    def to_dict(self) -> dict[str, Any]:
        return {
            "generated_at": self.generated_at,
            "source": self.source,
            "n_markets": self.n_markets,
            "tradable": [r.to_dict() for r in self.tradable],
            "tradable_low_liquidity": [r.to_dict() for r in self.tradable_low_liquidity],
            "tradable_td": [r.to_dict() for r in self.tradable_td],
            "tradable_td_low_liquidity": [r.to_dict() for r in self.tradable_td_low_liquidity],
            "informational": [r.to_dict() for r in self.informational],
            "rejected": [r.to_dict() for r in self.rejected],
        }


def build_report(
    tradable: list[PricedOpportunity],
    informational: list[InformationalRow],
    *,
    min_edge_pct: float,
    source: str | None = None,
    n_markets: int = 0,
    rejected: list[PricedOpportunity] | None = None,
) -> FairValueReport:
    min_edge = min_edge_pct / 100.0
    kept = [r for r in tradable if r.fee_adjusted_edge >= min_edge]
    kept.sort(key=lambda r: r.rank_score, reverse=True)
    props = [r for r in kept if not is_td_yesno(r.stat)]
    tds = [r for r in kept if is_td_yesno(r.stat)]
    liquid = [r for r in props if not r.low_liquidity]
    thin = [r for r in props if r.low_liquidity]
    td_liquid = [r for r in tds if not r.low_liquidity]
    td_thin = [r for r in tds if r.low_liquidity]
    info = [r for r in informational if abs(r.raw_edge) >= min_edge]
    info.sort(key=lambda r: abs(r.raw_edge), reverse=True)
    vetoed = list(rejected or [])
    vetoed.sort(key=lambda r: abs(r.fee_adjusted_edge), reverse=True)
    return FairValueReport(
        tradable=liquid,
        tradable_low_liquidity=thin,
        tradable_td=td_liquid,
        tradable_td_low_liquidity=td_thin,
        informational=info,
        rejected=vetoed,
        generated_at=datetime.now(UTC).isoformat(),
        source=source,
        n_markets=n_markets,
    )


def _pct(value: float) -> str:
    return f"{value * 100:.2f}%"


def _num(value: float | None, digits: int = 2) -> str:
    if value is None:
        return "—"
    return f"{value:.{digits}f}"


def render_report(report: FairValueReport, *, console: Console | None = None) -> None:
    out = console or Console()
    out.print(
        f"[bold]NFL fair-value report[/bold]  {report.generated_at}  "
        f"markets={report.n_markets}"
    )
    _print_tradable(
        out,
        report.tradable,
        title="Tradable opportunities (Kalshi + Polymarket only)",
        style="green",
    )
    _print_tradable(
        out,
        report.tradable_low_liquidity,
        title="Low liquidity — verify fill price manually",
        style="yellow",
    )
    _print_tradable(
        out,
        report.tradable_td,
        title="Touchdowns yes/no (anytime, first, 2+)",
        style="cyan",
    )
    _print_tradable(
        out,
        report.tradable_td_low_liquidity,
        title="Touchdowns yes/no — low liquidity",
        style="yellow",
    )
    _print_informational(out, report.informational)
    if report.rejected:
        out.print(
            f"[dim]rejected={len(report.rejected)} "
            f"(vetoed different-strike rows; see JSON `rejected`)[/dim]"
        )


def _print_tradable(
    console: Console,
    rows: list[PricedOpportunity],
    *,
    title: str,
    style: str,
) -> None:
    table = Table(title=title, title_style=style, show_lines=False)
    table.add_column("Market", overflow="fold", max_width=42)
    table.add_column("Venue")
    table.add_column("Side")
    table.add_column("Fair", justify="right")
    table.add_column("Ask", justify="right")
    table.add_column("Line", justify="right")
    table.add_column("Edge", justify="right")
    table.add_column("EV/$", justify="right")
    table.add_column("Conf", justify="right")
    table.add_column("Liq/Vol", justify="right")
    if not rows:
        table.add_row("— none —", "", "", "", "", "", "", "", "", "")
        console.print(table)
        return
    for row in rows:
        loc = row.liquidity if row.liquidity is not None else row.volume
        vol = row.volume_24hr if row.volume_24hr is not None else row.volume
        liq_s = f"{_num(loc, 0)}/{_num(vol, 0)}"
        flag = " ⚠" if row.low_confidence else ""
        table.add_row(
            f"{row.market}{flag}",
            row.venue,
            row.side,
            _pct(row.fair_prob),
            _pct(row.market_price),
            _num(row.market_line, 1),
            f"{row.edge_pct:+.2f}pp",
            f"{row.expected_value_per_contract:+.3f}",
            f"{row.confidence:.2f}",
            liq_s,
        )
    console.print(table)


def _print_informational(console: Console, rows: list[InformationalRow]) -> None:
    table = Table(
        title="Informational only — sportsbooks are NOT ACTIONABLE",
        title_style="red",
        show_lines=False,
    )
    table.add_column("Market", overflow="fold", max_width=42)
    table.add_column("Book")
    table.add_column("Book p", justify="right")
    table.add_column("Fair p", justify="right")
    table.add_column("Line", justify="right")
    table.add_column("Δ", justify="right")
    table.add_column("Actionable")
    if not rows:
        table.add_row("— none —", "", "", "", "", "", "NO")
        console.print(table)
        return
    shown = rows[:40]
    for row in shown:
        table.add_row(
            row.market,
            row.book,
            _pct(row.book_prob),
            _pct(row.fair_prob),
            _num(row.book_line, 1),
            f"{row.edge_pct:+.2f}pp",
            "NO",
        )
    console.print(table)
    if len(rows) > 40:
        console.print(f"[dim]… {len(rows) - 40} more informational rows in JSON output[/dim]")
