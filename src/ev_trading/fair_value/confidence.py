"""0–1 confidence from book count, fit quality, venue liquidity, and line delta."""

from __future__ import annotations

import math

from ev_trading.fair_value.config import FairValueConfig


def _clamp01(value: float) -> float:
    return min(max(float(value), 0.0), 1.0)


def books_score(n_books: int, *, target: int) -> float:
    if target <= 0:
        return 1.0
    return _clamp01(n_books / float(target))


def fit_quality_score(r2: float | None, *, agreement_std: float | None = None) -> float:
    if r2 is not None:
        return _clamp01(r2)
    if agreement_std is None:
        return 0.5
    # Same-strike: tighter book agreement → higher quality.
    return _clamp01(1.0 - agreement_std / 0.10)


def liquidity_score(
    *,
    volume: float | None,
    liquidity: float | None,
    volume_24hr: float | None,
    cfg: FairValueConfig,
) -> float:
    observed = [v for v in (volume, liquidity, volume_24hr) if v is not None and v > 0]
    if not observed:
        return 0.0
    value = max(observed)
    floor = max(min(cfg.min_liquidity, cfg.min_volume), 1.0)
    full = max(cfg.liquidity_full_scale, floor * 2.0)
    if value < floor:
        return 0.5 * value / floor
    span = math.log(full) - math.log(floor)
    if span <= 0:
        return 1.0
    return _clamp01(0.5 + 0.5 * (math.log(value) - math.log(floor)) / span)


def line_delta_score(
    line_delta: float | None,
    *,
    volume: float | None,
    liquidity: float | None,
    cfg: FairValueConfig,
    extrapolation_distance: float | None = None,
    book_line_min: float | None = None,
) -> float:
    delta = abs(float(line_delta)) if line_delta is not None else 0.0
    extra = max(float(extrapolation_distance or 0.0), 0.0)
    if extra > 0:
        delta = max(delta, extra)
        # Relative extra: 7 yards off a 7.5-yard TE main should crush
        # confidence; 6 yards off a 213-yard passing main should not.
        rel = extra / max(abs(book_line_min) if book_line_min is not None else 1.0, 1.0)
        delta += rel * max(cfg.line_delta_scale, 1e-6)
    scale = max(cfg.line_delta_scale, 1e-6)
    base = 1.0 / (1.0 + delta / scale)
    depth = max(v or 0.0 for v in (volume, liquidity)) if (volume or liquidity) else 0.0
    if depth <= 0 and delta >= cfg.stale_delta:
        base *= 0.5
    return _clamp01(base)


def is_low_liquidity(
    *,
    volume: float | None,
    liquidity: float | None,
    volume_24hr: float | None,
    cfg: FairValueConfig,
) -> bool:
    liq_ok = liquidity is not None and liquidity >= cfg.min_liquidity
    vol_ok = (volume is not None and volume >= cfg.min_volume) or (
        volume_24hr is not None and volume_24hr >= cfg.min_volume
    )
    if liquidity is None and volume is None and volume_24hr is None:
        return True
    return not (liq_ok or vol_ok)


def confidence_score(
    *,
    n_books: int,
    r2: float | None = None,
    agreement_std: float | None = None,
    volume: float | None = None,
    liquidity: float | None = None,
    volume_24hr: float | None = None,
    line_delta: float | None = None,
    extrapolation_distance: float | None = None,
    book_line_min: float | None = None,
    cfg: FairValueConfig | None = None,
) -> float:
    """0–1 score. No hidden floor — a garbage extrapolation can be 0."""
    cfg = cfg or FairValueConfig()
    w = cfg.confidence_weights
    extra_score = line_delta_score(
        line_delta,
        volume=volume,
        liquidity=liquidity,
        cfg=cfg,
        extrapolation_distance=extrapolation_distance,
        book_line_min=book_line_min,
    )
    parts = {
        "n_books": books_score(n_books, target=cfg.target_book_count),
        "fit_quality": fit_quality_score(r2, agreement_std=agreement_std),
        "liquidity": liquidity_score(
            volume=volume, liquidity=liquidity, volume_24hr=volume_24hr, cfg=cfg
        ),
        "line_delta": extra_score,
        "extrapolation": extra_score,
    }
    extra_w = float(w.extrapolation)
    total_w = w.n_books + w.fit_quality + w.liquidity + w.line_delta + extra_w
    if total_w <= 0:
        return 0.0
    score = (
        w.n_books * parts["n_books"]
        + w.fit_quality * parts["fit_quality"]
        + w.liquidity * parts["liquidity"]
        + w.line_delta * parts["line_delta"]
        + extra_w * parts["extrapolation"]
    ) / total_w
    return _clamp01(score)
