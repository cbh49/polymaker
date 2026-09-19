"""Sportsbook consensus: same-strike average or inverse-CDF distribution fit."""

from __future__ import annotations

import math
from collections.abc import Sequence
from statistics import NormalDist

from ev_trading.fair_value.config import FairValueConfig
from ev_trading.fair_value.models import (
    BookPoint,
    DistributionType,
    FittedDistribution,
    SameStrikeConsensus,
    SigmaSource,
)

_STD_NORMAL = NormalDist()


def clip_prob(p: float, eps: float = 0.001) -> float:
    return min(max(float(p), eps), 1.0 - eps)


def inverse_cdf_z(prob_under: float, *, eps: float = 0.001) -> float:
    """Standard-normal z such that Φ(z) = P(under) = 1 - P(over)."""
    return _STD_NORMAL.inv_cdf(clip_prob(prob_under, eps))


def _weighted_mean(values: Sequence[float], weights: Sequence[float]) -> float:
    wsum = sum(weights)
    if wsum <= 0:
        raise ValueError("weights must sum to a positive number")
    return sum(v * w for v, w in zip(values, weights, strict=True)) / wsum


def _weighted_linreg(
    xs: Sequence[float],
    ys: Sequence[float],
    weights: Sequence[float],
) -> tuple[float, float, float]:
    """WLS: y = intercept + slope * x. Returns intercept, slope, R²."""
    wsum = sum(weights)
    if wsum <= 0:
        raise ValueError("weights must sum to a positive number")
    mx = sum(w * x for w, x in zip(weights, xs, strict=True)) / wsum
    my = sum(w * y for w, y in zip(weights, ys, strict=True)) / wsum
    varx = sum(w * (x - mx) ** 2 for w, x in zip(weights, xs, strict=True))
    cov = sum(w * (x - mx) * (y - my) for w, x, y in zip(weights, xs, ys, strict=True))
    if varx <= 1e-18:
        return my, 0.0, 0.0
    slope = cov / varx
    intercept = my - slope * mx
    ss_tot = sum(w * (y - my) ** 2 for w, y in zip(weights, ys, strict=True))
    ss_res = sum(
        w * (y - (intercept + slope * x)) ** 2 for w, x, y in zip(weights, xs, ys, strict=True)
    )
    r2 = 1.0 - ss_res / ss_tot if ss_tot > 1e-18 else 1.0
    return intercept, slope, r2


def lines_are_same_strike(lines: Sequence[float], tolerance: float) -> bool:
    if len(lines) <= 1:
        return True
    return max(lines) - min(lines) <= tolerance


def strike_range(points: Sequence[BookPoint]) -> tuple[float, float] | None:
    lines = [p.line for p in points]
    if not lines:
        return None
    return min(lines), max(lines)


def extrapolation_for(market_line: float | None, points: Sequence[BookPoint]) -> tuple[bool, float]:
    """Whether `market_line` sits outside the observed book strikes.

    Interpolation vs extrapolation is the difference between pricing a
    number the books actually quoted and walking a curve past its data.
    """
    if market_line is None:
        return False, 0.0
    rng = strike_range(points)
    if rng is None:
        return False, 0.0
    lo, hi = rng
    if lo <= market_line <= hi:
        return False, 0.0
    return True, max(lo - market_line, market_line - hi)


def same_strike_consensus(
    points: Sequence[BookPoint],
    *,
    cfg: FairValueConfig | None = None,
) -> SameStrikeConsensus:
    cfg = cfg or FairValueConfig()
    if not points:
        raise ValueError("need at least one book to build a consensus")
    probs = [p.fair_over for p in points]
    weights = [p.weight for p in points]
    fair = _weighted_mean(probs, weights)
    mean = _weighted_mean(probs, weights)
    var = _weighted_mean([(p - mean) ** 2 for p in probs], weights)
    std = math.sqrt(max(var, 0.0))
    line = _weighted_mean([p.line for p in points], weights)
    return SameStrikeConsensus(
        fair_prob=min(max(fair, 0.0), 1.0),
        line=line,
        n_books=len(points),
        std=std,
        low_confidence=len(points) < cfg.min_books_for_fit,
    )


def poisson_pmf(k: int, lam: float) -> float:
    if k < 0 or lam < 0:
        return 0.0
    if lam == 0:
        return 1.0 if k == 0 else 0.0
    logp = -lam + k * math.log(lam) - math.lgamma(k + 1)
    return math.exp(logp)


def poisson_cdf(k: int, lam: float) -> float:
    """P(X <= k) for X ~ Poisson(λ)."""
    if k < 0:
        return 0.0
    acc = 0.0
    for i in range(k + 1):
        acc += poisson_pmf(i, lam)
        if acc >= 1.0:
            return 1.0
    return min(acc, 1.0)


def nbinom_pmf(k: int, r: float, mu: float) -> float:
    """NB2: mean μ, dispersion r (failures before r successes)."""
    if k < 0 or r <= 0 or mu < 0:
        return 0.0
    if mu == 0:
        return 1.0 if k == 0 else 0.0
    p = r / (r + mu)
    p = min(max(p, 1e-12), 1.0 - 1e-12)
    logp = (
        math.lgamma(k + r)
        - math.lgamma(k + 1)
        - math.lgamma(r)
        + r * math.log(p)
        + k * math.log(1.0 - p)
    )
    return math.exp(logp)


def nbinom_cdf(k: int, r: float, mu: float) -> float:
    if k < 0:
        return 0.0
    acc = 0.0
    for i in range(k + 1):
        acc += nbinom_pmf(i, r, mu)
        if acc >= 1.0:
            return 1.0
    return min(acc, 1.0)


def _poisson_lambda_from_over(line: float, p_over: float) -> float:
    """Solve 1 - F_poisson(floor(line); λ) = p_over."""
    target = clip_prob(p_over, 1e-9)
    k = math.floor(line)
    lo, hi = 1e-8, max(abs(line) * 8.0, 8.0)
    while 1.0 - poisson_cdf(int(k), hi) < target:
        hi *= 2.0
        if hi > 1e6:
            break
    for _ in range(70):
        mid = 0.5 * (lo + hi)
        p = 1.0 - poisson_cdf(int(k), mid)
        if p > target:
            hi = mid
        else:
            lo = mid
    return 0.5 * (lo + hi)


def shift_over_prob(
    line: float,
    p_over: float,
    target_line: float,
    fit: FittedDistribution,
    *,
    tolerance: float | None = None,
) -> float:
    """Walk one book's de-vigged P(over) from `line` to `target_line`.

    Same-strike (within tolerance) is identity. Normal and nbinom reuse
    ``fit.sigma`` and keep this book's implied p at its own line. Poisson
    inverts this book's (line, p_over) to λ, then evaluates at the target.
    """
    src = float(line)
    dst = float(target_line)
    p = min(max(float(p_over), 0.0), 1.0)
    tol = FairValueConfig().same_strike_line_tolerance if tolerance is None else float(tolerance)
    if abs(src - dst) <= tol:
        return p
    kind = str(fit.distribution_type)
    if kind in {"normal", "nbinom"}:
        z = inverse_cdf_z(1.0 - p)
        mu = src - fit.sigma * z
        return fair_prob_at_line(mu, fit.sigma, dst, "normal")
    if kind == "poisson":
        lam = _poisson_lambda_from_over(src, p)
        return fair_prob_at_line(lam, fit.sigma, dst, "poisson")
    raise ValueError(f"unknown distribution_type: {kind}")


def fair_prob_at_line(
    mu: float,
    sigma: float,
    target_line: float,
    distribution_type: DistributionType | str = "normal",
    *,
    extra: dict[str, float] | None = None,
) -> float:
    """Fair P(over) = P(X > target_line) under the fitted distribution."""
    kind = str(distribution_type)
    if kind == "normal":
        if sigma <= 0:
            raise ValueError("sigma must be positive for a Normal distribution")
        z = (target_line - mu) / sigma
        return 1.0 - _STD_NORMAL.cdf(z)
    if kind == "poisson":
        lam = max(mu, 1e-12)
        k = math.floor(target_line)
        return 1.0 - poisson_cdf(int(k), lam)
    if kind == "nbinom":
        r = None if extra is None else extra.get("r")
        if r is None:
            var = sigma * sigma
            if var <= mu + 1e-9:
                return fair_prob_at_line(mu, sigma, target_line, "poisson")
            r = (mu * mu) / (var - mu)
        r = max(float(r), 1e-6)
        k = math.floor(target_line)
        return 1.0 - nbinom_cdf(int(k), r, max(mu, 0.0))
    raise ValueError(f"unknown distribution_type: {distribution_type}")


def _low_confidence(
    n_books: int,
    r2: float | None,
    cfg: FairValueConfig,
    *,
    sigma_source: SigmaSource,
) -> bool:
    if n_books < cfg.min_books_for_fit:
        return True
    if r2 is not None and r2 < cfg.min_r2:
        return True
    return sigma_source != "fitted"


def fit_normal(
    points: Sequence[BookPoint],
    *,
    stat: str,
    cfg: FairValueConfig | None = None,
) -> FittedDistribution:
    """Regress line = μ + σ z, z = Φ^{-1}(P(under)).

    Main-line quotes are often near 50/50 after devig, so σ is weakly identified.
    Out-of-bounds or low z-variance falls back to a μ-scaled default σ, keeping
    the observed P(over) at the mean book line.
    """
    cfg = cfg or FairValueConfig()
    if not points:
        raise ValueError("need at least one book to fit a distribution")
    eps = cfg.prob_clip
    zs = [inverse_cdf_z(1.0 - p.fair_over, eps=eps) for p in points]
    lines = [p.line for p in points]
    weights = [p.weight for p in points]
    mu_lines = _weighted_mean(lines, weights)
    z_mean = _weighted_mean(zs, weights)
    z_var = _weighted_mean([(z - z_mean) ** 2 for z in zs], weights)
    intercept, slope, r2 = _weighted_linreg(zs, lines, weights)
    lo, hi = cfg.sigma_bounds_for(stat, mu_lines)
    bucket_name, _spec = cfg.role_bucket_for(stat, mu_lines)
    sigma_source: SigmaSource = "fitted"
    mu, sigma = intercept, slope
    if z_var < cfg.min_z_variance or sigma <= 0 or not (lo <= sigma <= hi):
        # One-strike (or out-of-bounds) identification: keep the observed
        # P(over) at the mean line, but use this market's usage-bucket σ
        # rather than a global-per-stat prior.
        sigma = cfg.scaled_default_sigma(stat, mu_lines)
        mu = mu_lines - sigma * z_mean
        sigma_source = "fallback"
    n = len(points)
    return FittedDistribution(
        mu=mu,
        sigma=sigma,
        distribution_type="normal",
        r2=r2,
        n_books=n,
        low_confidence=_low_confidence(n, r2, cfg, sigma_source=sigma_source),
        sigma_source=sigma_source,
        role_bucket=bucket_name,
    )


def fit_poisson(
    points: Sequence[BookPoint],
    *,
    cfg: FairValueConfig | None = None,
) -> FittedDistribution:
    cfg = cfg or FairValueConfig()
    if not points:
        raise ValueError("need at least one book to fit a distribution")
    lams = [_poisson_lambda_from_over(p.line, p.fair_over) for p in points]
    weights = [p.weight for p in points]
    mu = _weighted_mean(lams, weights)
    sigma = math.sqrt(max(mu, 0.0))
    n = len(points)
    return FittedDistribution(
        mu=mu,
        sigma=max(sigma, 1e-6),
        distribution_type="poisson",
        r2=None,
        n_books=n,
        low_confidence=n < cfg.min_books_for_fit,
        sigma_source="fitted",
        extra={"lambda": mu},
    )


def fit_nbinom(
    points: Sequence[BookPoint],
    *,
    stat: str,
    cfg: FairValueConfig | None = None,
) -> FittedDistribution:
    """Normal inverse-CDF for (μ, σ), then map to NB2 if σ² > μ, else Poisson."""
    cfg = cfg or FairValueConfig()
    normal = fit_normal(points, stat=stat, cfg=cfg)
    mu = max(normal.mu, 1e-6)
    var = normal.sigma * normal.sigma
    if var <= mu + 1e-9:
        pois = fit_poisson(points, cfg=cfg)
        return FittedDistribution(
            mu=pois.mu,
            sigma=pois.sigma,
            distribution_type="poisson",
            r2=normal.r2,
            n_books=normal.n_books,
            low_confidence=normal.low_confidence or pois.low_confidence,
            sigma_source=normal.sigma_source,
            role_bucket=normal.role_bucket,
            extra={"lambda": pois.mu},
        )
    r = (mu * mu) / (var - mu)
    return FittedDistribution(
        mu=mu,
        sigma=normal.sigma,
        distribution_type="nbinom",
        r2=normal.r2,
        n_books=normal.n_books,
        low_confidence=normal.low_confidence,
        sigma_source=normal.sigma_source,
        role_bucket=normal.role_bucket,
        extra={"r": r},
    )


def fit_distribution(
    points: Sequence[BookPoint],
    distribution_type: DistributionType | str,
    *,
    stat: str,
    cfg: FairValueConfig | None = None,
) -> FittedDistribution:
    kind = str(distribution_type)
    if kind == "normal":
        return fit_normal(points, stat=stat, cfg=cfg)
    if kind == "poisson":
        return fit_poisson(points, cfg=cfg)
    if kind == "nbinom":
        return fit_nbinom(points, stat=stat, cfg=cfg)
    raise ValueError(f"unknown distribution_type: {distribution_type}")


def fair_over_from_fit(fit: FittedDistribution, target_line: float) -> float:
    return fair_prob_at_line(
        fit.mu,
        fit.sigma,
        target_line,
        fit.distribution_type,
        extra=fit.extra,
    )
