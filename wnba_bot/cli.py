"""wnba-bot CLI — WNBA article research → findings + consensus best bets JSON.

  # Existing per-game flow (default)
  wnba-bot
  wnba-bot --mode games

  # New slate-wide flow: search "WNBA {DATE} Best Bets", process ~15 articles
  wnba-bot --mode daily
"""

# ruff: noqa: B008

from __future__ import annotations

from datetime import datetime
from enum import Enum
from pathlib import Path

import typer
from dotenv import load_dotenv
from rich.console import Console

console = Console()

_TRADING_BOT_ROOT = Path(__file__).resolve().parents[1]
_DEFAULT_MATCHUPS = _TRADING_BOT_ROOT.parent / "WNBA" / "json-data" / "wnba_matchups.json"
_DEFAULT_FINDINGS = Path("output/wnba_findings.json")
_DEFAULT_BEST_BETS = Path("output/wnba_best_bets.json")
_DEFAULT_DAILY_FINDINGS = Path("output/wnba_daily_findings.json")
_DEFAULT_DAILY_BEST_BETS = Path("output/wnba_daily_best_bets.json")


class Mode(str, Enum):
    games = "games"
    daily = "daily"


def main(
    mode: Mode = typer.Option(
        Mode.games,
        "--mode",
        help="games = per-matchup search (default); daily = slate-wide 'WNBA {DATE} Best Bets'",
    ),
    matchups: Path = typer.Option(
        _DEFAULT_MATCHUPS,
        "--matchups",
        help="Path to wnba_matchups.json",
        show_default=True,
    ),
    findings: Path | None = typer.Option(
        None,
        "--findings",
        help="Output path for article extractions (mode-specific default if omitted)",
    ),
    best_bets: Path | None = typer.Option(
        None,
        "--best-bets",
        help="Output path for consensus best bets (mode-specific default if omitted)",
    ),
    days: int = typer.Option(2, "--days", help="[games] Only use articles from the last N days"),
    hours: int = typer.Option(
        12,
        "--hours",
        help="[daily] Only use articles published in the last N hours",
    ),
    articles: int = typer.Option(
        15,
        "--articles",
        help="[daily] Max articles to fetch and process",
    ),
    min_agreement: int = typer.Option(
        2,
        "--min-agreement",
        help="Minimum distinct articles mentioning a bet to export it",
    ),
    when: str | None = typer.Option(
        None,
        "--when",
        help="Override date for search query (YYYY-MM-DD); default today",
    ),
) -> None:
    """WNBA best-bets research: articles → consensus picks."""
    load_dotenv()

    when_dt: datetime | None = None
    if when:
        when_dt = datetime.strptime(when, "%Y-%m-%d")

    matchups_path = matchups.resolve()
    if not matchups_path.is_file():
        console.print(f"[red]Matchups file not found: {matchups_path}[/red]")
        raise typer.Exit(1)

    if mode == Mode.daily:
        from wnba_bot.daily_pipeline import run_daily_pipeline

        findings_path = findings or _DEFAULT_DAILY_FINDINGS
        best_bets_path = best_bets or _DEFAULT_DAILY_BEST_BETS
        console.print(f"[bold]WNBA research (daily slate)[/bold] ← {matchups_path}")
        run_daily_pipeline(
            matchups_path,
            findings_path,
            best_bets_path,
            hours=hours,
            article_limit=articles,
            when=when_dt,
            min_agreement=min_agreement,
            log=lambda msg: console.print(msg),
        )
        return

    from wnba_bot.pipeline import run_pipeline

    findings_path = findings or _DEFAULT_FINDINGS
    best_bets_path = best_bets or _DEFAULT_BEST_BETS
    console.print(f"[bold]WNBA research (per-game)[/bold] ← {matchups_path}")
    run_pipeline(
        matchups_path,
        findings_path,
        best_bets_path,
        days=days,
        when=when_dt,
        min_agreement=min_agreement,
        log=lambda msg: console.print(msg),
    )


def run() -> None:
    """Console script entrypoint."""
    typer.run(main)


if __name__ == "__main__":
    run()
