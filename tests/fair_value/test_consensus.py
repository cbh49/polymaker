"""Distribution fit recovers a known μ/σ; fair_prob_at_line matches the generator."""

from __future__ import annotations

from statistics import NormalDist

import pytest

from ev_trading.fair_value.config import FairValueConfig
from ev_trading.fair_value.consensus import (
    BookPoint,
    extrapolation_for,
    fair_prob_at_line,
    fit_distribution,
    fit_poisson,
    poisson_cdf,
    same_strike_consensus,
)


def _points_from_normal(mu: float, sigma: float, lines: list[float]) -> list[BookPoint]:
    dist = NormalDist(mu, sigma)
    out: list[BookPoint] = []
    for i, line in enumerate(lines):
        p_over = 1.0 - dist.cdf(line)
        out.append(BookPoint(book=f"b{i}", line=line, fair_over=p_over, weight=1.0))
    return out


def test_normal_fit_recovers_ground_truth() -> None:
    mu, sigma = 220.0, 45.0
    lines = [190.5, 205.5, 220.5, 235.5, 250.5]
    cfg = FairValueConfig(min_z_variance=0.01, min_r2=0.9)
    fit = fit_distribution(
        _points_from_normal(mu, sigma, lines),
        "normal",
        stat="passing_yards",
        cfg=cfg,
    )
    assert fit.sigma_source == "fitted"
    assert fit.mu == pytest.approx(mu, abs=0.5)
    assert fit.sigma == pytest.approx(sigma, abs=0.5)
    assert fit.r2 is not None and fit.r2 > 0.99
    assert not fit.low_confidence


def test_fair_prob_at_line_matches_normal_cdf() -> None:
    mu, sigma, line = 213.0, 62.0, 224.5
    got = fair_prob_at_line(mu, sigma, line, "normal")
    expected = 1.0 - NormalDist(mu, sigma).cdf(line)
    assert got == pytest.approx(expected, rel=1e-9)


def test_low_book_count_is_low_confidence_not_dropped() -> None:
    points = _points_from_normal(30.0, 20.0, [28.5, 31.5])
    cfg = FairValueConfig(min_books_for_fit=4)
    fit = fit_distribution(points, "normal", stat="receiving_yards", cfg=cfg)
    assert fit.n_books == 2
    assert fit.low_confidence is True


def test_same_strike_weighted_average() -> None:
    points = [
        BookPoint("a", 3.5, 0.50, 1.0),
        BookPoint("b", 3.5, 0.54, 3.0),
    ]
    cons = same_strike_consensus(points)
    assert cons.fair_prob == pytest.approx((0.50 + 1.62) / 4.0)
    assert cons.line == pytest.approx(3.5)
    assert cons.n_books == 2


def test_poisson_fit_recovers_lambda() -> None:
    lam = 1.8
    lines = [0.5, 1.5, 2.5, 3.5]
    points: list[BookPoint] = []
    for i, line in enumerate(lines):
        p_over = 1.0 - poisson_cdf(int(line), lam)
        points.append(BookPoint(f"b{i}", line, p_over, 1.0))
    fit = fit_poisson(points)
    assert fit.distribution_type == "poisson"
    assert fit.mu == pytest.approx(lam, abs=0.08)


def test_poisson_fair_prob_at_line() -> None:
    # Over 1.5 → P(X >= 2) = 1 - F(1)
    p = fair_prob_at_line(2.0, 0.0, 1.5, "poisson")
    assert p == pytest.approx(1.0 - poisson_cdf(1, 2.0), rel=1e-9)


def test_same_strike_qb_rush_does_not_use_rb_sigma() -> None:
    """Books clustered at 0.5 must not inherit σ=32 (RB rushing default)."""
    points = [
        BookPoint("betr", 0.5, 0.456, 1.0),
        BookPoint("circasports", 0.5, 0.429, 1.5),
        BookPoint("draftkings", 0.5, 0.441, 1.0),
        BookPoint("fanduel", 0.5, 0.427, 1.0),
        BookPoint("hardrock", 0.5, 0.428, 1.0),
        BookPoint("thescore", 0.5, 0.437, 1.0),
    ]
    fit = fit_distribution(points, "normal", stat="rushing_yards")
    assert fit.sigma_source == "fallback"
    assert fit.role_bucket == "low_volume"
    assert fit.sigma == pytest.approx(6.0)
    p_over_longshot = fair_prob_at_line(fit.mu, fit.sigma, 9.5, "normal")
    # Old bug: σ=32 → P(over 9.5) ≈ 0.39, so a 19¢ Kalshi over looked +EV.
    assert p_over_longshot < 0.20
    p_over_main = fair_prob_at_line(fit.mu, fit.sigma, 0.5, "normal")
    assert p_over_main == pytest.approx(
        sum(p.fair_over * p.weight for p in points) / sum(p.weight for p in points),
        abs=0.01,
    )


def test_typical_rb_keeps_full_default_sigma() -> None:
    points = [
        BookPoint("a", 65.5, 0.50, 1.0),
        BookPoint("b", 65.5, 0.48, 1.0),
        BookPoint("c", 65.5, 0.52, 1.0),
        BookPoint("d", 65.5, 0.49, 1.0),
    ]
    fit = fit_distribution(points, "normal", stat="rushing_yards")
    assert fit.sigma == pytest.approx(32.0)
    assert fit.role_bucket == "workhorse"
    cfg = FairValueConfig()
    assert cfg.scaled_default_sigma("spread", 3.5) == pytest.approx(13.5)
    assert cfg.role_bucket_for("receiving_yards", 7.5)[0] == "low_target_share"
    assert cfg.scaled_default_sigma("receiving_yards", 7.5) == pytest.approx(10.0)
    lo, hi = cfg.sigma_bounds_for("receiving_yards", 7.5)
    assert (lo, hi) == (4.0, 16.0)
    lo_wr, hi_wr = cfg.sigma_bounds_for("receiving_yards", 85.5)
    assert (lo_wr, hi_wr) == (12.0, 50.0)


def test_extrapolation_flag_is_outside_observed_book_range() -> None:
    points = [
        BookPoint("a", 6.5, 0.50, 1.0),
        BookPoint("b", 7.5, 0.48, 1.0),
        BookPoint("c", 10.5, 0.40, 1.0),
    ]
    is_extra, dist = extrapolation_for(14.5, points)
    assert is_extra is True
    assert dist == pytest.approx(4.0)
    inside, zero = extrapolation_for(7.5, points)
    assert inside is False
    assert zero == pytest.approx(0.0)
