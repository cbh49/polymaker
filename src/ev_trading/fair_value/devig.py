"""Two-way vig removal: multiplicative (default) and Shin.

Inputs may be implied probabilities in (0, 1] or American odds (abs > 1).
"""

from __future__ import annotations

import math
from collections.abc import Sequence
from typing import Literal

DevigMethod = Literal["multiplicative", "shin"]


def american_to_prob(odds: float) -> float:
    """American odds → raw implied probability (with vig still in it)."""
    if odds >= 0:
        return 100.0 / (odds + 100.0)
    return abs(odds) / (abs(odds) + 100.0)


def prob_to_american(prob: float) -> int:
    """Implied probability → American odds (rounded to a whole number)."""
    p = min(max(float(prob), 1e-6), 1.0 - 1e-6)
    if p >= 0.5:
        return int(round(-100.0 * p / (1.0 - p)))
    return int(round(100.0 * (1.0 - p) / p))


def as_implied_prob(value: float) -> float:
    """Treat |x| <= 1 as a probability, otherwise as American odds."""
    if -1.0 <= value <= 1.0:
        if value <= 0.0:
            raise ValueError(f"probability must be in (0, 1], got {value}")
        return float(value)
    return american_to_prob(float(value))


def multiplicative_devig(prob_a: float, prob_b: float) -> tuple[float, float]:
    """Normalize two implied probabilities so they sum to 1."""
    if prob_a <= 0 or prob_b <= 0:
        raise ValueError("implied probabilities must be positive")
    total = prob_a + prob_b
    return prob_a / total, prob_b / total


def shin_devig(probs: Sequence[float], *, tol: float = 1e-12) -> list[float]:
    """Shin (1993) inversion: better than multiplicative on skewed two-way lines.

    Solves for insider-trading share z so the inverted probabilities sum to 1.
    At z=0 this is not quite multiplicative; z is identified from the overround.
    """
    implied = [float(p) for p in probs]
    if any(p <= 0 for p in implied):
        raise ValueError("implied probabilities must be positive")
    total = sum(implied)
    if total <= 0:
        raise ValueError("implied probabilities must sum to a positive number")
    if abs(total - 1.0) <= 1e-12:
        return implied

    def inverted(z: float) -> list[float]:
        z_use = min(max(z, 0.0), 1.0 - 1e-12)
        denom = 2.0 * (1.0 - z_use)
        out: list[float] = []
        for pi in implied:
            inner = z_use * z_use + 4.0 * (1.0 - z_use) * (pi * pi) / total
            out.append((math.sqrt(max(inner, 0.0)) - z_use) / denom)
        return out

    def error(z: float) -> float:
        return sum(inverted(z)) - 1.0

    # z=0 → sum = sqrt(overround) > 1 when there is vig; raise z until sum=1.
    if error(0.0) <= 0:
        return [p / total for p in implied]

    lo, hi = 0.0, 1.0 - 1e-9
    if error(hi) > 0:
        return [p / total for p in implied]

    for _ in range(80):
        mid = (lo + hi) / 2.0
        if error(mid) > 0:
            lo = mid
        else:
            hi = mid
        if hi - lo < tol:
            break

    fair = inverted((lo + hi) / 2.0)
    s = sum(fair)
    if s <= 0:
        return [p / total for p in implied]
    return [p / s for p in fair]


def shin_devig_two_way(prob_a: float, prob_b: float) -> tuple[float, float]:
    a, b = shin_devig([prob_a, prob_b])
    return a, b


def devig_two_way(
    side_a: float,
    side_b: float,
    *,
    method: DevigMethod = "multiplicative",
) -> tuple[float, float]:
    """Devig a two-outcome market. Accepts implied probs or American odds."""
    pa = as_implied_prob(side_a)
    pb = as_implied_prob(side_b)
    if method == "shin":
        return shin_devig_two_way(pa, pb)
    if method == "multiplicative":
        return multiplicative_devig(pa, pb)
    raise ValueError(f"unknown devig method: {method}")


def devig_one_sided(implied: float, *, overround: float = 1.05) -> float:
    """Haircut a yes-only quote (anytime TD, first TD) by an assumed overround."""
    p = as_implied_prob(implied)
    if overround <= 1.0:
        return min(max(p, 0.0), 1.0)
    other = max(overround - p, 1e-12)
    return p / (p + other)
