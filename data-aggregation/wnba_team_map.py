"""Canonical WNBA team names / abbreviations used by the betting-splits scrapers."""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

SCRIPT_DIR = Path(__file__).resolve().parent
REPO_ROOT = SCRIPT_DIR.parents[1]
DEFAULT_MATCHUPS = REPO_ROOT / "WNBA" / "json-data" / "wnba_matchups.json"

# Canonical abbrev -> full name (matches WNBA/games.py).
ABBR_TO_NAME: dict[str, str] = {
    "ATL": "Atlanta Dream",
    "CHI": "Chicago Sky",
    "CON": "Connecticut Sun",
    "DAL": "Dallas Wings",
    "GS": "Golden State Valkyries",
    "IND": "Indiana Fever",
    "LA": "Los Angeles Sparks",
    "LV": "Las Vegas Aces",
    "MIN": "Minnesota Lynx",
    "NY": "New York Liberty",
    "PHX": "Phoenix Mercury",
    "POR": "Portland Fire",
    "SEA": "Seattle Storm",
    "TOR": "Toronto Tempo",
    "WAS": "Washington Mystics",
}

# Extra codes / labels that show up on DK, VSiN, TheSpread, ESPN.
NAME_ALIASES: dict[str, str] = {
    "atlanta": "Atlanta Dream",
    "atlanta dream": "Atlanta Dream",
    "atl dream": "Atlanta Dream",
    "chicago": "Chicago Sky",
    "chicago sky": "Chicago Sky",
    "chi sky": "Chicago Sky",
    "connecticut": "Connecticut Sun",
    "connecticut sun": "Connecticut Sun",
    "con sun": "Connecticut Sun",
    "conn sun": "Connecticut Sun",
    "dallas": "Dallas Wings",
    "dallas wings": "Dallas Wings",
    "dal wings": "Dallas Wings",
    "golden state": "Golden State Valkyries",
    "golden state valkyries": "Golden State Valkyries",
    "gs valkyries": "Golden State Valkyries",
    "gsv valkyries": "Golden State Valkyries",
    "indiana": "Indiana Fever",
    "indiana fever": "Indiana Fever",
    "ind fever": "Indiana Fever",
    "los angeles": "Los Angeles Sparks",
    "los angeles sparks": "Los Angeles Sparks",
    "la sparks": "Los Angeles Sparks",
    "las sparks": "Los Angeles Sparks",
    "las vegas": "Las Vegas Aces",
    "las vegas aces": "Las Vegas Aces",
    "lv aces": "Las Vegas Aces",
    "lva aces": "Las Vegas Aces",
    "minnesota": "Minnesota Lynx",
    "minnesota lynx": "Minnesota Lynx",
    "min lynx": "Minnesota Lynx",
    "new york": "New York Liberty",
    "new york liberty": "New York Liberty",
    "ny liberty": "New York Liberty",
    "nyl liberty": "New York Liberty",
    "phoenix": "Phoenix Mercury",
    "phoenix mercury": "Phoenix Mercury",
    "phx mercury": "Phoenix Mercury",
    "portland": "Portland Fire",
    "portland fire": "Portland Fire",
    "por fire": "Portland Fire",
    "seattle": "Seattle Storm",
    "seattle storm": "Seattle Storm",
    "sea storm": "Seattle Storm",
    "toronto": "Toronto Tempo",
    "toronto tempo": "Toronto Tempo",
    "tor tempo": "Toronto Tempo",
    "washington": "Washington Mystics",
    "washington mystics": "Washington Mystics",
    "was mystics": "Washington Mystics",
    "wsh mystics": "Washington Mystics",
}

ABBR_ALIASES: dict[str, str] = {
    "ATL": "ATL",
    "CHI": "CHI",
    "CON": "CON",
    "CONN": "CON",
    "DAL": "DAL",
    "GS": "GS",
    "GSV": "GS",
    "IND": "IND",
    "LA": "LA",
    "LAS": "LA",
    "LV": "LV",
    "LVA": "LV",
    "MIN": "MIN",
    "NY": "NY",
    "NYL": "NY",
    "PHX": "PHX",
    "PHO": "PHX",
    "POR": "POR",
    "SEA": "SEA",
    "TOR": "TOR",
    "WAS": "WAS",
    "WSH": "WAS",
}


def _norm_key(text: str) -> str:
    return re.sub(r"\s+", " ", text.strip().lower())


def _build_name_to_abbr() -> dict[str, str]:
    mapping: dict[str, str] = {}
    for abbr, name in ABBR_TO_NAME.items():
        mapping[_norm_key(name)] = abbr
        mapping.setdefault(_norm_key(name.rsplit(" ", 1)[-1]), abbr)
    full_to_abbr = {_norm_key(name): abbr for abbr, name in ABBR_TO_NAME.items()}
    for alias, name in NAME_ALIASES.items():
        mapping[_norm_key(alias)] = full_to_abbr[_norm_key(name)]
    return mapping


NAME_TO_ABBR = _build_name_to_abbr()


def canonical_name(text: str) -> str | None:
    """Resolve a DK/VSiN/TheSpread label to the canonical full team name."""
    raw = text.strip()
    if not raw:
        return None
    key = _norm_key(raw)
    if key in NAME_ALIASES:
        return NAME_ALIASES[key]
    upper = raw.upper()
    if upper in ABBR_ALIASES:
        return ABBR_TO_NAME[ABBR_ALIASES[upper]]
    if upper in ABBR_TO_NAME:
        return ABBR_TO_NAME[upper]
    abbr = NAME_TO_ABBR.get(key)
    if abbr:
        return ABBR_TO_NAME[abbr]
    return raw


def canonical_abbr(text: str) -> str | None:
    raw = text.strip()
    if not raw:
        return None
    upper = raw.upper()
    if upper in ABBR_ALIASES:
        return ABBR_ALIASES[upper]
    if upper in ABBR_TO_NAME:
        return upper
    return NAME_TO_ABBR.get(_norm_key(raw))


def load_matchups(path: Path = DEFAULT_MATCHUPS) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    raw = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(raw, list):
        return []
    return [row for row in raw if isinstance(row, dict)]


def match_matchup(
    away_name: str | None,
    home_name: str | None,
    matchups: list[dict[str, Any]],
) -> dict[str, Any] | None:
    if not away_name or not home_name:
        return None
    away_l, home_l = away_name.lower(), home_name.lower()
    for row in matchups:
        if str(row.get("away", "")).strip().lower() == away_l and str(
            row.get("home", "")
        ).strip().lower() == home_l:
            return row
    return None
