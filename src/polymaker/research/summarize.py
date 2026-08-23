"""SummarizeAgent — extract best bets from one article at a time via Anthropic."""

from __future__ import annotations

import json
import os
from typing import Any

import anthropic

from polymaker.research.schemas import ArticleBets, ArticleRef, ExtractedBet

DEFAULT_MODEL = "claude-haiku-4-5-20251001"

SUMMARIZE_SYSTEM = (
    "You extract sports betting best bets from articles. "
    "List only concrete bets the article recommends as plays "
    "(moneyline, run line / spread, over/under totals). "
    "Do not treat odds tables, morning line snapshots, or market roundups as picks. "
    "Do not explain why. Do not invent picks not present in the text. "
    "Return valid JSON only."
)


def _model() -> str:
    return (os.getenv("ANTHROPIC_MODEL") or "").strip() or DEFAULT_MODEL


def _build_prompt(query: str, art: ArticleRef) -> str:
    text = art.content or art.snippet
    return f"""Extract the best bets listed in this article about: "{query}"

Search carefully for recommended picks, prioritizing labeled recommendations:
- phrases like "best bet", "free pick", "prediction", "lean", "play"
- moneyline / ML (including "Team -116", "Team +130", "Free Pick: Team -110")
- run line / spread (Team -1.5 / +1.5)
- over / under game totals

Distinguish recommendations from market info:
- Odds tables, "morning snapshot", current lines, or both sides of ML/run line/total listed together are INFORMATIONAL — do not extract those as best_bets.
- If the article has an explicit Best Bet / Free Pick / Play section, extract ONLY those recommended picks (not every line mentioned earlier for context).
- Example: an article may show Rays -156 / Athletics +132 / RL / total as a snapshot, then later say "Best Bet: Tampa Bay Rays moneyline -156" — return only that Rays ML pick.
- Only include a line if the article clearly endorses it as a pick to bet. Listing a price is not a recommendation.

List only the concrete picks the article recommends as plays.
Ignore informational odds listings. Prefer labeled Best Bet / Free Pick sections when present.
If there are no clear recommended bets, return an empty best_bets array.

Also set game_relevant:
- true if the article discusses this game's moneyline, run line, and/or game total
- false if it is only about player props, pitcher props, or a different topic

=== ARTICLE: {art.title} ===
{text}

Return a JSON object with this exact structure:
{{
  "articles": [
    {{
      "title": {json.dumps(art.title)},
      "game_relevant": true,
      "best_bets": [
        {{
          "bet_type": "MONEYLINE | RUN_LINE | TOTAL",
          "selection": "short pick label e.g. Houston Astros ML or UNDER 7.5",
          "side": "HOME | AWAY | OVER | UNDER | null",
          "line": null,
          "raw": "phrase from the article"
        }}
      ]
    }}
  ]
}}

Rules:
- bet_type must be MONEYLINE, RUN_LINE, or TOTAL.
- "Team -150" / "Team +120" (American odds, 3+ digits) is MONEYLINE, not a run line.
- "Team -1.5" / "Team +1.5" is RUN_LINE.
- Game totals only (e.g. UNDER 7.5). Do NOT list player props (pitcher outs, strikeouts, hits, etc.) as TOTAL.
- For totals, set side to OVER or UNDER and line to the number when known.
- For run line, put the spread in line when known (e.g. -1.5).
- Keep selection concise. Return only valid JSON."""


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


def _parse_article_json(raw: str, art: ArticleRef) -> ArticleBets:
    try:
        data: dict[str, Any] = json.loads(_strip_fences(raw))
    except json.JSONDecodeError:
        return ArticleBets(
            title=art.title,
            url=art.url,
            best_bets=[],
            content=art.content or art.snippet,
        )

    articles_raw = data.get("articles") or []
    row = articles_raw[0] if articles_raw and isinstance(articles_raw[0], dict) else {}
    # Allow a flat single-article payload as well as {"articles": [...]}
    if not row and isinstance(data.get("best_bets"), list):
        row = data

    return ArticleBets(
        title=str(row.get("title") or art.title),
        url=art.url,
        best_bets=_parse_bets(row),
        content=art.content or art.snippet,
        game_relevant=_parse_game_relevant(row),
    )


def _response_text(resp: Any) -> str:
    raw_parts: list[str] = []
    for block in resp.content:
        text = getattr(block, "text", None)
        if isinstance(text, str):
            raw_parts.append(text)
    return "".join(raw_parts)


def summarize_one(
    client: anthropic.Anthropic,
    query: str,
    art: ArticleRef,
    *,
    model: str | None = None,
) -> ArticleBets:
    """Send one article to Claude; return structured best bets."""
    resp = client.messages.create(
        model=model or _model(),
        max_tokens=800,
        system=SUMMARIZE_SYSTEM,
        messages=[{"role": "user", "content": _build_prompt(query, art)}],
    )
    return _parse_article_json(_response_text(resp), art)


def summarize_articles(
    client: anthropic.Anthropic,
    query: str,
    articles: list[ArticleRef],
    *,
    model: str | None = None,
) -> list[ArticleBets]:
    """Summarize each article individually."""
    return [summarize_one(client, query, art, model=model) for art in articles]


def make_client(api_key: str | None = None) -> anthropic.Anthropic:
    key = api_key or os.getenv("ANTHROPIC_API_KEY")
    if not key:
        raise RuntimeError("ANTHROPIC_API_KEY not set")
    return anthropic.Anthropic(api_key=key)
