"""SummarizeAgent — extract WNBA best bets from articles via Anthropic."""

from __future__ import annotations

import json
import os
from typing import Any

import anthropic

from wnba_bot.schemas import ArticleFindings, ArticleRef, ExtractedBet, Matchup

DEFAULT_MODEL = "claude-haiku-4-5-20251001"

SUMMARIZE_SYSTEM = (
    "You extract WNBA sports betting best bets from articles. "
    "List only concrete game bets the article recommends as plays "
    "(moneyline, point spread, over/under totals). "
    "Do not treat odds tables, morning line snapshots, or market roundups as picks. "
    "Do not explain why. Do not invent picks not present in the text. "
    "Ignore player props. Return valid JSON only."
)


def _model() -> str:
    return (os.getenv("ANTHROPIC_MODEL") or "").strip() or DEFAULT_MODEL


def _lines_block(matchup: Matchup) -> str:
    lines = matchup.lines
    total = lines.total
    total_hint = ""
    try:
        t = float(total)
        total_hint = (
            f" (books drift; treat totals roughly {t - 1.5:.1f}–{t + 1.5:.1f} as this game total)"
        )
    except (TypeError, ValueError):
        pass
    return f"""Reference lines for this game (Team1=away, Team2=home):
- Away: {matchup.away} | Home: {matchup.home}
- Moneyline: {matchup.away} {lines.ml_away} / {matchup.home} {lines.ml_home}
- Spread: {matchup.away} {lines.spread_away} / {matchup.home} {lines.spread_home}
- Total: {total}{total_hint}
Use these to recognize when an article is picking this game's ML, spread, or total even if the number is slightly different."""


def _bet_schema_example() -> str:
    return """{
  "bet_type": "MONEYLINE | SPREAD | TOTAL",
  "selection": "short pick label e.g. Atlanta Dream ML or UNDER 186.5",
  "side": "HOME | AWAY | OVER | UNDER | null",
  "line": null,
  "raw": "phrase from the article"
}"""


def _extraction_rules() -> str:
    return """Search carefully for recommended picks, prioritizing labeled recommendations:
- "best bet", "best bets", "my play", "free play", "free pick", "prediction", "lean", "play"
- moneyline / ML
- spread / point spread
- over / under game totals

Distinguish recommendations from market info:
- Odds tables, "morning snapshot", current lines, or both sides of ML/spread/total listed together are INFORMATIONAL — do not extract those as best_bets.
- If the article has an explicit Best Bet / Free Pick / Play section, extract ONLY those recommended picks (not every line mentioned earlier for context).
- Example: an article may show Rays -156 / Athletics +132 / RL / total as a snapshot, then later say "Best Bet: Tampa Bay Rays moneyline -156" — return only that Rays ML pick.
- Only include a line if the article clearly endorses it as a pick to bet. Listing a price is not a recommendation.

Rules:
- bet_type must be MONEYLINE, SPREAD, or TOTAL.
- "Team -150" / "Team +120" (American odds, 3+ digits) is MONEYLINE, not a spread.
- "Team -4.5" / "Team +12.5" is SPREAD.
- Game totals only (e.g. UNDER 186.5). Do NOT list player props (points, rebounds, assists, PRA, threes) as TOTAL.
- For totals, set side to OVER or UNDER and line to the number when known.
- For spreads, put the spread in line when known (e.g. -12.5) and side HOME or AWAY when clear.
- Keep selection concise. Return only valid JSON."""


def _build_pair_prompt(
    query: str,
    matchup: Matchup,
    art_a: ArticleRef,
    art_b: ArticleRef,
) -> str:
    text_a = art_a.content or art_a.snippet
    text_b = art_b.content or art_b.snippet
    return f"""Extract the best bets listed in each of these two articles about: "{query}"

{_lines_block(matchup)}

{_extraction_rules()}

For each article, list only the concrete picks the article recommends as plays.
Ignore informational odds listings. Prefer labeled Best Bet / Free Pick sections when present.
If an article has no clear recommended bets, return an empty best_bets array for that article.

Also set game_relevant:
- true if the article discusses this game's moneyline, spread, and/or game total
- false if it is only about player props or a different topic

=== ARTICLE A: {art_a.title} ===
{text_a}

=== ARTICLE B: {art_b.title} ===
{text_b}

Return a JSON object with this exact structure:
{{
  "articles": [
    {{
      "title": {json.dumps(art_a.title)},
      "game_relevant": true,
      "best_bets": [
        {_bet_schema_example()}
      ]
    }},
    {{
      "title": {json.dumps(art_b.title)},
      "game_relevant": false,
      "best_bets": []
    }}
  ]
}}"""


def _build_single_prompt(query: str, matchup: Matchup, art: ArticleRef) -> str:
    text = art.content or art.snippet
    return f"""Extract the best bets listed in this article about: "{query}"

{_lines_block(matchup)}

{_extraction_rules()}

List only the concrete picks the article recommends as plays.
Ignore informational odds listings. Prefer labeled Best Bet / Free Pick sections when present.
If there are no clear recommended bets, return an empty best_bets array.

Also set game_relevant:
- true if the article discusses this game's moneyline, spread, and/or game total
- false if it is only about player props or a different topic

=== ARTICLE: {art.title} ===
{text}

Return a JSON object with this exact structure:
{{
  "articles": [
    {{
      "title": {json.dumps(art.title)},
      "game_relevant": true,
      "best_bets": [
        {_bet_schema_example()}
      ]
    }}
  ]
}}"""


def _strip_fences(raw: str) -> str:
    text = raw.strip()
    if text.startswith("```"):
        parts = text.split("```")
        if len(parts) >= 2:
            text = parts[1]
            if text.startswith("json"):
                text = text[4:]
    return text.strip()


def _parse_bets(row: dict[str, Any]) -> list[ExtractedBet]:
    bets: list[ExtractedBet] = []
    for b in row.get("best_bets") or []:
        if not isinstance(b, dict):
            continue
        line_val = b.get("line")
        line: float | None
        try:
            line = float(line_val) if line_val is not None else None
        except (TypeError, ValueError):
            line = None
        side_raw = b.get("side")
        side = None if side_raw in (None, "null", "") else str(side_raw)
        bets.append(
            ExtractedBet(
                bet_type=str(b.get("bet_type") or "UNKNOWN"),
                selection=str(b.get("selection") or ""),
                side=side,
                line=line,
                raw=str(b.get("raw") or ""),
            )
        )
    return bets


def _parse_game_relevant(row: dict[str, Any]) -> bool | None:
    gr_raw = row.get("game_relevant")
    if gr_raw is True or gr_raw is False:
        return gr_raw
    if isinstance(gr_raw, str) and gr_raw.lower() in ("true", "false"):
        return gr_raw.lower() == "true"
    return None


def _parse_articles_json(raw: str, fallbacks: list[ArticleRef]) -> list[ArticleFindings]:
    try:
        data: dict[str, Any] = json.loads(_strip_fences(raw))
    except json.JSONDecodeError:
        return [
            ArticleFindings(title=f.title, url=f.url, best_bets=[], game_relevant=None)
            for f in fallbacks
        ]

    articles_raw = data.get("articles") or []
    out: list[ArticleFindings] = []
    for i, fallback in enumerate(fallbacks):
        row = articles_raw[i] if i < len(articles_raw) and isinstance(articles_raw[i], dict) else {}
        out.append(
            ArticleFindings(
                title=str(row.get("title") or fallback.title),
                url=fallback.url,
                best_bets=_parse_bets(row),
                game_relevant=_parse_game_relevant(row),
            )
        )
    return out


def _response_text(resp: Any) -> str:
    raw_parts: list[str] = []
    for block in resp.content:
        text = getattr(block, "text", None)
        if isinstance(text, str):
            raw_parts.append(text)
    return "".join(raw_parts)


def summarize_pair(
    client: anthropic.Anthropic,
    query: str,
    matchup: Matchup,
    art_a: ArticleRef,
    art_b: ArticleRef,
    *,
    model: str | None = None,
) -> list[ArticleFindings]:
    """Send two articles to Claude; return per-article structured best bets."""
    resp = client.messages.create(
        model=model or _model(),
        max_tokens=1200,
        system=SUMMARIZE_SYSTEM,
        messages=[{"role": "user", "content": _build_pair_prompt(query, matchup, art_a, art_b)}],
    )
    return _parse_articles_json(_response_text(resp), [art_a, art_b])


def summarize_one(
    client: anthropic.Anthropic,
    query: str,
    matchup: Matchup,
    art: ArticleRef,
    *,
    model: str | None = None,
) -> ArticleFindings:
    """Send one article to Claude; return structured best bets."""
    resp = client.messages.create(
        model=model or _model(),
        max_tokens=800,
        system=SUMMARIZE_SYSTEM,
        messages=[{"role": "user", "content": _build_single_prompt(query, matchup, art)}],
    )
    parsed = _parse_articles_json(_response_text(resp), [art])
    return parsed[0]


def summarize_articles(
    client: anthropic.Anthropic,
    query: str,
    matchup: Matchup,
    articles: list[ArticleRef],
    *,
    model: str | None = None,
) -> list[ArticleFindings]:
    """Summarize articles as pairs, then a single leftover when count is odd."""
    results: list[ArticleFindings] = []
    i = 0
    while i < len(articles):
        if i + 1 < len(articles):
            results.extend(
                summarize_pair(
                    client, query, matchup, articles[i], articles[i + 1], model=model
                )
            )
            i += 2
        else:
            results.append(summarize_one(client, query, matchup, articles[i], model=model))
            i += 1
    return results


def make_client(api_key: str | None = None) -> anthropic.Anthropic:
    key = api_key or os.getenv("ANTHROPIC_API_KEY")
    if not key:
        raise RuntimeError("ANTHROPIC_API_KEY not set")
    return anthropic.Anthropic(api_key=key)
