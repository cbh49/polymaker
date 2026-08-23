"""
Market registry: the list of Polymarket markets we're watching today.

Populate via `export_watch_list.py` (catalog moneylines + optional sharp
annotations). `load_watch_list` reads the exported JSON.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import List, Optional


@dataclass
class WatchedMarket:
    condition_id: str          # identifies the market (e.g. one game's moneyline)
    league: str                # "MLB" or "WNBA"
    label: str                 # human-readable, e.g. "NYY vs BOS ML"
    yes_token_id: str          # token id for tokens[0] / "yes" side
    no_token_id: str           # token id for tokens[1] / "no" side
    yes_outcome: str = ""      # outcome label for yes token (often a team name)
    no_outcome: str = ""       # outcome label for no token
    slug: str = ""             # polymarket slug when known
    start_time: str = ""       # Gamma event.startTime (ISO UTC), when known
    # optional link back to your own sharp-tracker output for this game
    sharp_tier: Optional[str] = None
    sharp_side: Optional[str] = None
    sharp_composite_gap: Optional[float] = None

    def token_id_for_side(self, side: str) -> str:
        key = side.strip().lower()
        if key in ("yes", "y", "0"):
            return self.yes_token_id
        if key in ("no", "n", "1"):
            return self.no_token_id
        raise ValueError(f"unknown side {side!r}")

    def outcome_for_side(self, side: str) -> str:
        key = side.strip().lower()
        if key in ("yes", "y", "0"):
            return self.yes_outcome or "Yes"
        if key in ("no", "n", "1"):
            return self.no_outcome or "No"
        raise ValueError(f"unknown side {side!r}")

    def side_for_outcome(self, outcome: str) -> Optional[str]:
        """Map a trade outcome label to yes/no using stored outcome names."""
        label = (outcome or "").strip().lower()
        if not label:
            return None
        yes = (self.yes_outcome or "yes").strip().lower()
        no = (self.no_outcome or "no").strip().lower()
        if label in ("yes", "y") or label == yes or label in yes.split() or yes in label:
            return "yes"
        if label in ("no", "n") or label == no or label in no.split() or no in label:
            return "no"
        # nicknames: last word of team name
        yes_nick = yes.rsplit(" ", 1)[-1]
        no_nick = no.rsplit(" ", 1)[-1]
        if label == yes_nick or yes_nick in label.split():
            return "yes"
        if label == no_nick or no_nick in label.split():
            return "no"
        return None


def load_watch_list(path: str) -> List[WatchedMarket]:
    """Load a watch list exported from export_watch_list.py."""
    data = json.loads(Path(path).read_text())
    markets: List[WatchedMarket] = []
    for row in data:
        markets.append(
            WatchedMarket(
                condition_id=str(row["condition_id"]),
                league=str(row["league"]),
                label=str(row["label"]),
                yes_token_id=str(row["yes_token_id"]),
                no_token_id=str(row["no_token_id"]),
                yes_outcome=str(row.get("yes_outcome") or ""),
                no_outcome=str(row.get("no_outcome") or ""),
                slug=str(row.get("slug") or ""),
                start_time=str(row.get("start_time") or row.get("startTime") or ""),
                sharp_tier=row.get("sharp_tier"),
                sharp_side=row.get("sharp_side"),
                sharp_composite_gap=(
                    float(row["sharp_composite_gap"])
                    if row.get("sharp_composite_gap") is not None
                    else None
                ),
            )
        )
    return markets
