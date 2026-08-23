"""
Wallet track record store.

Persisted to disk so it accumulates value over time instead of resetting
every run. Win rate here is a placeholder you fill in as markets resolve --
this module does NOT auto-grade wallets (you don't have resolution
callbacks wired in yet). Treat `win_rate` as manually/periodically updated
until you build a resolution-tracking job.
"""

import json
from dataclasses import dataclass, asdict, field
from pathlib import Path
from typing import Dict


@dataclass
class WalletStats:
    address: str
    trade_count: int = 0
    total_volume_usd: float = 0.0
    wins: int = 0
    losses: int = 0
    pending: int = 0
    last_seen_ts: float = 0.0

    @property
    def resolved_count(self) -> int:
        return self.wins + self.losses

    @property
    def win_rate(self) -> float:
        if self.resolved_count == 0:
            return 0.0
        return self.wins / self.resolved_count


class WalletStore:
    def __init__(self, path: str = "wallet_stats.json"):
        self.path = Path(path)
        self._wallets: Dict[str, WalletStats] = {}
        self._load()

    def _load(self):
        if self.path.exists():
            raw = json.loads(self.path.read_text())
            self._wallets = {addr: WalletStats(**row) for addr, row in raw.items()}

    def save(self):
        raw = {addr: asdict(stats) for addr, stats in self._wallets.items()}
        self.path.write_text(json.dumps(raw, indent=2))

    def record_trade(self, address: str, size_usd: float, ts: float):
        w = self._wallets.setdefault(address, WalletStats(address=address))
        w.trade_count += 1
        w.total_volume_usd += size_usd
        w.pending += 1
        w.last_seen_ts = ts

    def get(self, address: str) -> WalletStats:
        return self._wallets.get(address, WalletStats(address=address))

    def is_smart_money(self, address: str, min_trades: int, min_win_rate: float) -> bool:
        w = self.get(address)
        return w.resolved_count >= min_trades and w.win_rate >= min_win_rate
