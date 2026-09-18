"""Fair-value knobs: JSON overlay on top of in-code defaults."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

DevigMethod = Literal["multiplicative", "shin"]


class KalshiFeeConfig(BaseModel):
    model_config = ConfigDict(extra="ignore")

    taker_rate: float = 0.07
    multiplier: float = 1.0
    contracts: int = 1


class PolymarketFeeConfig(BaseModel):
    model_config = ConfigDict(extra="ignore")

    taker_bps: float = 0.0
    gas_usd: float = 0.02
    assumed_contracts: int = 10


class ConfidenceWeights(BaseModel):
    model_config = ConfigDict(extra="ignore")

    n_books: float = 0.30
    fit_quality: float = 0.25
    liquidity: float = 0.30
    line_delta: float = 0.15
    extrapolation: float = 0.0


class RoleBucketSpec(BaseModel):
    """Fallback σ (and optional bounds) for one usage bucket of a stat.

    v1 heuristic: bucket by this market's own consensus line, not position
    or snap share. Real target/snap data would be a stronger role signal.
    """

    model_config = ConfigDict(extra="ignore")

    line_lt: float
    sigma: float
    sigma_bounds: tuple[float, float] | None = None


class FairValueConfig(BaseModel):
    """Pipeline config. Unknown JSON keys are ignored so overlays stay additive."""

    model_config = ConfigDict(extra="ignore")

    devig_method: DevigMethod = "multiplicative"
    book_weights: dict[str, float] = Field(
        default_factory=lambda: {
            "circasports": 1.5,
            "circa": 1.5,
            "pinnacle": 2.0,
            "draftkings": 1.0,
            "fanduel": 1.0,
            "mgm": 1.0,
            "caesars": 1.0,
            "betrivers": 1.0,
            "betr": 1.0,
            "fanatics": 1.0,
            "hardrock": 1.0,
            "thescore": 1.0,
            "default": 1.0,
        }
    )
    stat_distributions: dict[str, str] = Field(
        default_factory=lambda: {
            "default": "normal",
            "moneyline": "same_strike",
            "anytime_td": "same_strike",
            "first_td": "same_strike",
            "2plus_td": "same_strike",
            "spread": "normal",
            "total": "normal",
            "passing_yards": "normal",
            "rushing_yards": "normal",
            "receiving_yards": "normal",
            "rushing_receiving_yards": "normal",
            "receptions": "nbinom",
            "interceptions": "poisson",
            "passing_tds": "poisson",
            "rushing_tds": "poisson",
            "receiving_tds": "poisson",
        }
    )
    default_sigma: dict[str, float] = Field(
        default_factory=lambda: {
            "default": 20.0,
            "spread": 13.5,
            "total": 13.5,
            "passing_yards": 62.0,
            "rushing_yards": 32.0,
            "receiving_yards": 28.0,
            "rushing_receiving_yards": 38.0,
            "receptions": 2.2,
            "interceptions": 1.1,
            "passing_tds": 1.1,
            "rushing_tds": 0.8,
            "receiving_tds": 0.8,
        }
    )
    # Usage-bucketed fallback σ. Replaces the old typical_line ratio hack:
    # a 0.5-yard QB rush and a 100-yard RB no longer share one prior.
    role_buckets: dict[str, dict[str, RoleBucketSpec]] = Field(
        default_factory=lambda: {
            "receiving_yards": {
                "low_target_share": RoleBucketSpec(
                    line_lt=20, sigma=10, sigma_bounds=(4.0, 16.0)
                ),
                "moderate": RoleBucketSpec(line_lt=50, sigma=18, sigma_bounds=(8.0, 28.0)),
                "high_target_share": RoleBucketSpec(
                    line_lt=999, sigma=28, sigma_bounds=(12.0, 50.0)
                ),
            },
            "rushing_yards": {
                "low_volume": RoleBucketSpec(line_lt=5, sigma=6, sigma_bounds=(2.0, 10.0)),
                "committee": RoleBucketSpec(line_lt=40, sigma=18, sigma_bounds=(8.0, 28.0)),
                "workhorse": RoleBucketSpec(line_lt=999, sigma=32, sigma_bounds=(15.0, 55.0)),
            },
            "passing_yards": {
                "backup": RoleBucketSpec(line_lt=150, sigma=40, sigma_bounds=(20.0, 55.0)),
                "starter": RoleBucketSpec(line_lt=260, sigma=55, sigma_bounds=(35.0, 80.0)),
                "high_volume": RoleBucketSpec(
                    line_lt=999, sigma=62, sigma_bounds=(35.0, 90.0)
                ),
            },
            "rushing_receiving_yards": {
                "low_volume": RoleBucketSpec(line_lt=20, sigma=12, sigma_bounds=(5.0, 20.0)),
                "committee": RoleBucketSpec(line_lt=70, sigma=24, sigma_bounds=(12.0, 40.0)),
                "workhorse": RoleBucketSpec(line_lt=999, sigma=38, sigma_bounds=(18.0, 60.0)),
            },
            "receptions": {
                "low_volume": RoleBucketSpec(line_lt=2.5, sigma=1.0, sigma_bounds=(0.5, 1.8)),
                "moderate": RoleBucketSpec(line_lt=6.0, sigma=1.6, sigma_bounds=(0.8, 3.0)),
                "high_volume": RoleBucketSpec(
                    line_lt=999, sigma=2.2, sigma_bounds=(1.0, 6.0)
                ),
            },
        }
    )
    min_r2_for_fitted_sigma: float = 0.4
    # 0.4 (not 0.5) so Chase 50+ vs ~85.5 (~41% relative) is vetoed while
    # a 225+ passing alt vs books at 211–218 (~3%) still passes.
    max_fallback_extrapolation_ratio: float = 0.4
    min_books_hard_floor: int = 3
    # Skip the worse O/U side when |Δline|/max(|fair_line|, 1) exceeds this
    # and σ was not identified from multiple book strikes (long-shot alts).
    unfitted_max_rel_delta: float = 1.0
    sigma_bounds: dict[str, tuple[float, float]] = Field(
        default_factory=lambda: {
            "default": (5.0, 120.0),
            "spread": (8.0, 20.0),
            "total": (8.0, 22.0),
            "passing_yards": (35.0, 90.0),
            "rushing_yards": (15.0, 55.0),
            "receiving_yards": (12.0, 50.0),
            "rushing_receiving_yards": (18.0, 60.0),
            "receptions": (1.0, 6.0),
        }
    )
    min_books_for_fit: int = 4
    min_r2: float = 0.75
    min_z_variance: float = 0.02
    same_strike_line_tolerance: float = 0.26
    one_sided_overround: float = 1.05
    prob_clip: float = 0.001
    kalshi_fee: KalshiFeeConfig = Field(default_factory=KalshiFeeConfig)
    polymarket_fee: PolymarketFeeConfig = Field(default_factory=PolymarketFeeConfig)
    min_liquidity: float = 100.0
    min_volume: float = 50.0
    liquidity_full_scale: float = 5000.0
    min_edge_pct: float = 1.0
    line_delta_scale: float = 15.0
    stale_delta: float = 8.0
    target_book_count: int = 8
    confidence_weights: ConfidenceWeights = Field(default_factory=ConfidenceWeights)

    def book_weight(self, book: str) -> float:
        key = (book or "").strip().lower()
        if key in self.book_weights:
            return float(self.book_weights[key])
        return float(self.book_weights.get("default", 1.0))

    def distribution_for(self, stat: str) -> str:
        key = (stat or "").strip().lower()
        if key in self.stat_distributions:
            return self.stat_distributions[key]
        return self.stat_distributions.get("default", "normal")

    def default_sigma_for(self, stat: str) -> float:
        key = (stat or "").strip().lower()
        if key in self.default_sigma:
            return float(self.default_sigma[key])
        return float(self.default_sigma.get("default", 20.0))

    def role_bucket_for(
        self, stat: str, consensus_line: float | None
    ) -> tuple[str | None, RoleBucketSpec | None]:
        """Pick a usage bucket from this market's own consensus line.

        v1 heuristic only — consensus_line is a usage proxy (a 7.5-yard TE
        market is not a WR1). Real target/snap share would replace this.
        """
        key = (stat or "").strip().lower()
        buckets = self.role_buckets.get(key)
        if not buckets:
            return None, None
        line = abs(float(consensus_line)) if consensus_line is not None else 0.0
        ordered = sorted(buckets.items(), key=lambda item: item[1].line_lt)
        for name, spec in ordered:
            if line < spec.line_lt:
                return name, spec
        if ordered:
            return ordered[-1]
        return None, None

    def scaled_default_sigma(self, stat: str, mu: float) -> float:
        """Fallback residual SD for this market's usage bucket.

        Spreads/totals have no buckets and keep the flat game residual.
        """
        _name, spec = self.role_bucket_for(stat, mu)
        if spec is not None:
            return float(spec.sigma)
        return self.default_sigma_for(stat)

    def sigma_bounds_for(
        self, stat: str, mu: float | None = None
    ) -> tuple[float, float]:
        if mu is not None:
            _name, spec = self.role_bucket_for(stat, mu)
            if spec is not None and spec.sigma_bounds is not None:
                lo, hi = spec.sigma_bounds
                return float(lo), float(hi)
        key = (stat or "").strip().lower()
        if key in self.sigma_bounds:
            lo, hi = self.sigma_bounds[key]
            return float(lo), float(hi)
        lo, hi = self.sigma_bounds.get("default", (5.0, 120.0))
        return float(lo), float(hi)

    @classmethod
    def load(cls, path: str | Path | None = None) -> FairValueConfig:
        overlay = _read_json(_resolve_config_path(path))
        return cls.model_validate(overlay) if overlay else cls()


def _resolve_config_path(path: str | Path | None) -> Path | None:
    if path is not None:
        return Path(path)
    here = Path("config") / "fair_value.json"
    if here.exists():
        return here
    packaged = Path(__file__).resolve().parents[3] / "config" / "fair_value.json"
    if packaged.exists():
        return packaged
    return None


def _read_json(path: Path | None) -> dict[str, Any]:
    if path is None or not path.exists():
        return {}
    raw = json.loads(path.read_text(encoding="utf-8"))
    return raw if isinstance(raw, dict) else {}
