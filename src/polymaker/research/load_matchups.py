"""Load MLB matchups from matchups.json."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from polymaker.research.schemas import Matchup, MatchupLines


def _str(row: dict[str, Any], key: str, default: str = "") -> str:
    val = row.get(key)
    if val is None:
        return default
    return str(val).strip()


def matchup_from_row(row: dict[str, Any]) -> Matchup:
    """Map a raw matchups.json object (Team1=away, Team2=home) to Matchup."""
    return Matchup(
        away=_str(row, "away"),
        home=_str(row, "home"),
        game_time=_str(row, "game_time"),
        lines=MatchupLines(
            ml_away=_str(row, "Team1Spread"),
            ml_home=_str(row, "Team2Spread"),
            run_line_away=_str(row, "Team1RunLine"),
            run_line_home=_str(row, "Team2RunLine"),
            total=_str(row, "Total"),
        ),
        favorite=_str(row, "Favorite"),
        espn_game_id=_str(row, "espn_game_id"),
    )


def load_matchups(path: str | Path) -> list[Matchup]:
    """Read and normalize MLB matchups.json into Matchup models."""
    path = Path(path)
    raw = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(raw, list):
        raise ValueError(f"Expected a JSON array of matchups in {path}")
    out: list[Matchup] = []
    for row in raw:
        if not isinstance(row, dict):
            continue
        m = matchup_from_row(row)
        if not m.away or not m.home:
            continue
        out.append(m)
    return out


def matchup_key(away: str, home: str) -> str:
    return f"{away} @ {home}"


def parse_matchup_teams(matchup: str) -> tuple[str, str] | None:
    """Split 'Away @ Home' into (away, home)."""
    if "@" not in matchup:
        return None
    away, home = matchup.split("@", 1)
    away, home = away.strip(), home.strip()
    if not away or not home:
        return None
    return away, home
