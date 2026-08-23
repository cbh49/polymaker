"""Canonical MLB team names / abbreviations used by the betting-splits scrapers.

The Docker image only contains trading-bot/, so scrapers must not depend on
../MLB/links/mlbTeamAbbrevations.json at runtime. Overlay that file when it
exists (local monorepo checkouts).
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

SCRIPT_DIR = Path(__file__).resolve().parent
REPO_ROOT = SCRIPT_DIR.parents[1]
DEFAULT_ABBREVS = REPO_ROOT / "MLB" / "links" / "mlbTeamAbbrevations.json"
DEFAULT_MATCHUPS = REPO_ROOT / "MLB" / "json" / "matchups.json"

# Preferred betting abbr -> full name.
ABBR_TO_NAME: dict[str, str] = {
    "ARI": "Arizona Diamondbacks",
    "ATL": "Atlanta Braves",
    "BAL": "Baltimore Orioles",
    "BOS": "Boston Red Sox",
    "CHC": "Chicago Cubs",
    "CWS": "Chicago White Sox",
    "CIN": "Cincinnati Reds",
    "CLE": "Cleveland Guardians",
    "COL": "Colorado Rockies",
    "DET": "Detroit Tigers",
    "HOU": "Houston Astros",
    "KC": "Kansas City Royals",
    "LAA": "Los Angeles Angels",
    "LAD": "Los Angeles Dodgers",
    "MIA": "Miami Marlins",
    "MIL": "Milwaukee Brewers",
    "MIN": "Minnesota Twins",
    "NYM": "New York Mets",
    "NYY": "New York Yankees",
    "ATH": "Athletics",
    "PHI": "Philadelphia Phillies",
    "PIT": "Pittsburgh Pirates",
    "SD": "San Diego Padres",
    "SF": "San Francisco Giants",
    "SEA": "Seattle Mariners",
    "STL": "St. Louis Cardinals",
    "TB": "Tampa Bay Rays",
    "TEX": "Texas Rangers",
    "TOR": "Toronto Blue Jays",
    "WSH": "Washington Nationals",
}

# Alternate codes / labels that show up on PlayerProps, VSiN, SBD, EVA, Covers.
ABBR_ALIASES: dict[str, str] = {
    "AZ": "ARI",
    "ARI": "ARI",
    "CHW": "CWS",
    "CWS": "CWS",
    "WAS": "WSH",
    "WSH": "WSH",
    "WSN": "WSH",
    "OAK": "ATH",
    "ATH": "ATH",
    "FLA": "MIA",
    "KCR": "KC",
    "SDP": "SD",
    "SFG": "SF",
    "TBR": "TB",
}

NAME_ALIASES: dict[str, str] = {
    "st louis cardinals": "St. Louis Cardinals",
    "st. louis cardinals": "St. Louis Cardinals",
    "oakland athletics": "Athletics",
    "athletics": "Athletics",
    "arizona diamondbacks": "Arizona Diamondbacks",
    "chicago white sox": "Chicago White Sox",
    "washington nationals": "Washington Nationals",
}


def _norm_key(text: str) -> str:
    return re.sub(r"\s+", " ", text.strip().lower())


def _is_standard_abbr(abbr: str) -> bool:
    return bool(re.fullmatch(r"[A-Z]{2,3}", abbr))


def _build_name_to_abbr() -> dict[str, str]:
    mapping: dict[str, str] = {}
    nick_to_abbrs: dict[str, list[str]] = {}
    for abbr, name in ABBR_TO_NAME.items():
        mapping[_norm_key(name)] = abbr
        nick_to_abbrs.setdefault(_norm_key(name.rsplit(" ", 1)[-1]), []).append(abbr)
    for nick, abbrs in nick_to_abbrs.items():
        if len(abbrs) == 1:
            mapping.setdefault(nick, abbrs[0])
    for alias, name in NAME_ALIASES.items():
        mapping[_norm_key(alias)] = mapping[_norm_key(name)]
    for alias, canonical in ABBR_ALIASES.items():
        mapping[_norm_key(alias)] = canonical
    return mapping


NAME_TO_ABBR = _build_name_to_abbr()


def canonical_name(text: str) -> str | None:
    raw = (text or "").strip()
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
    raw = (text or "").strip()
    if not raw:
        return None
    upper = raw.upper()
    if upper in ABBR_ALIASES:
        return ABBR_ALIASES[upper]
    if upper in ABBR_TO_NAME:
        return upper
    name = canonical_name(raw)
    if not name:
        return None
    return NAME_TO_ABBR.get(_norm_key(name))


def _overlay_file(path: Path | None, mapping: dict[str, str]) -> None:
    if path is None or not path.exists():
        return
    raw = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(raw, dict):
        return
    for abbr, name in raw.items():
        if isinstance(abbr, str) and isinstance(name, str):
            mapping[abbr.strip().upper()] = name.strip()


def load_abbr_to_name(path: Path | None = DEFAULT_ABBREVS) -> dict[str, str]:
    """Full abbr -> name map. Bundled table first; optional JSON overlay."""
    mapping = dict(ABBR_TO_NAME)
    for alias, canonical in ABBR_ALIASES.items():
        mapping[alias] = ABBR_TO_NAME[canonical]
    _overlay_file(path, mapping)
    for alias, canonical in ABBR_ALIASES.items():
        mapping[alias] = ABBR_TO_NAME[canonical]
    return mapping


def load_abbr_maps(
    path: Path | None = DEFAULT_ABBREVS,
) -> tuple[dict[str, str], dict[str, str]]:
    """Return (abbr->name, normalized_name->preferred abbr)."""
    abbr_to_name = load_abbr_to_name(path)
    name_to_abbr: dict[str, str] = {}
    for abbr, name in abbr_to_name.items():
        key = _norm_key(name)
        if key not in name_to_abbr or (
            _is_standard_abbr(abbr) and not _is_standard_abbr(name_to_abbr[key])
        ):
            name_to_abbr[key] = abbr
    for name, abbr in NAME_TO_ABBR.items():
        if name not in name_to_abbr or _is_standard_abbr(abbr):
            name_to_abbr[name] = abbr
    # Prefer the codes PlayerProps / books actually print on matchup strings.
    name_to_abbr["arizona diamondbacks"] = "AZ"
    name_to_abbr["chicago white sox"] = "CWS"
    name_to_abbr["washington nationals"] = "WSH"
    name_to_abbr["athletics"] = "ATH"
    return abbr_to_name, name_to_abbr


def load_matchups(path: Path = DEFAULT_MATCHUPS) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    raw = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(raw, list):
        return []
    return [row for row in raw if isinstance(row, dict)]
