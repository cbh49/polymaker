"""End-to-end MLB research pipeline: daily slate search → summarize → size → JSON."""

from __future__ import annotations

import json
from collections.abc import Callable
from datetime import datetime
from pathlib import Path

from polymaker.research.consensus import articles_for_play, build_additional_plays
from polymaker.research.daily_summarize import summarize_daily_articles
from polymaker.research.load_matchups import load_matchups
from polymaker.research.load_plays import iter_plays, load_best_plays
from polymaker.research.schemas import (
    ArticleBets,
    BestPlay,
    DailyArticleFindings,
    Matchup,
    SizedPlay,
    SizedPlaysFile,
)
from polymaker.research.search import (
    DAILY_HOURS,
    MAX_DAILY_ARTICLE_CHARS,
    NUM_DAILY_ARTICLES,
    build_daily_search_query,
    fetch_all,
    search_daily_articles,
)
from polymaker.research.sizer import MIN_SIDE_AGREEMENT, size_play
from polymaker.research.summarize import make_client

LogFn = Callable[[str], None]


def _default_log(msg: str) -> None:
    print(msg)


def _size_breton_play(
    play: BestPlay,
    articles: list[DailyArticleFindings],
    matchups: list[Matchup],
    *,
    query: str,
    min_agreement: int,
) -> SizedPlay:
    """Size one Breton play against slate-wide article findings for its matchup."""
    paired = articles_for_play(play, articles, matchups)
    article_bets = [
        ArticleBets(
            title=art.title,
            url=art.url,
            best_bets=bets,
            game_relevant=True,
        )
        for art, bets in paired
    ]
    return size_play(
        play,
        article_bets,
        query=query,
        min_agreement=min_agreement,
        origin="breton",
    )


def run_pipeline(
    plays_path: str | Path,
    out_path: str | Path,
    *,
    matchups_path: str | Path | None = None,
    hours: int = DAILY_HOURS,
    article_limit: int = NUM_DAILY_ARTICLES,
    min_agreement: int = MIN_SIDE_AGREEMENT,
    api_key: str | None = None,
    when: datetime | None = None,
    log: LogFn = _default_log,
) -> SizedPlaysFile:
    """Search MLB Best Bets articles once, size Breton plays, add consensus extras."""
    plays_path = Path(plays_path)
    out_path = Path(out_path)
    if matchups_path is None:
        matchups_path = plays_path.resolve().parents[1] / "json" / "matchups.json"
    matchups_path = Path(matchups_path)

    best = load_best_plays(plays_path)
    matchups = load_matchups(matchups_path)
    client = make_client(api_key)
    query = build_daily_search_query(when=when)
    generated_at = datetime.now().isoformat(timespec="seconds")

    log("\n=== Daily MLB slate search ===")
    log(f"  query: {query}")
    log(f"  window: last {hours}h | target articles: {article_limit}")
    log(f"  matchups: {len(matchups)}")
    log(f"  breton plays: {len(iter_plays(best))}")

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

    ml_out: list[SizedPlay] = []
    ou_out: list[SizedPlay] = []
    for play in iter_plays(best):
        sized = _size_breton_play(
            play,
            article_findings,
            matchups,
            query=query,
            min_agreement=min_agreement,
        )
        log(
            f"  {play.pick}: support {sized.support_count} / "
            f"opposite {sized.opposite_count} → {sized.units}u"
        )
        if play.category == "ou":
            ou_out.append(sized)
        else:
            ml_out.append(sized)

    additional = build_additional_plays(
        article_findings,
        matchups,
        iter_plays(best),
        query=query,
        min_agreement=min_agreement,
    )
    if additional:
        log(f"  additional consensus plays: {len(additional)}")
        for sized_extra in additional:
            log(f"    + {sized_extra.pick} ({sized_extra.matchup}) ×{sized_extra.support_count}")

    result = SizedPlaysFile(
        generated_at=generated_at,
        source_plays=str(plays_path.resolve()),
        source_matchups=str(matchups_path.resolve()),
        query=query,
        hours=hours,
        article_limit=article_limit,
        min_agreement=min_agreement,
        ml_best_plays=ml_out,
        ou_best_plays=ou_out,
        additional_plays=additional,
    )

    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(
        json.dumps(result.model_dump(), indent=2) + "\n",
        encoding="utf-8",
    )
    log(
        f"\nWrote {out_path} "
        f"({len(ml_out)} ml + {len(ou_out)} ou + {len(additional)} additional)"
    )
    return result
