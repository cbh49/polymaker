"""Load MLB best plays from llm_best_plays.json."""

from __future__ import annotations

import json
from pathlib import Path

from polymaker.research.schemas import BestPlay, BestPlaysFile


def load_best_plays(path: str | Path) -> BestPlaysFile:
    """Read and normalize ml + ou best plays from the export file."""
    p = Path(path)
    raw = json.loads(p.read_text(encoding="utf-8"))
    ml = [
        BestPlay(
            matchup=row["matchup"],
            time=row.get("time", ""),
            pick=row["pick"],
            bet_type=row.get("bet_type"),
            category="ml",
        )
        for row in raw.get("ml_best_plays", [])
    ]
    ou = [
        BestPlay(
            matchup=row["matchup"],
            time=row.get("time", ""),
            pick=row["pick"],
            bet_type=row.get("bet_type") or "TOTAL",
            category="ou",
        )
        for row in raw.get("ou_best_plays", [])
    ]
    return BestPlaysFile(ml_best_plays=ml, ou_best_plays=ou)


def iter_plays(plays: BestPlaysFile) -> list[BestPlay]:
    """Flat list of all plays (ml then ou)."""
    return list(plays.ml_best_plays) + list(plays.ou_best_plays)
