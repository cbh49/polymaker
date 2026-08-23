"""Tests for MLB consensus additional-play discovery."""

from __future__ import annotations

from polymaker.research.consensus import build_additional_plays
from polymaker.research.schemas import BestPlay, DailyArticleFindings, Matchup, TaggedBet


def test_additional_plays_require_multiple_articles_and_uncovered_game() -> None:
    matchups = [
        Matchup(away="Chicago Cubs", home="Washington Nationals", game_time="6:45PM"),
        Matchup(away="Boston Red Sox", home="Toronto Blue Jays", game_time="7:07PM"),
    ]
    breton = [
        BestPlay(
            matchup="Boston Red Sox @ Toronto Blue Jays",
            pick="Boston Red Sox ML",
            bet_type="MONEYLINE",
            category="ml",
        )
    ]
    articles = [
        DailyArticleFindings(
            title="a1",
            url="https://example.com/1",
            best_bets=[
                TaggedBet(
                    away="Chicago Cubs",
                    home="Washington Nationals",
                    bet_type="MONEYLINE",
                    selection="Chicago Cubs ML",
                    side="AWAY",
                    raw="Cubs ML",
                ),
                TaggedBet(
                    away="Boston Red Sox",
                    home="Toronto Blue Jays",
                    bet_type="MONEYLINE",
                    selection="Toronto Blue Jays ML",
                    side="HOME",
                    raw="Jays ML",
                ),
            ],
        ),
        DailyArticleFindings(
            title="a2",
            url="https://example.com/2",
            best_bets=[
                TaggedBet(
                    away="Chicago Cubs",
                    home="Washington Nationals",
                    bet_type="MONEYLINE",
                    selection="Cubs moneyline",
                    side="AWAY",
                    raw="take the Cubs",
                )
            ],
        ),
    ]

    extra = build_additional_plays(articles, matchups, breton, query="MLB August 11 Best Bets")
    assert len(extra) == 1
    assert extra[0].pick == "Chicago Cubs ML"
    assert extra[0].matchup == "Chicago Cubs @ Washington Nationals"
    assert extra[0].support_count == 2
    assert extra[0].origin == "consensus"
    # Red Sox/Jays game is covered by Breton plays — not emitted as additional
    assert all("Blue Jays" not in p.matchup for p in extra)
