"""Canonical NFL team names and abbreviations used by the betting-splits scrapers.

Sources disagree on labels (DK "KC Chiefs", VSiN "Wash Commanders", SBD
"Kansas City" / KC). Resolve to a stable betting abbr + full name so splits
merge. Rams=`LA`, Chargers=`LAC` to match Polymarket / ev_trading.
"""

from __future__ import annotations

import re
from typing import Any

# Canonical betting abbr -> full team name (matches ev_trading.nfl_odds).
ABBR_TO_NAME: dict[str, str] = {
    "ARI": "Arizona Cardinals",
    "ATL": "Atlanta Falcons",
    "BAL": "Baltimore Ravens",
    "BUF": "Buffalo Bills",
    "CAR": "Carolina Panthers",
    "CHI": "Chicago Bears",
    "CIN": "Cincinnati Bengals",
    "CLE": "Cleveland Browns",
    "DAL": "Dallas Cowboys",
    "DEN": "Denver Broncos",
    "DET": "Detroit Lions",
    "GB": "Green Bay Packers",
    "HOU": "Houston Texans",
    "IND": "Indianapolis Colts",
    "JAX": "Jacksonville Jaguars",
    "KC": "Kansas City Chiefs",
    "LA": "Los Angeles Rams",
    "LAC": "Los Angeles Chargers",
    "LV": "Las Vegas Raiders",
    "MIA": "Miami Dolphins",
    "MIN": "Minnesota Vikings",
    "NE": "New England Patriots",
    "NO": "New Orleans Saints",
    "NYG": "New York Giants",
    "NYJ": "New York Jets",
    "PHI": "Philadelphia Eagles",
    "PIT": "Pittsburgh Steelers",
    "SEA": "Seattle Seahawks",
    "SF": "San Francisco 49ers",
    "TB": "Tampa Bay Buccaneers",
    "TEN": "Tennessee Titans",
    "WAS": "Washington Commanders",
}

ABBR_ALIASES: dict[str, str] = {
    "JAC": "JAX",
    "JAX": "JAX",
    "WSH": "WAS",
    "WAS": "WAS",
    "LAR": "LA",
    "LA": "LA",
    "GBP": "GB",
    "GB": "GB",
    "GNB": "GB",
    "KCC": "KC",
    "KC": "KC",
    "LVR": "LV",
    "LV": "LV",
    "OAK": "LV",
    "NEP": "NE",
    "NE": "NE",
    "NOS": "NO",
    "NO": "NO",
    "SFO": "SF",
    "SF": "SF",
    "TBB": "TB",
    "TB": "TB",
    "TAM": "TB",
    "ARZ": "ARI",
    "AZ": "ARI",
}

# Extra labels that show up on DK, VSiN, TheSpread, SBD, EVA, Covers.
# Do not alias city-only "New York" / "Los Angeles" — those are two teams each.
NAME_ALIASES: dict[str, str] = {
    "arizona": "Arizona Cardinals",
    "arizona cardinals": "Arizona Cardinals",
    "cardinals": "Arizona Cardinals",
    "atlanta": "Atlanta Falcons",
    "atlanta falcons": "Atlanta Falcons",
    "falcons": "Atlanta Falcons",
    "baltimore": "Baltimore Ravens",
    "baltimore ravens": "Baltimore Ravens",
    "ravens": "Baltimore Ravens",
    "buffalo": "Buffalo Bills",
    "buffalo bills": "Buffalo Bills",
    "bills": "Buffalo Bills",
    "carolina": "Carolina Panthers",
    "carolina panthers": "Carolina Panthers",
    "panthers": "Carolina Panthers",
    "chicago": "Chicago Bears",
    "chicago bears": "Chicago Bears",
    "bears": "Chicago Bears",
    "cincinnati": "Cincinnati Bengals",
    "cincinnati bengals": "Cincinnati Bengals",
    "bengals": "Cincinnati Bengals",
    "cleveland": "Cleveland Browns",
    "cleveland browns": "Cleveland Browns",
    "browns": "Cleveland Browns",
    "dallas": "Dallas Cowboys",
    "dallas cowboys": "Dallas Cowboys",
    "cowboys": "Dallas Cowboys",
    "denver": "Denver Broncos",
    "denver broncos": "Denver Broncos",
    "broncos": "Denver Broncos",
    "detroit": "Detroit Lions",
    "detroit lions": "Detroit Lions",
    "lions": "Detroit Lions",
    "green bay": "Green Bay Packers",
    "green bay packers": "Green Bay Packers",
    "gb packers": "Green Bay Packers",
    "packers": "Green Bay Packers",
    "houston": "Houston Texans",
    "houston texans": "Houston Texans",
    "texans": "Houston Texans",
    "indianapolis": "Indianapolis Colts",
    "indianapolis colts": "Indianapolis Colts",
    "colts": "Indianapolis Colts",
    "jacksonville": "Jacksonville Jaguars",
    "jacksonville jaguars": "Jacksonville Jaguars",
    "jaguars": "Jacksonville Jaguars",
    "jags": "Jacksonville Jaguars",
    "kansas city": "Kansas City Chiefs",
    "kansas city chiefs": "Kansas City Chiefs",
    "kc chiefs": "Kansas City Chiefs",
    "chiefs": "Kansas City Chiefs",
    "la rams": "Los Angeles Rams",
    "los angeles rams": "Los Angeles Rams",
    "rams": "Los Angeles Rams",
    "la chargers": "Los Angeles Chargers",
    "los angeles chargers": "Los Angeles Chargers",
    "chargers": "Los Angeles Chargers",
    "las vegas": "Las Vegas Raiders",
    "las vegas raiders": "Las Vegas Raiders",
    "lv raiders": "Las Vegas Raiders",
    "raiders": "Las Vegas Raiders",
    "miami": "Miami Dolphins",
    "miami dolphins": "Miami Dolphins",
    "dolphins": "Miami Dolphins",
    "minnesota": "Minnesota Vikings",
    "minnesota vikings": "Minnesota Vikings",
    "vikings": "Minnesota Vikings",
    "new england": "New England Patriots",
    "new england patriots": "New England Patriots",
    "ne patriots": "New England Patriots",
    "patriots": "New England Patriots",
    "pats": "New England Patriots",
    "new orleans": "New Orleans Saints",
    "new orleans saints": "New Orleans Saints",
    "saints": "New Orleans Saints",
    "ny giants": "New York Giants",
    "new york giants": "New York Giants",
    "giants": "New York Giants",
    "ny jets": "New York Jets",
    "new york jets": "New York Jets",
    "jets": "New York Jets",
    "philadelphia": "Philadelphia Eagles",
    "philadelphia eagles": "Philadelphia Eagles",
    "eagles": "Philadelphia Eagles",
    "pittsburgh": "Pittsburgh Steelers",
    "pittsburgh steelers": "Pittsburgh Steelers",
    "steelers": "Pittsburgh Steelers",
    "seattle": "Seattle Seahawks",
    "seattle seahawks": "Seattle Seahawks",
    "seahawks": "Seattle Seahawks",
    "san francisco": "San Francisco 49ers",
    "san francisco 49ers": "San Francisco 49ers",
    "sf 49ers": "San Francisco 49ers",
    "49ers": "San Francisco 49ers",
    "niners": "San Francisco 49ers",
    "tampa": "Tampa Bay Buccaneers",
    "tampa bay": "Tampa Bay Buccaneers",
    "tampa bay buccaneers": "Tampa Bay Buccaneers",
    "buccaneers": "Tampa Bay Buccaneers",
    "bucs": "Tampa Bay Buccaneers",
    "tennessee": "Tennessee Titans",
    "tennessee titans": "Tennessee Titans",
    "titans": "Tennessee Titans",
    "washington": "Washington Commanders",
    "washington commanders": "Washington Commanders",
    "wash commanders": "Washington Commanders",
    "commanders": "Washington Commanders",
}


def _norm_key(text: str) -> str:
    return re.sub(r"\s+", " ", (text or "").strip().lower())


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
    """Resolve a DK/VSiN/TheSpread/SBD/EVA label to the canonical full team name."""
    raw = (text or "").strip()
    if not raw:
        return None
    key = _norm_key(raw)
    if key in NAME_ALIASES:
        return NAME_ALIASES[key]
    upper = re.sub(r"[^A-Za-z]", "", raw).upper()
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
    upper = re.sub(r"[^A-Za-z]", "", raw).upper()
    if upper in ABBR_ALIASES:
        return ABBR_ALIASES[upper]
    if upper in ABBR_TO_NAME:
        return upper
    return NAME_TO_ABBR.get(_norm_key(raw))


def names_match(a: str | None, b: str | None) -> bool:
    if not a or not b:
        return False
    aa, ab = canonical_abbr(a), canonical_abbr(b)
    if aa and ab:
        return aa == ab
    ca, cb = canonical_name(a) or a, canonical_name(b) or b
    return _norm_key(ca) == _norm_key(cb)


def match_matchup(
    away_name: str | None,
    home_name: str | None,
    matchups: list[dict[str, Any]],
) -> dict[str, Any] | None:
    if not away_name or not home_name:
        return None
    for row in matchups:
        if names_match(away_name, str(row.get("away") or "")) and names_match(
            home_name, str(row.get("home") or "")
        ):
            return row
    return None
