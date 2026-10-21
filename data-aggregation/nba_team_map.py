"""Canonical NBA team names and abbreviations used by the betting-splits scrapers.

Sources disagree on labels (DK "GS Warriors" / "LA Lakers" / "PHO Suns" / "NO Pelicans",
Polymarket `gsw` / `lal` / `phx` / `nop`). Resolve to a stable betting abbr + full
name so splits merge. Lakers=`LAL`, Clippers=`LAC`. Bare "LA" is not a team.
"""

from __future__ import annotations

import re
from typing import Any

# Canonical betting abbr -> full team name. DK-style codes (GS, PHO, NO, NY, SA, UTA).
ABBR_TO_NAME: dict[str, str] = {
    "ATL": "Atlanta Hawks",
    "BOS": "Boston Celtics",
    "BKN": "Brooklyn Nets",
    "CHA": "Charlotte Hornets",
    "CHI": "Chicago Bulls",
    "CLE": "Cleveland Cavaliers",
    "DAL": "Dallas Mavericks",
    "DEN": "Denver Nuggets",
    "DET": "Detroit Pistons",
    "GS": "Golden State Warriors",
    "HOU": "Houston Rockets",
    "IND": "Indiana Pacers",
    "LAC": "Los Angeles Clippers",
    "LAL": "Los Angeles Lakers",
    "MEM": "Memphis Grizzlies",
    "MIA": "Miami Heat",
    "MIL": "Milwaukee Bucks",
    "MIN": "Minnesota Timberwolves",
    "NO": "New Orleans Pelicans",
    "NY": "New York Knicks",
    "OKC": "Oklahoma City Thunder",
    "ORL": "Orlando Magic",
    "PHI": "Philadelphia 76ers",
    "PHO": "Phoenix Suns",
    "POR": "Portland Trail Blazers",
    "SAC": "Sacramento Kings",
    "SA": "San Antonio Spurs",
    "TOR": "Toronto Raptors",
    "UTA": "Utah Jazz",
    "WAS": "Washington Wizards",
}

ABBR_ALIASES: dict[str, str] = {
    "GSW": "GS",
    "PHX": "PHO",
    "NOP": "NO",
    "NYK": "NY",
    "SAS": "SA",
    "UTAH": "UTA",
    "WSH": "WAS",
    "CHO": "CHA",
    "BRK": "BKN",
    "BK": "BKN",
}

# Extra labels from DK, TheSpread, SBD, EVA, Covers.
# Do not alias bare "Los Angeles" / "LA" — Lakers and Clippers share that city.
NAME_ALIASES: dict[str, str] = {
    "atlanta": "Atlanta Hawks",
    "atlanta hawks": "Atlanta Hawks",
    "hawks": "Atlanta Hawks",
    "boston": "Boston Celtics",
    "boston celtics": "Boston Celtics",
    "celtics": "Boston Celtics",
    "brooklyn": "Brooklyn Nets",
    "brooklyn nets": "Brooklyn Nets",
    "nets": "Brooklyn Nets",
    "charlotte": "Charlotte Hornets",
    "charlotte hornets": "Charlotte Hornets",
    "hornets": "Charlotte Hornets",
    "chicago": "Chicago Bulls",
    "chicago bulls": "Chicago Bulls",
    "bulls": "Chicago Bulls",
    "cleveland": "Cleveland Cavaliers",
    "cleveland cavaliers": "Cleveland Cavaliers",
    "cavaliers": "Cleveland Cavaliers",
    "cavs": "Cleveland Cavaliers",
    "dallas": "Dallas Mavericks",
    "dallas mavericks": "Dallas Mavericks",
    "mavericks": "Dallas Mavericks",
    "mavs": "Dallas Mavericks",
    "denver": "Denver Nuggets",
    "denver nuggets": "Denver Nuggets",
    "nuggets": "Denver Nuggets",
    "detroit": "Detroit Pistons",
    "detroit pistons": "Detroit Pistons",
    "pistons": "Detroit Pistons",
    "golden state": "Golden State Warriors",
    "golden state warriors": "Golden State Warriors",
    "gs warriors": "Golden State Warriors",
    "gsw warriors": "Golden State Warriors",
    "warriors": "Golden State Warriors",
    "houston": "Houston Rockets",
    "houston rockets": "Houston Rockets",
    "rockets": "Houston Rockets",
    "indiana": "Indiana Pacers",
    "indiana pacers": "Indiana Pacers",
    "pacers": "Indiana Pacers",
    "la clippers": "Los Angeles Clippers",
    "los angeles clippers": "Los Angeles Clippers",
    "clippers": "Los Angeles Clippers",
    "la lakers": "Los Angeles Lakers",
    "los angeles lakers": "Los Angeles Lakers",
    "lakers": "Los Angeles Lakers",
    "memphis": "Memphis Grizzlies",
    "memphis grizzlies": "Memphis Grizzlies",
    "grizzlies": "Memphis Grizzlies",
    "miami": "Miami Heat",
    "miami heat": "Miami Heat",
    "heat": "Miami Heat",
    "milwaukee": "Milwaukee Bucks",
    "milwaukee bucks": "Milwaukee Bucks",
    "bucks": "Milwaukee Bucks",
    "minnesota": "Minnesota Timberwolves",
    "minnesota timberwolves": "Minnesota Timberwolves",
    "timberwolves": "Minnesota Timberwolves",
    "wolves": "Minnesota Timberwolves",
    "new orleans": "New Orleans Pelicans",
    "new orleans pelicans": "New Orleans Pelicans",
    "no pelicans": "New Orleans Pelicans",
    "pelicans": "New Orleans Pelicans",
    "new york": "New York Knicks",
    "new york knicks": "New York Knicks",
    "ny knicks": "New York Knicks",
    "knicks": "New York Knicks",
    "oklahoma city": "Oklahoma City Thunder",
    "oklahoma city thunder": "Oklahoma City Thunder",
    "okc thunder": "Oklahoma City Thunder",
    "thunder": "Oklahoma City Thunder",
    "orlando": "Orlando Magic",
    "orlando magic": "Orlando Magic",
    "magic": "Orlando Magic",
    "philadelphia": "Philadelphia 76ers",
    "philadelphia 76ers": "Philadelphia 76ers",
    "76ers": "Philadelphia 76ers",
    "sixers": "Philadelphia 76ers",
    "phoenix": "Phoenix Suns",
    "phoenix suns": "Phoenix Suns",
    "pho suns": "Phoenix Suns",
    "phx suns": "Phoenix Suns",
    "suns": "Phoenix Suns",
    "portland": "Portland Trail Blazers",
    "portland trail blazers": "Portland Trail Blazers",
    "trail blazers": "Portland Trail Blazers",
    "blazers": "Portland Trail Blazers",
    "sacramento": "Sacramento Kings",
    "sacramento kings": "Sacramento Kings",
    "kings": "Sacramento Kings",
    "san antonio": "San Antonio Spurs",
    "san antonio spurs": "San Antonio Spurs",
    "sa spurs": "San Antonio Spurs",
    "spurs": "San Antonio Spurs",
    "toronto": "Toronto Raptors",
    "toronto raptors": "Toronto Raptors",
    "raptors": "Toronto Raptors",
    "utah": "Utah Jazz",
    "utah jazz": "Utah Jazz",
    "uta jazz": "Utah Jazz",
    "jazz": "Utah Jazz",
    "washington": "Washington Wizards",
    "washington wizards": "Washington Wizards",
    "was wizards": "Washington Wizards",
    "wizards": "Washington Wizards",
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


def _la_team(text: str) -> str | None:
    """Lakers and Clippers both show up as an LA token. Nickname decides."""
    key = _norm_key(text)
    if "clipper" in key:
        return "LAC"
    if "laker" in key:
        return "LAL"
    return None


def _abbr_plus_nickname(text: str) -> str | None:
    """Resolve 'MIL Bucks' / 'GS Warriors' when the nickname belongs to that abbr.

    The first token alone is not enough: 'MIN Lynx' shares an NBA abbr but is
    not the Timberwolves.
    """
    parts = _norm_key(text).split()
    if len(parts) < 2:
        return None
    token = re.sub(r"[^A-Za-z0-9]", "", parts[0]).upper()
    abbr = ABBR_ALIASES.get(token)
    if abbr is None and token in ABBR_TO_NAME:
        abbr = token
    if abbr is None:
        return None
    nickname = _norm_key(ABBR_TO_NAME[abbr]).rsplit(" ", 1)[-1]
    rest = " ".join(parts[1:])
    if rest == nickname or rest.endswith(" " + nickname):
        return abbr
    return None


def canonical_name(text: str) -> str | None:
    """Resolve a DK/TheSpread/SBD/EVA label to the canonical full team name."""
    raw = (text or "").strip()
    if not raw:
        return None
    la = _la_team(raw)
    if la:
        return ABBR_TO_NAME[la]
    key = _norm_key(raw)
    if key in NAME_ALIASES:
        return NAME_ALIASES[key]
    upper = re.sub(r"[^A-Za-z0-9]", "", raw).upper()
    if upper in ABBR_ALIASES:
        return ABBR_TO_NAME[ABBR_ALIASES[upper]]
    if upper in ABBR_TO_NAME:
        return ABBR_TO_NAME[upper]
    abbr = _abbr_plus_nickname(raw) or NAME_TO_ABBR.get(key)
    if abbr:
        return ABBR_TO_NAME[abbr]
    return raw


def canonical_abbr(text: str) -> str | None:
    raw = (text or "").strip()
    if not raw:
        return None
    la = _la_team(raw)
    if la:
        return la
    upper = re.sub(r"[^A-Za-z0-9]", "", raw).upper()
    if upper in ABBR_ALIASES:
        return ABBR_ALIASES[upper]
    if upper in ABBR_TO_NAME:
        return upper
    return _abbr_plus_nickname(raw) or NAME_TO_ABBR.get(_norm_key(raw))


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
