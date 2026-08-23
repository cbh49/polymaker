"""Load sharp-money JSON plays produced by data-aggregation/find_sharp_money.py."""

from __future__ import annotations

import json
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any


@dataclass(frozen=True, slots=True)
class SharpPlay:
    league: str
    matchup: str
    side: str
    market: str
    tier: str
    home_away: str | None
    game_time_utc: str | None
    implied_fair_prob: float | None
    rlm_confirmed: bool
    composite_gap: float | None
    source_path: str
    raw: dict[str, Any]


def load_sharp_file(path: str | Path) -> list[SharpPlay]:
    """Load plays from an mlb/wnba_sharp_money.json file."""
    p = Path(path)
    if not p.is_file():
        raise FileNotFoundError(f"sharp money file not found: {p}")
    data = json.loads(p.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise ValueError(f"expected object in {p}")
    league = str(data.get("league") or "").strip().upper() or _league_from_name(p.name)
    plays_raw = data.get("plays") or []
    if not isinstance(plays_raw, list):
        raise ValueError(f"plays must be a list in {p}")

    out: list[SharpPlay] = []
    for row in plays_raw:
        if not isinstance(row, dict):
            continue
        matchup = str(row.get("matchup") or "").strip()
        side = str(row.get("side") or "").strip()
        market = str(row.get("market") or "moneyline").strip().lower()
        tier = str(row.get("tier") or "").strip().upper()
        if not matchup or not side:
            continue
        fair = row.get("implied_fair_prob")
        gap = row.get("composite_gap")
        out.append(
            SharpPlay(
                league=league,
                matchup=matchup,
                side=side,
                market=market,
                tier=tier or "B",
                home_away=str(row["home_away"]).strip().lower() if row.get("home_away") else None,
                game_time_utc=str(row["game_time_utc"]) if row.get("game_time_utc") else None,
                implied_fair_prob=float(fair) if fair is not None else None,
                rlm_confirmed=bool(row.get("rlm_confirmed")),
                composite_gap=float(gap) if gap is not None else None,
                source_path=str(p),
                raw=row,
            )
        )
    return out


def load_sharp_plays(paths: Sequence[str | Path]) -> list[SharpPlay]:
    plays: list[SharpPlay] = []
    for path in paths:
        plays.extend(load_sharp_file(path))
    return plays


def _league_from_name(name: str) -> str:
    lower = name.lower()
    if "wnba" in lower:
        return "WNBA"
    if "ufc" in lower:
        return "UFC"
    return "MLB"
