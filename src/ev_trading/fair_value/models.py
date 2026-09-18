"""Shared types for the sportsbook-consensus → tradable-venue pipeline."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any, Literal

DistributionType = Literal["normal", "poisson", "nbinom"]
DevigMethod = Literal["multiplicative", "shin"]
VenueName = Literal["kalshi", "polymarket"]
SideName = Literal["yes", "no", "over", "under", "home", "away"]
SigmaSource = Literal["fitted", "fallback"]
VetoReason = Literal[
    "fit_quality_too_low",
    "fallback_sigma_extrapolation",
    "insufficient_books",
]


@dataclass(frozen=True, slots=True)
class BookPoint:
    """One sportsbook's (line, fair over-probability) after devigging."""

    book: str
    line: float
    fair_over: float
    weight: float
    raw_over: float | None = None
    raw_under: float | None = None


@dataclass(frozen=True, slots=True)
class FittedDistribution:
    mu: float
    sigma: float
    distribution_type: DistributionType
    r2: float | None
    n_books: int
    low_confidence: bool
    sigma_source: SigmaSource = "fitted"
    role_bucket: str | None = None
    extra: dict[str, float] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        row = asdict(self)
        return row


@dataclass(frozen=True, slots=True)
class SameStrikeConsensus:
    fair_prob: float
    line: float | None
    n_books: int
    std: float
    low_confidence: bool

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True, slots=True)
class PricedOpportunity:
    """One tradable (Kalshi/Polymarket) side vs sportsbook-implied fair value."""

    market: str
    matchup: str
    player: str | None
    stat: str
    venue: VenueName
    side: str
    fair_prob: float
    fair_line: float | None
    market_line: float | None
    market_price: float
    raw_edge: float
    fee_adjusted_edge: float
    expected_value_per_contract: float
    confidence: float
    volume: float | None
    liquidity: float | None
    volume_24hr: float | None
    line_delta: float | None
    low_liquidity: bool
    low_confidence: bool
    n_books: int
    fit_r2: float | None
    market_id: str | None = None
    ticker: str | None = None
    rank_score: float = 0.0
    is_extrapolation: bool | None = None
    extrapolation_distance: float | None = None
    sigma_source: SigmaSource | None = None
    role_bucket: str | None = None
    veto_reason: str | None = None

    @property
    def edge_pct(self) -> float:
        return self.fee_adjusted_edge * 100.0

    def to_dict(self) -> dict[str, Any]:
        row = asdict(self)
        row["edge_pct"] = round(self.edge_pct, 3)
        return row


@dataclass(frozen=True, slots=True)
class InformationalRow:
    """Sportsbook-vs-consensus gap. Not tradable on this stack."""

    market: str
    matchup: str
    player: str | None
    stat: str
    book: str
    book_line: float | None
    book_prob: float
    fair_prob: float
    raw_edge: float
    n_books: int
    actionable: bool = False

    @property
    def edge_pct(self) -> float:
        return self.raw_edge * 100.0

    def to_dict(self) -> dict[str, Any]:
        row = asdict(self)
        row["edge_pct"] = round(self.edge_pct, 3)
        row["actionable"] = False
        return row
