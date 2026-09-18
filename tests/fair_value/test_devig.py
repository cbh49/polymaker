"""Devig math: American odds, multiplicative, and Shin."""

from __future__ import annotations

import pytest

from ev_trading.fair_value.devig import (
    american_to_prob,
    as_implied_prob,
    devig_one_sided,
    devig_two_way,
    multiplicative_devig,
    shin_devig_two_way,
)


def test_american_minus_110() -> None:
    assert american_to_prob(-110) == pytest.approx(110 / 210, rel=1e-9)


def test_american_plus_150() -> None:
    assert american_to_prob(150) == pytest.approx(100 / 250, rel=1e-9)


def test_american_minus_200() -> None:
    assert american_to_prob(-200) == pytest.approx(200 / 300, rel=1e-9)


def test_as_implied_prob_passes_through_unit_interval() -> None:
    assert as_implied_prob(0.5238) == pytest.approx(0.5238)
    assert as_implied_prob(-110) == pytest.approx(american_to_prob(-110))


def test_multiplicative_balanced_juice() -> None:
    a, b = multiplicative_devig(american_to_prob(-110), american_to_prob(-110))
    assert a == pytest.approx(0.5)
    assert b == pytest.approx(0.5)
    assert a + b == pytest.approx(1.0)


def test_multiplicative_from_american_odds_direct() -> None:
    a, b = devig_two_way(-110, -110, method="multiplicative")
    assert a == pytest.approx(0.5)
    assert b == pytest.approx(0.5)


def test_multiplicative_skewed_line() -> None:
    fav, dog = devig_two_way(-200, 170, method="multiplicative")
    assert fav + dog == pytest.approx(1.0)
    assert fav > dog
    raw_fav = american_to_prob(-200)
    raw_dog = american_to_prob(170)
    assert fav == pytest.approx(raw_fav / (raw_fav + raw_dog))


def test_shin_balanced_equals_half() -> None:
    a, b = shin_devig_two_way(american_to_prob(-110), american_to_prob(-110))
    assert a == pytest.approx(0.5, abs=1e-9)
    assert b == pytest.approx(0.5, abs=1e-9)


def test_shin_sums_to_one_and_differs_on_skew() -> None:
    fav_m, dog_m = devig_two_way(-250, 180, method="multiplicative")
    fav_s, dog_s = devig_two_way(-250, 180, method="shin")
    assert fav_s + dog_s == pytest.approx(1.0)
    assert 0 < dog_s < 1 and 0 < fav_s < 1
    # Shin puts more mass on the favorite than multiplicative (longshot overround).
    assert fav_s > fav_m
    assert dog_s < dog_m


def test_devig_one_sided_haircut() -> None:
    fair = devig_one_sided(0.25, overround=1.05)
    assert fair == pytest.approx(0.25 / 1.05)
    assert 0 < fair < 0.25
