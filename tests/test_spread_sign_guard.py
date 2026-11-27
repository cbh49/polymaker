"""Spread buys must agree with Polymarket's own signed line for the bought team.

Regression for UVA @ XAV (2026-11-27): the sharp play was Xavier +7.5, but the
Polymarket event lists Xavier as the away team, so the slug's `spread-away-7pt5`
market is Xavier -7.5. The bot bought that. The guard must refuse it.
"""

from __future__ import annotations

import json

from polymaker.trading.match import spread_sign_problem

SLUG_XAV_MINUS_7 = "cbb-xav-vir-2026-11-27-spread-away-7pt5"
SLUG_VIR_MINUS_7 = "cbb-xav-vir-2026-11-27-spread-home-7pt5"


def _event(*markets: dict) -> dict:
    return {"slug": "cbb-xav-vir-2026-11-27", "markets": list(markets)}


def _market(slug: str, outcomes: list[str], title: str) -> dict:
    # Gamma returns outcomes as a JSON string; the guard must accept both forms.
    return {"slug": slug, "outcomes": json.dumps(outcomes), "groupItemTitle": title}


# Real shape from Gamma: outcomes list the spread team first.
EVENT = _event(
    _market(SLUG_XAV_MINUS_7, ["Xavier Musketeers", "Virginia Cavaliers"], "Spread -7.5"),
    _market(SLUG_VIR_MINUS_7, ["Virginia Cavaliers", "Xavier Musketeers"], "Spread -7.5"),
)


def test_bought_xavier_minus_7_is_refused_for_a_plus_7_5_play() -> None:
    problem = spread_sign_problem(
        EVENT,
        slug=SLUG_XAV_MINUS_7,
        outcome="Xavier Musketeers",
        play_line=7.5,
    )
    assert problem is not None
    assert "sign mismatch" in problem


def test_xavier_plus_7_5_in_the_virginia_market_is_allowed() -> None:
    # Same play, correct market: Virginia -7.5 means Xavier +7.5.
    assert (
        spread_sign_problem(
            EVENT,
            slug=SLUG_VIR_MINUS_7,
            outcome="Xavier Musketeers",
            play_line=7.5,
        )
        is None
    )


def test_a_minus_line_play_on_the_favorite_passes() -> None:
    assert (
        spread_sign_problem(
            EVENT,
            slug=SLUG_XAV_MINUS_7,
            outcome="Xavier Musketeers",
            play_line=-7.5,
        )
        is None
    )


def test_small_line_difference_within_tolerance_passes() -> None:
    event = _event(_market(SLUG_VIR_MINUS_7, ["Virginia Cavaliers", "Xavier Musketeers"], "Spread -8.5"))
    assert (
        spread_sign_problem(event, slug=SLUG_VIR_MINUS_7, outcome="Xavier Musketeers", play_line=7.5)
        is None
    )


def test_unreadable_or_missing_market_fails_closed() -> None:
    assert spread_sign_problem(None, slug=SLUG_VIR_MINUS_7, outcome="Xavier Musketeers", play_line=7.5)
    assert spread_sign_problem(
        _event(), slug=SLUG_VIR_MINUS_7, outcome="Xavier Musketeers", play_line=7.5
    )
    no_title = _event({"slug": SLUG_VIR_MINUS_7, "outcomes": ["A", "B"], "groupItemTitle": ""})
    assert spread_sign_problem(
        no_title, slug=SLUG_VIR_MINUS_7, outcome="Xavier Musketeers", play_line=7.5
    )
    assert spread_sign_problem(
        EVENT, slug=SLUG_VIR_MINUS_7, outcome="Xavier Musketeers", play_line=None
    )
