"""End-to-end WNBA research pipeline: search → summarize → consensus → JSON."""

from __future__ import annotations

import json
from collections.abc import Callable
from datetime import datetime
from pathlib import Path

import anthropic

from wnba_bot.consensus import MIN_AGREEMENT, build_consensus
from wnba_bot.load_matchups import load_matchups
from wnba_bot.schemas import (
    BestBetsFile,
    FindingsFile,
    GameFindings,
    Matchup,
)
from wnba_bot.search import build_search_query, fetch_all, search_web
from wnba_bot.summarize import make_client, summarize_articles

LogFn = Callable[[str], None]


def _default_log(msg: str) -> None:
    print(msg)


def research_game(
    matchup: Matchup,
    client: anthropic.Anthropic,
    *,
    days: int = 2,
    when: datetime | None = None,
    log: LogFn = _default_log,
) -> GameFindings:
    """Run search + summarize for a single matchup."""
    query = build_search_query(matchup, when=when)
    log(f"\n=== {matchup.away} @ {matchup.home} ===")
    log(f"  query: {query}")

    articles = search_web(query, days=days)
    if not articles:
        log("  no articles found; continuing with empty findings")
        return GameFindings(
            away=matchup.away,
            home=matchup.home,
            game_time=matchup.game_time,
            lines=matchup.lines,
            query=query,
            articles=[],
        )

    fetch_all(articles)
    log(f"  fetched {len(articles)} articles; summarizing...")
    try:
        article_findings = summarize_articles(client, query, matchup, articles)
    except Exception as exc:  # noqa: BLE001 — keep pipeline moving on LLM/API failures
        log(f"  summarize failed ({exc}); continuing with empty article bets")
        article_findings = []

    bet_count = sum(len(a.best_bets) for a in article_findings)
    log(f"  extracted {bet_count} bets across {len(article_findings)} articles")
    return GameFindings(
        away=matchup.away,
        home=matchup.home,
        game_time=matchup.game_time,
        lines=matchup.lines,
        query=query,
        articles=article_findings,
    )


def run_pipeline(
    matchups_path: str | Path,
    findings_path: str | Path,
    best_bets_path: str | Path,
    *,
    days: int = 2,
    api_key: str | None = None,
    when: datetime | None = None,
    min_agreement: int = MIN_AGREEMENT,
    log: LogFn = _default_log,
) -> tuple[FindingsFile, BestBetsFile]:
    """Load matchups, research each game, write findings + consensus best bets."""
    matchups_path = Path(matchups_path)
    findings_path = Path(findings_path)
    best_bets_path = Path(best_bets_path)

    matchups = load_matchups(matchups_path)
    client = make_client(api_key)
    generated_at = datetime.now().isoformat(timespec="seconds")

    games: list[GameFindings] = []
    for matchup in matchups:
        games.append(research_game(matchup, client, days=days, when=when, log=log))

    findings = FindingsFile(
        generated_at=generated_at,
        source_matchups=str(matchups_path.resolve()),
        games=games,
    )
    findings_path.parent.mkdir(parents=True, exist_ok=True)
    findings_path.write_text(
        json.dumps(findings.model_dump(), indent=2) + "\n",
        encoding="utf-8",
    )
    log(f"\nWrote {findings_path} ({len(games)} games)")

    consensus_games = build_consensus(games, matchups, min_agreement=min_agreement)
    best = BestBetsFile(
        generated_at=generated_at,
        min_agreement=min_agreement,
        games=consensus_games,
    )
    best_bets_path.parent.mkdir(parents=True, exist_ok=True)
    best_bets_path.write_text(
        json.dumps(best.model_dump(), indent=2) + "\n",
        encoding="utf-8",
    )
    total_bets = sum(len(g.best_bets) for g in consensus_games)
    log(f"Wrote {best_bets_path} ({total_bets} consensus bets, min_agreement={min_agreement})")
    return findings, best
