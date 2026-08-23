"""Tests for unit sizing and pick matching."""

from __future__ import annotations

import pytest

from polymaker.research.schemas import ArticleBets, BestPlay, ExtractedBet
from polymaker.research.sizer import (
    article_opposes,
    article_supports,
    parse_our_pick,
    size_play,
    units_from_sides,
    units_from_support,
)


@pytest.mark.parametrize(
    ("support", "n", "units"),
    [
        (4, 4, 2.0),
        (3, 4, 2.0),
        (2, 4, 1.0),
        (1, 4, 0.5),
        (0, 4, 0.5),
        (0, 0, 1.0),
    ],
)
def test_units_from_support(support: int, n: int, units: float) -> None:
    got, pct = units_from_support(support, n)
    assert got == units
    if n:
        assert pct == support / n
    else:
        assert pct == 0.0


@pytest.mark.parametrize(
    ("support", "opposite", "units"),
    [
        (3, 1, 2.0),
        (2, 0, 2.0),
        (2, 1, 2.0),
        (1, 0, 1.0),  # need multiple articles to increase
        (0, 2, 0.5),
        (1, 3, 0.5),
        (2, 2, 1.0),  # tie keeps base
        (0, 0, 1.0),
        (0, 1, 1.0),  # single opposite is not enough to decrease
    ],
)
def test_units_from_sides(support: int, opposite: int, units: float) -> None:
    got, _pct = units_from_sides(support, opposite)
    assert got == units


def test_parse_moneyline() -> None:
    play = BestPlay(
        matchup="Detroit Tigers @ San Francisco Giants",
        pick="Detroit Tigers ML",
        bet_type="MONEYLINE",
        category="ml",
    )
    n = parse_our_pick(play)
    assert n.bet_type == "MONEYLINE"
    assert n.team == "detroit tigers"


def test_parse_run_line() -> None:
    play = BestPlay(
        matchup="Athletics @ Boston Red Sox",
        pick="Boston Red Sox -1.5",
        bet_type="RUN_LINE",
        category="ml",
    )
    n = parse_our_pick(play)
    assert n.bet_type == "RUN_LINE"
    assert n.team == "boston red sox"
    assert n.line == -1.5


def test_parse_under() -> None:
    play = BestPlay(
        matchup="Baltimore Orioles @ Texas Rangers",
        pick="UNDER 7.5",
        bet_type="TOTAL",
        category="ou",
    )
    n = parse_our_pick(play)
    assert n.bet_type == "TOTAL"
    assert n.side == "UNDER"
    assert n.line == 7.5


def test_article_supports_moneyline() -> None:
    play = BestPlay(
        matchup="Houston Astros @ San Diego Padres",
        pick="Houston Astros ML",
        bet_type="MONEYLINE",
        category="ml",
    )
    art = ArticleBets(
        title="Picks",
        url="https://example.com",
        best_bets=[
            ExtractedBet(
                bet_type="MONEYLINE",
                selection="Houston Astros ML",
                raw="take the Astros moneyline",
            )
        ],
    )
    assert article_supports(play, art) is True


def test_run_line_supports_moneyline() -> None:
    play = BestPlay(
        matchup="Detroit Tigers @ San Francisco Giants",
        pick="Detroit Tigers ML",
        bet_type="MONEYLINE",
        category="ml",
    )
    art = ArticleBets(
        title="DK",
        best_bets=[
            ExtractedBet(
                bet_type="RUN_LINE",
                selection="Detroit Tigers -1.5",
                side="AWAY",
                line=-1.5,
                raw="Best bet: Tigers -1.5 (+124)",
            )
        ],
    )
    assert article_supports(play, art) is True


def test_american_odds_moneyline_in_content() -> None:
    play = BestPlay(
        matchup="Detroit Tigers @ San Francisco Giants",
        pick="Detroit Tigers ML",
        bet_type="MONEYLINE",
        category="ml",
    )
    art = ArticleBets(
        title="SCP",
        best_bets=[],
        content="Chris Ruffolo's Free Pick: Detroit Tigers -116",
    )
    assert article_supports(play, art) is True


def test_opposite_team_run_line_does_not_support_ml() -> None:
    play = BestPlay(
        matchup="Detroit Tigers @ San Francisco Giants",
        pick="Detroit Tigers ML",
        bet_type="MONEYLINE",
        category="ml",
    )
    art = ArticleBets(
        title="Opp",
        best_bets=[
            ExtractedBet(
                bet_type="RUN_LINE",
                selection="San Francisco Giants -1.5",
                line=-1.5,
                raw="Giants -1.5",
            )
        ],
    )
    assert article_supports(play, art) is False
    assert article_opposes(play, art) is True


def test_article_supports_under_variants() -> None:
    play = BestPlay(
        matchup="Baltimore Orioles @ Texas Rangers",
        pick="UNDER 7.5",
        bet_type="TOTAL",
        category="ou",
    )
    art = ArticleBets(
        title="Totals",
        best_bets=[
            ExtractedBet(
                bet_type="TOTAL",
                selection="Under 7.5",
                side="UNDER",
                line=7.5,
                raw="u7.5",
            )
        ],
    )
    assert article_supports(play, art) is True


def test_article_rejects_opposite_total() -> None:
    play = BestPlay(
        matchup="Baltimore Orioles @ Texas Rangers",
        pick="UNDER 7.5",
        bet_type="TOTAL",
        category="ou",
    )
    art = ArticleBets(
        title="Totals",
        best_bets=[
            ExtractedBet(
                bet_type="TOTAL",
                selection="OVER 7.5",
                side="OVER",
                line=7.5,
            )
        ],
    )
    assert article_supports(play, art) is False
    assert article_opposes(play, art) is True


def test_props_only_article_is_skipped() -> None:
    play = BestPlay(
        matchup="Detroit Tigers @ San Francisco Giants",
        pick="Detroit Tigers ML",
        bet_type="MONEYLINE",
        category="ml",
    )
    prop_art = ArticleBets(
        title="Tony Picks",
        url="https://example.com/prop",
        game_relevant=False,
        best_bets=[
            ExtractedBet(
                bet_type="TOTAL",
                selection="Jackson Jobe Under 14.5 pitcher outs",
                side="UNDER",
                line=14.5,
                raw="Jackson Jobe Under 14.5 pitcher outs at -130",
            )
        ],
    )
    support_art = ArticleBets(
        title="ML",
        best_bets=[
            ExtractedBet(
                bet_type="MONEYLINE",
                selection="Detroit Tigers ML",
                raw="Tigers ML",
            )
        ],
    )
    sized = size_play(play, [prop_art, support_art, prop_art, support_art])
    assert sized.sources[0].skipped is True
    assert sized.sources[0].supports is None
    assert sized.support_count == 2
    assert sized.opposite_count == 0
    assert sized.article_count == 2
    assert sized.units == 2.0


def test_props_only_detected_without_llm_flag() -> None:
    play = BestPlay(
        matchup="Detroit Tigers @ San Francisco Giants",
        pick="Detroit Tigers ML",
        bet_type="MONEYLINE",
        category="ml",
    )
    art = ArticleBets(
        title="Prop",
        best_bets=[
            ExtractedBet(
                bet_type="TOTAL",
                selection="Jackson Jobe Under 14.5 pitcher outs",
                side="UNDER",
                line=14.5,
                raw="recommended player prop",
            )
        ],
    )
    sized = size_play(play, [art])
    assert sized.sources[0].skipped is True
    assert sized.sources[0].skip_reason == "props_only"
    assert sized.article_count == 0
    assert sized.units == 1.0


def test_size_play_aggregates_support_vs_opposite() -> None:
    play = BestPlay(
        matchup="Minnesota Twins @ Milwaukee Brewers",
        pick="Milwaukee Brewers ML",
        bet_type="MONEYLINE",
        category="ml",
    )
    support_bet = ExtractedBet(
        bet_type="MONEYLINE",
        selection="Milwaukee Brewers moneyline",
        raw="Brewers ML",
    )
    opposite_bet = ExtractedBet(
        bet_type="MONEYLINE",
        selection="Minnesota Twins ML",
        raw="Twins ML",
    )
    articles = [
        ArticleBets(title="a1", best_bets=[support_bet]),
        ArticleBets(title="a2", best_bets=[support_bet]),
        ArticleBets(title="a3", best_bets=[opposite_bet]),
        ArticleBets(title="a4", best_bets=[]),  # no stance — ignored
    ]
    sized = size_play(play, articles, query="test")
    assert sized.support_count == 2
    assert sized.opposite_count == 1
    assert sized.article_count == 3
    assert sized.units == 2.0
    assert sized.sources[0].supports is True
    assert sized.sources[2].opposes is True
    assert sized.sources[3].supports is None


def test_size_play_decreases_on_opposite_majority() -> None:
    play = BestPlay(
        matchup="Boston Red Sox @ Toronto Blue Jays",
        pick="Boston Red Sox ML",
        bet_type="MONEYLINE",
        category="ml",
    )
    articles = [
        ArticleBets(
            title="a1",
            best_bets=[ExtractedBet(bet_type="MONEYLINE", selection="Toronto Blue Jays ML")],
        ),
        ArticleBets(
            title="a2",
            best_bets=[ExtractedBet(bet_type="MONEYLINE", selection="Toronto Blue Jays ML")],
        ),
        ArticleBets(
            title="a3",
            best_bets=[ExtractedBet(bet_type="MONEYLINE", selection="Boston Red Sox ML")],
        ),
    ]
    sized = size_play(play, articles)
    assert sized.support_count == 1
    assert sized.opposite_count == 2
    assert sized.units == 0.5
