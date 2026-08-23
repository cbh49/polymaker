"""Extract MLB best bets from slate-wide "best bets" articles (one article at a time)."""

from __future__ import annotations

import json
from collections.abc import Callable
from typing import Any

import anthropic

from polymaker.research.schemas import ArticleRef, DailyArticleFindings, Matchup, TaggedBet
from polymaker.research.summarize import SUMMARIZE_SYSTEM, _model, _response_text, _strip_fences

DAILY_MAX_TOKENS = 2000
LogFn = Callable[[str], None]


def _slate_block(matchups: list[Matchup]) -> str:
    lines: list[str] = ["Today's MLB slate (use these exact team names when tagging bets):"]
    for i, m in enumerate(matchups, start=1):
        lines.append(
            f"{i}. {m.away} @ {m.home} | "
            f"ML {m.away} {m.lines.ml_away} / {m.home} {m.lines.ml_home} | "
            f"RL {m.away} {m.lines.run_line_away} / {m.home} {m.lines.run_line_home} | "
            f"Total {m.lines.total}"
        )
    return "\n".join(lines)


def _build_daily_prompt(query: str, matchups: list[Matchup], art: ArticleRef) -> str:
    text = art.content or art.snippet
    return f"""Extract ALL recommended MLB best bets from this article about: "{query}"

{_slate_block(matchups)}

Rules:
- List only concrete recommended plays (moneyline, run line, over/under game totals).
- Do NOT invent picks. Do NOT include player props or pitcher props.
- Ignore odds tables / morning snapshots that are informational only.
- Prefer labeled Best Bet / Free Pick / Play sections when present.
- For each bet, set away/home to the matchup teams from the slate above (exact names).
- If a pick is for a game not on today's slate, skip it.
- bet_type must be MONEYLINE, RUN_LINE, or TOTAL.
- "Team -150" / "Team +120" (American odds, 3+ digits) is MONEYLINE, not a run line.
- "Team -1.5" / "Team +1.5" is RUN_LINE.
- For totals, set side to OVER or UNDER and line when known.
- For run line, put the spread in line when known (e.g. -1.5) and side HOME or AWAY when clear.
- Keep selection concise. Return valid JSON only.

=== ARTICLE: {art.title} ===
{text}

Return a JSON object with this exact structure:
{{
  "best_bets": [
    {{
      "away": "Away Team Name",
      "home": "Home Team Name",
      "bet_type": "MONEYLINE | RUN_LINE | TOTAL",
      "selection": "short pick label",
      "side": "HOME | AWAY | OVER | UNDER | null",
      "line": null,
      "raw": "phrase from the article"
    }}
  ]
}}"""


def _parse_tagged_bets(raw: str) -> list[TaggedBet]:
    try:
        data: dict[str, Any] = json.loads(_strip_fences(raw))
    except json.JSONDecodeError:
        return []

    bets: list[TaggedBet] = []
    for b in data.get("best_bets") or []:
        if not isinstance(b, dict):
            continue
        line_val = b.get("line")
        try:
            line = float(line_val) if line_val is not None else None
        except (TypeError, ValueError):
            line = None
        side_raw = b.get("side")
        side = None if side_raw in (None, "null", "") else str(side_raw)
        bets.append(
            TaggedBet(
                away=str(b.get("away") or "").strip(),
                home=str(b.get("home") or "").strip(),
                bet_type=str(b.get("bet_type") or "UNKNOWN"),
                selection=str(b.get("selection") or ""),
                side=side,
                line=line,
                raw=str(b.get("raw") or ""),
            )
        )
    return bets


def summarize_daily_article(
    client: anthropic.Anthropic,
    query: str,
    matchups: list[Matchup],
    art: ArticleRef,
    *,
    model: str | None = None,
) -> DailyArticleFindings:
    """Extract slate-wide best bets from a single article."""
    resp = client.messages.create(
        model=model or _model(),
        max_tokens=DAILY_MAX_TOKENS,
        system=SUMMARIZE_SYSTEM,
        messages=[{"role": "user", "content": _build_daily_prompt(query, matchups, art)}],
    )
    return DailyArticleFindings(
        title=art.title,
        url=art.url,
        published=art.published,
        best_bets=_parse_tagged_bets(_response_text(resp)),
    )


def summarize_daily_articles(
    client: anthropic.Anthropic,
    query: str,
    matchups: list[Matchup],
    articles: list[ArticleRef],
    *,
    model: str | None = None,
    log: LogFn | None = None,
) -> list[DailyArticleFindings]:
    """Process each article individually (no pairing)."""
    results: list[DailyArticleFindings] = []
    for i, art in enumerate(articles, start=1):
        if log:
            log(f"  [{i}/{len(articles)}] summarizing: {art.title[:80]}")
        try:
            findings = summarize_daily_article(client, query, matchups, art, model=model)
        except Exception as exc:  # noqa: BLE001 — keep pipeline moving
            if log:
                log(f"    summarize failed ({exc}); skipping bets")
            findings = DailyArticleFindings(
                title=art.title,
                url=art.url,
                published=art.published,
                best_bets=[],
            )
        results.append(findings)
        if log:
            log(f"    extracted {len(findings.best_bets)} bets")
    return results
