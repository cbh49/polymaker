"""Unit tests for WNBA consensus clustering."""

from __future__ import annotations

from wnba_bot.consensus import build_consensus_for_game
from wnba_bot.schemas import (
    ArticleFindings,
    ExtractedBet,
    GameFindings,
    Matchup,
    MatchupLines,
)


def _matchup() -> Matchup:
    return Matchup(
        away="Toronto Tempo",
        home="Atlanta Dream",
        game_time="8:00PM",
        lines=MatchupLines(
            ml_away="+525",
            ml_home="-750",
            spread_away="+12.5",
            spread_home="-12.5",
            total="186.5",
        ),
    )


def test_consensus_requires_two_articles() -> None:
    game = GameFindings(
        away="Toronto Tempo",
        home="Atlanta Dream",
        articles=[
            ArticleFindings(
                title="A",
                url="https://a.example",
                best_bets=[
                    ExtractedBet(
                        bet_type="TOTAL",
                        selection="UNDER 186.5",
                        side="UNDER",
                        line=186.5,
                        raw="Best bet: Under 186.5",
                    )
                ],
            ),
            ArticleFindings(
                title="B",
                url="https://b.example",
                best_bets=[
                    ExtractedBet(
                        bet_type="TOTAL",
                        selection="UNDER 185.5",
                        side="UNDER",
                        line=185.5,
                        raw="Play the under 185.5",
                    )
                ],
            ),
            ArticleFindings(
                title="C",
                url="https://c.example",
                best_bets=[
                    ExtractedBet(
                        bet_type="MONEYLINE",
                        selection="Atlanta Dream ML",
                        side="HOME",
                        line=None,
                        raw="Dream moneyline",
                    )
                ],
            ),
        ],
    )
    out = build_consensus_for_game(game, _matchup())
    assert len(out.best_bets) == 1
    bet = out.best_bets[0]
    assert bet.bet_type == "TOTAL"
    assert bet.side == "UNDER"
    assert bet.mention_count == 2
    assert bet.line == 186.0  # median of 186.5 and 185.5


def test_same_article_does_not_double_count() -> None:
    game = GameFindings(
        away="Toronto Tempo",
        home="Atlanta Dream",
        articles=[
            ArticleFindings(
                title="A",
                url="https://a.example",
                best_bets=[
                    ExtractedBet(
                        bet_type="SPREAD",
                        selection="Dream -12.5",
                        side="HOME",
                        line=-12.5,
                        raw="Atlanta -12.5",
                    ),
                    ExtractedBet(
                        bet_type="SPREAD",
                        selection="Atlanta Dream -12",
                        side="HOME",
                        line=-12.0,
                        raw="also Dream -12",
                    ),
                ],
            ),
        ],
    )
    out = build_consensus_for_game(game, _matchup())
    assert out.best_bets == []
