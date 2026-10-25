"""Canonical NHL team names and abbreviations used by the betting-splits scrapers.

Sources disagree on labels (DK "NY Islanders" / "LA Kings" / "MTL Canadiens",
Polymarket `nyi` / `lak` / `mon`). Resolve to a stable betting abbr + full
name so splits merge. Islanders=`NYI`, Rangers=`NYR`. Bare "NY" is not a team.
"""

from __future__ import annotations

import re
from typing import Any

# Canonical betting abbr -> full team name. NHL API codes.
ABBR_TO_NAME: dict[str, str] = {
    "ANA": "Anaheim Ducks",
    "BOS": "Boston Bruins",
    "BUF": "Buffalo Sabres",
    "CAR": "Carolina Hurricanes",
    "CBJ": "Columbus Blue Jackets",
    "CGY": "Calgary Flames",
    "CHI": "Chicago Blackhawks",
    "COL": "Colorado Avalanche",
    "DAL": "Dallas Stars",
    "DET": "Detroit Red Wings",
    "EDM": "Edmonton Oilers",
    "FLA": "Florida Panthers",
    "LAK": "Los Angeles Kings",
    "MIN": "Minnesota Wild",
    "MTL": "Montreal Canadiens",
    "NJD": "New Jersey Devils",
    "NSH": "Nashville Predators",
    "NYI": "New York Islanders",
    "NYR": "New York Rangers",
    "OTT": "Ottawa Senators",
    "PHI": "Philadelphia Flyers",
    "PIT": "Pittsburgh Penguins",
    "SEA": "Seattle Kraken",
    "SJS": "San Jose Sharks",
    "STL": "St. Louis Blues",
    "TBL": "Tampa Bay Lightning",
    "TOR": "Toronto Maple Leafs",
    "UTA": "Utah Mammoth",
    "VAN": "Vancouver Canucks",
    "VGK": "Vegas Golden Knights",
    "WPG": "Winnipeg Jets",
    "WSH": "Washington Capitals",
}

# Polymarket nhl-YYYY slug tokens. Calgary=`cal`, Vegas=`las`, Montreal=`mon`,
# New Jersey=`nj`, San Jose=`sj`, Tampa=`tb`, Utah=`utah`.
POLY_CODES: dict[str, str] = {
    "ANA": "ana",
    "BOS": "bos",
    "BUF": "buf",
    "CAR": "car",
    "CBJ": "cbj",
    "CGY": "cal",
    "CHI": "chi",
    "COL": "col",
    "DAL": "dal",
    "DET": "det",
    "EDM": "edm",
    "FLA": "fla",
    "LAK": "lak",
    "MIN": "min",
    "MTL": "mon",
    "NJD": "nj",
    "NSH": "nsh",
    "NYI": "nyi",
    "NYR": "nyr",
    "OTT": "ott",
    "PHI": "phi",
    "PIT": "pit",
    "SEA": "sea",
    "SJS": "sj",
    "STL": "stl",
    "TBL": "tb",
    "TOR": "tor",
    "UTA": "utah",
    "VAN": "van",
    "VGK": "las",
    "WPG": "wpg",
    "WSH": "wsh",
}

ABBR_ALIASES: dict[str, str] = {
    "LA": "LAK",
    "L.A": "LAK",
    "L.A.": "LAK",
    "SJ": "SJS",
    "S.J": "SJS",
    "S.J.": "SJS",
    "TB": "TBL",
    "T.B": "TBL",
    "T.B.": "TBL",
    "NJ": "NJD",
    "N.J": "NJD",
    "N.J.": "NJD",
    "MON": "MTL",
    "NAS": "NSH",
    "VEG": "VGK",
    "LV": "VGK",
    "LVR": "VGK",
    "LAS": "VGK",
    "WAS": "WSH",
    "UTAH": "UTA",
    "CAL": "CGY",
}

# Extra labels from DK, TheSpread, SBD, EVA, Covers.
# Do not alias bare "New York" / "NY" — Islanders and Rangers share that city.
NAME_ALIASES: dict[str, str] = {
    "anaheim": "Anaheim Ducks",
    "anaheim ducks": "Anaheim Ducks",
    "ducks": "Anaheim Ducks",
    "boston": "Boston Bruins",
    "boston bruins": "Boston Bruins",
    "bruins": "Boston Bruins",
    "buffalo": "Buffalo Sabres",
    "buffalo sabres": "Buffalo Sabres",
    "sabres": "Buffalo Sabres",
    "carolina": "Carolina Hurricanes",
    "carolina hurricanes": "Carolina Hurricanes",
    "hurricanes": "Carolina Hurricanes",
    "canes": "Carolina Hurricanes",
    "columbus": "Columbus Blue Jackets",
    "columbus blue jackets": "Columbus Blue Jackets",
    "blue jackets": "Columbus Blue Jackets",
    "cbj blue jackets": "Columbus Blue Jackets",
    "jackets": "Columbus Blue Jackets",
    "calgary": "Calgary Flames",
    "calgary flames": "Calgary Flames",
    "cgy flames": "Calgary Flames",
    "flames": "Calgary Flames",
    "chicago": "Chicago Blackhawks",
    "chicago blackhawks": "Chicago Blackhawks",
    "blackhawks": "Chicago Blackhawks",
    "hawks": "Chicago Blackhawks",
    "colorado": "Colorado Avalanche",
    "colorado avalanche": "Colorado Avalanche",
    "col avalanche": "Colorado Avalanche",
    "avalanche": "Colorado Avalanche",
    "avs": "Colorado Avalanche",
    "dallas": "Dallas Stars",
    "dallas stars": "Dallas Stars",
    "dal stars": "Dallas Stars",
    "stars": "Dallas Stars",
    "detroit": "Detroit Red Wings",
    "detroit red wings": "Detroit Red Wings",
    "red wings": "Detroit Red Wings",
    "det red wings": "Detroit Red Wings",
    "wings": "Detroit Red Wings",
    "edmonton": "Edmonton Oilers",
    "edmonton oilers": "Edmonton Oilers",
    "edm oilers": "Edmonton Oilers",
    "oilers": "Edmonton Oilers",
    "florida": "Florida Panthers",
    "florida panthers": "Florida Panthers",
    "panthers": "Florida Panthers",
    "los angeles": "Los Angeles Kings",
    "los angeles kings": "Los Angeles Kings",
    "la kings": "Los Angeles Kings",
    "lak kings": "Los Angeles Kings",
    "kings": "Los Angeles Kings",
    "minnesota": "Minnesota Wild",
    "minnesota wild": "Minnesota Wild",
    "min wild": "Minnesota Wild",
    "wild": "Minnesota Wild",
    "montreal": "Montreal Canadiens",
    "montreal canadiens": "Montreal Canadiens",
    "mtl canadiens": "Montreal Canadiens",
    "canadiens": "Montreal Canadiens",
    "canadians": "Montreal Canadiens",
    "habs": "Montreal Canadiens",
    "new jersey": "New Jersey Devils",
    "new jersey devils": "New Jersey Devils",
    "nj devils": "New Jersey Devils",
    "njd devils": "New Jersey Devils",
    "devils": "New Jersey Devils",
    "nashville": "Nashville Predators",
    "nashville predators": "Nashville Predators",
    "nsh predators": "Nashville Predators",
    "predators": "Nashville Predators",
    "preds": "Nashville Predators",
    "new york islanders": "New York Islanders",
    "ny islanders": "New York Islanders",
    "nyi islanders": "New York Islanders",
    "islanders": "New York Islanders",
    "isles": "New York Islanders",
    "new york rangers": "New York Rangers",
    "ny rangers": "New York Rangers",
    "nyr rangers": "New York Rangers",
    "rangers": "New York Rangers",
    "ottawa": "Ottawa Senators",
    "ottawa senators": "Ottawa Senators",
    "senators": "Ottawa Senators",
    "sens": "Ottawa Senators",
    "philadelphia": "Philadelphia Flyers",
    "philadelphia flyers": "Philadelphia Flyers",
    "flyers": "Philadelphia Flyers",
    "pittsburgh": "Pittsburgh Penguins",
    "pittsburgh penguins": "Pittsburgh Penguins",
    "penguins": "Pittsburgh Penguins",
    "pens": "Pittsburgh Penguins",
    "seattle": "Seattle Kraken",
    "seattle kraken": "Seattle Kraken",
    "kraken": "Seattle Kraken",
    "san jose": "San Jose Sharks",
    "san jose sharks": "San Jose Sharks",
    "sj sharks": "San Jose Sharks",
    "sharks": "San Jose Sharks",
    "st louis": "St. Louis Blues",
    "st louis blues": "St. Louis Blues",
    "st. louis blues": "St. Louis Blues",
    "stl blues": "St. Louis Blues",
    "blues": "St. Louis Blues",
    "tampa bay": "Tampa Bay Lightning",
    "tampa bay lightning": "Tampa Bay Lightning",
    "tb lightning": "Tampa Bay Lightning",
    "tbl lightning": "Tampa Bay Lightning",
    "lightning": "Tampa Bay Lightning",
    "bolts": "Tampa Bay Lightning",
    "toronto": "Toronto Maple Leafs",
    "toronto maple leafs": "Toronto Maple Leafs",
    "tor maple leafs": "Toronto Maple Leafs",
    "maple leafs": "Toronto Maple Leafs",
    "leafs": "Toronto Maple Leafs",
    "utah": "Utah Mammoth",
    "utah mammoth": "Utah Mammoth",
    "uta mammoth": "Utah Mammoth",
    "utah hockey club": "Utah Mammoth",
    "mammoth": "Utah Mammoth",
    "vancouver": "Vancouver Canucks",
    "vancouver canucks": "Vancouver Canucks",
    "van canucks": "Vancouver Canucks",
    "canucks": "Vancouver Canucks",
    "vegas": "Vegas Golden Knights",
    "vegas golden knights": "Vegas Golden Knights",
    "golden knights": "Vegas Golden Knights",
    "vgk golden knights": "Vegas Golden Knights",
    "knights": "Vegas Golden Knights",
    "winnipeg": "Winnipeg Jets",
    "winnipeg jets": "Winnipeg Jets",
    "wpg jets": "Winnipeg Jets",
    "jets": "Winnipeg Jets",
    "washington": "Washington Capitals",
    "washington capitals": "Washington Capitals",
    "wsh capitals": "Washington Capitals",
    "was capitals": "Washington Capitals",
    "capitals": "Washington Capitals",
    "caps": "Washington Capitals",
}


def _norm_key(text: str) -> str:
    cleaned = (text or "").replace(".", " ").replace("'", "")
    return re.sub(r"\s+", " ", cleaned.strip().lower())


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


def _ny_team(text: str) -> str | None:
    """Islanders and Rangers both show up as an NY token. Nickname decides."""
    key = _norm_key(text)
    if "islander" in key or key in {"isles", "nyi"}:
        return "NYI"
    if "ranger" in key or key == "nyr":
        return "NYR"
    return None


def _abbr_plus_nickname(text: str) -> str | None:
    """Resolve 'DAL Stars' / 'LA Kings' / 'MTL Canadiens' when the nickname matches.

    The first token alone is not enough: 'NY' is shared by the Islanders and Rangers.
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
    ny = _ny_team(raw)
    if ny:
        return ABBR_TO_NAME[ny]
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
    ny = _ny_team(raw)
    if ny:
        return ny
    upper = re.sub(r"[^A-Za-z0-9]", "", raw).upper()
    if upper in ABBR_ALIASES:
        return ABBR_ALIASES[upper]
    if upper in ABBR_TO_NAME:
        return upper
    return _abbr_plus_nickname(raw) or NAME_TO_ABBR.get(_norm_key(raw))


def poly_code(text: str) -> str | None:
    """Polymarket slug token for a betting abbr or team label."""
    abbr = canonical_abbr(text)
    if abbr is None:
        return None
    return POLY_CODES.get(abbr)


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
