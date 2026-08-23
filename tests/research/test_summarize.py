"""Tests for summarize JSON parsing (no live Anthropic calls)."""

from __future__ import annotations

from polymaker.research.schemas import ArticleRef
from polymaker.research.summarize import _build_prompt, _parse_article_json


def test_prompt_prefers_labeled_best_bets_over_odds_snapshots() -> None:
    art = ArticleRef(title="A", url="https://a.example", content="...")
    prompt = _build_prompt("Rays Athletics best bets", art)
    assert "INFORMATIONAL" in prompt
    assert "Best Bet: Tampa Bay Rays moneyline -156" in prompt
    assert "extract ONLY those recommended picks" in prompt
    assert "ARTICLE B" not in prompt


def test_parse_article_json_happy_path() -> None:
    art = ArticleRef(title="A", url="https://a.example")
    raw = """```json
{
  "articles": [
    {
      "title": "A",
      "best_bets": [
        {"bet_type": "MONEYLINE", "selection": "Tigers ML", "side": null, "line": null, "raw": "Tigers"}
      ]
    }
  ]
}
```"""
    parsed = _parse_article_json(raw, art)
    assert parsed.url == "https://a.example"
    assert parsed.best_bets[0].selection == "Tigers ML"


def test_parse_article_json_invalid_returns_empty_bets() -> None:
    art = ArticleRef(title="A", url="https://a.example")
    parsed = _parse_article_json("not json", art)
    assert parsed.best_bets == []
    assert parsed.url == "https://a.example"
