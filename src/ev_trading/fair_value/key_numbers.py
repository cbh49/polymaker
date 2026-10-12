"""NFL spread translation with a key-number margin distribution.

A Normal with σ ≈ 13.5 treats the half-point through 3 like any other half-point.
This prior puts extra mass on 3, 7, 6, 10, 4, and 14, then shifts the distribution
so it matches the de-vigged cover probability at the book line.

Integer book lines are P(cover | no push): the push is refunded. Kalshi and
Polymarket are strict ("wins by over X"), so the venue probability excludes the push.
"""

from __future__ import annotations

import math
from statistics import NormalDist

# Boosts vs a discretized Normal(0, 13.5). 3 is the largest key, then 7.
_KEY_BOOSTS: dict[int, float] = {
    3: 3.2,
    7: 2.4,
    6: 1.8,
    10: 1.6,
    4: 1.5,
    14: 1.4,
}
_BASE_SIGMA = 13.5


def _is_integer_line(line: float) -> bool:
    return abs(line - round(line)) < 1e-6


def _pmf(mu: float) -> dict[int, float]:
    """Integer margin PMF centered near μ, with extra mass on absolute key numbers.

    The bumps sit on the final margin (3, 7, …), not on the residual. Shifting
    the center moves a 3-point game through the key and a 8.5-point game past
    an ordinary integer.
    """
    dist = NormalDist(mu, _BASE_SIGMA)
    lo = math.floor(mu) - 60
    hi = math.ceil(mu) + 60
    raw: dict[int, float] = {}
    for k in range(lo, hi + 1):
        density = dist.cdf(k + 0.5) - dist.cdf(k - 0.5)
        raw[k] = density * _KEY_BOOSTS.get(abs(k), 1.0)
    total = sum(raw.values())
    if total <= 0:
        return {}
    return {k: v / total for k, v in raw.items()}


def _pmf_at(point: int, mu: float) -> float:
    return _pmf(mu).get(point, 0.0)


def p_margin_gt(threshold: float, mu: float) -> float:
    """P(margin > threshold) on the shifted integer margin."""
    if _is_integer_line(threshold):
        start = int(round(threshold)) + 1
    else:
        start = math.floor(threshold) + 1
    total = sum(prob for k, prob in _pmf(mu).items() if k >= start)
    return min(max(total, 0.0), 1.0)


def cover_prob(home_line: float, mu: float, *, conditional_push: bool) -> float:
    """P(home covers `home_line`).

    `conditional_push` is the sportsbook quote: an integer line refunds the push,
    so the number is P(margin clears | margin is not the push). Venue thresholds
    pass conditional_push=False and keep the strict probability.
    """
    threshold = -float(home_line)
    strict = p_margin_gt(threshold, mu)
    if conditional_push and _is_integer_line(home_line):
        push = _pmf_at(int(round(threshold)), mu)
        denom = 1.0 - push
        if denom <= 1e-9:
            return strict
        return min(max(strict / denom, 0.0), 1.0)
    return strict


def translate_cover(prob: float, from_line: float, to_line: float) -> float:
    """Move a de-vigged cover probability from the book line to a strict venue line."""
    if abs(from_line - to_line) <= 1e-9:
        return min(max(float(prob), 0.0), 1.0)
    target = min(max(float(prob), 0.001), 0.999)
    lo, hi = -45.0, 45.0
    for _ in range(60):
        mid = (lo + hi) / 2.0
        got = cover_prob(from_line, mid, conditional_push=True)
        if got < target:
            lo = mid
        else:
            hi = mid
    mu = (lo + hi) / 2.0
    return cover_prob(to_line, mu, conditional_push=False)
