"""
Rolling baseline tracker: keeps a trailing window of trade sizes per market
so we can compute a z-score for "is this trade/volume burst unusual for
THIS market" rather than using one global threshold across illiquid WNBA
props and high-volume MLB moneylines alike.
"""

import statistics
from collections import deque, defaultdict
from dataclasses import dataclass


@dataclass
class ZScoreResult:
    z: float
    mean: float
    stdev: float
    n: int


class MarketBaseline:
    def __init__(self, window_size: int = 20):
        self.window_size = window_size
        self._sizes: dict[str, deque] = defaultdict(lambda: deque(maxlen=window_size))

    def add_trade(self, condition_id: str, size_usd: float):
        self._sizes[condition_id].append(size_usd)

    def zscore(self, condition_id: str, size_usd: float) -> ZScoreResult:
        window = self._sizes[condition_id]
        if len(window) < 5:
            # not enough history yet to say what's "normal" for this market
            return ZScoreResult(z=0.0, mean=0.0, stdev=0.0, n=len(window))

        mean = statistics.mean(window)
        stdev = statistics.pstdev(window) or 1e-6
        z = (size_usd - mean) / stdev
        return ZScoreResult(z=z, mean=mean, stdev=stdev, n=len(window))
