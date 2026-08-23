"""Daily slate-wide WNBA research: one search → 15 articles → consensus across all games."""

from __future__ import annotations

import json
import re
from collections.abc import Callable
from datetime import datetime
from pathlib import Path

from wnba_bot.consensus import MIN_AGREEMENT, build_consensus
from wnba_bot.daily_summarize import summarize_daily_articles
from wnba_bot.load_matchups import load_matchups
from wnba_bot.schemas import (
    ArticleFindings,
    BestBetsFile,
    DailyArticleFindings,
    DailyFindingsFile,
    ExtractedBet,
    GameFindings,
    Matchup,
    TaggedBet,
)
from wnba_bot.search import (
    DAILY_HOURS,
    MAX_DAILY_ARTICLE_CHARS,
    NUM_DAILY_ARTICLES,
    build_daily_search_query,
    fetch_all,
    search_daily_articles,
)
from wnba_bot.summarize import make_client

LogFn = Callable[[str], None]

_WS_RE = re.compile(r"\s+")


def _default_log(msg: str) -> None:
    print(msg)


def _norm(s: str) -> str:
    s = s.lower().strip()
    s = s.replace("½", ".5").replace("−", "-")
    return _WS_RE.sub(" ", s)


def _aliases(name: str) -> set[str]:
    n = _norm(name)
    parts = n.split()
    aliases = {n}
    if parts:
        aliases.add(parts[-1])
    if n.startswith("the "):
        aliases.add(n[4:])
    return {a for a in aliases if a}


def _resolve_matchup(bet: TaggedBet, matchups: list[Matchup]) -> Matchup | None:
    """Map a tagged bet onto today's slate via away/home (or selection text)."""
    away_n = _norm(bet.away)
    home_n = _norm(bet.home)
    if away_n and home_n:
        for m in matchups:
            if _norm(m.away) == away_n and _norm(m.home) == home_n:
                return m
            # allow swapped away/home labels from the LLM
            if _norm(m.away) == home_n and _norm(m.home) == away_n:
                return m

    blob = _norm(f"{bet.away} {bet.home} {bet.selection} {bet.raw}")
    if not blob:
        return None

    best: Matchup | None = None
    best_hits = 0
    for m in matchups:
        away_hit = any(a and a in blob for a in _aliases(m.away))
        home_hit = any(a and a in blob for a in _aliases(m.home))
        hits = int(away_hit) + int(home_hit)
        if hits > best_hits:
            best_hits = hits
            best = m
    return best if best_hits >= 1 else None


def _to_extracted(bet: TaggedBet) -> ExtractedBet:
    return ExtractedBet(
        bet_type=bet.bet_type,
        selection=bet.selection,
        side=bet.side,
        line=bet.line,
        raw=bet.raw,
    )


def articles_to_game_findings(
    articles: list[DailyArticleFindings],
    matchups: list[Matchup],
    *,
    query: str,
) -> list[GameFindings]:
    """Pivot slate-wide article bets into per-game ArticleFindings for consensus."""
    # url -> (article meta, bets per matchup key)
    per_game: dict[tuple[str, str], list[ArticleFindings]] = {
        (m.away, m.home): [] for m in matchups
    }

    for art in articles:
        # Group this article's bets by resolved matchup
        by_matchup: dict[tuple[str, str], list[ExtractedBet]] = {}
        for bet in art.best_bets:
            matchup = _resolve_matchup(bet, matchups)
            if matchup is None:
                continue
            key = (matchup.away, matchup.home)
            by_matchup.setdefault(key, []).append(_to_extracted(bet))

        for key, bets in by_matchup.items():
            per_game.setdefault(key, []).append(
                ArticleFindings(
                    title=art.title,
                    url=art.url,
                    game_relevant=True,
                    best_bets=bets,
                )
            )

    games: list[GameFindings] = []
    for m in matchups:
        games.append(
            GameFindings(
                away=m.away,
                home=m.home,
                game_time=m.game_time,
                lines=m.lines,
                query=query,
                articles=per_game.get((m.away, m.home), []),
            )
        )
    return games


def run_daily_pipeline(
    matchups_path: str | Path,
    findings_path: str | Path,
    best_bets_path: str | Path,
    *,
    hours: int = DAILY_HOURS,
    article_limit: int = NUM_DAILY_ARTICLES,
    api_key: str | None = None,
    when: datetime | None = None,
    min_agreement: int = MIN_AGREEMENT,
    log: LogFn = _default_log,
) -> tuple[DailyFindingsFile, BestBetsFile]:
    """Search slate-wide best-bet articles, extract picks, write consensus JSON."""
    matchups_path = Path(matchups_path)
    findings_path = Path(findings_path)
    best_bets_path = Path(best_bets_path)

    matchups = load_matchups(matchups_path)
    client = make_client(api_key)
    generated_at = datetime.now().isoformat(timespec="seconds")
    query = build_daily_search_query(when=when)

    log("\n=== Daily WNBA slate search ===")
    log(f"  query: {query}")
    log(f"  window: last {hours}h | target articles: {article_limit}")
    log(f"  matchups: {len(matchups)}")

    articles = search_daily_articles(query, hours=hours, limit=article_limit)
    if not articles:
        log("  no articles found")
    else:
        log(f"  found {len(articles)} articles; fetching...")
        fetch_all(articles, max_chars=MAX_DAILY_ARTICLE_CHARS)

    article_findings = summarize_daily_articles(
        client, query, matchups, articles, log=log
    )
    total_bets = sum(len(a.best_bets) for a in article_findings)
    log(f"  extracted {total_bets} bets across {len(article_findings)} articles")

    findings = DailyFindingsFile(
        generated_at=generated_at,
        mode="daily",
        query=query,
        hours=hours,
        article_limit=article_limit,
        source_matchups=str(matchups_path.resolve()),
        articles=article_findings,
    )
    findings_path.parent.mkdir(parents=True, exist_ok=True)
    findings_path.write_text(
        json.dumps(findings.model_dump(), indent=2) + "\n",
        encoding="utf-8",
    )
    log(f"\nWrote {findings_path} ({len(article_findings)} articles)")

    games = articles_to_game_findings(article_findings, matchups, query=query)
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
    consensus_count = sum(len(g.best_bets) for g in consensus_games)
    log(
        f"Wrote {best_bets_path} "
        f"({consensus_count} consensus bets, min_agreement={min_agreement})"
    )
    return findings, best
