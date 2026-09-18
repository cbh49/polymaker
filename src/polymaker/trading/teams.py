"""Map betting-site team abbreviations to Polymarket sports slug codes + names.

Polymarket moneyline slugs look like `mlb-ari-atl-YYYY-MM-DD` /
`wnba-phx-la-YYYY-MM-DD` / `nfl-dal-nyg-YYYY-MM-DD` (away-home). Betting splits /
sharp-money JSON often use different abbreviations (AZ vs ari, LAR vs la).
"""

from __future__ import annotations

import re
import sys
from dataclasses import dataclass
from pathlib import Path

# Betting / ESPN-style abbr → Polymarket slug token (lowercase).
MLB_BETTING_TO_POLY: dict[str, str] = {
    "ARI": "ari",
    "AZ": "ari",
    "ATL": "atl",
    "BAL": "bal",
    "BOS": "bos",
    "CHC": "chc",
    "CHI": "chc",  # ambiguous; Cubs more common in MLB moneyline contexts
    "CHW": "cws",
    "CWS": "cws",
    "CIN": "cin",
    "CLE": "cle",
    "COL": "col",
    "DET": "det",
    "HOU": "hou",
    "KC": "kc",
    "KCR": "kc",
    "LAA": "laa",
    "LAD": "lad",
    "MIA": "mia",
    "FLA": "mia",
    "MIL": "mil",
    "MIN": "min",
    "NYM": "nym",
    "NYY": "nyy",
    "ATH": "oak",
    "OAK": "oak",
    "PHI": "phi",
    "PIT": "pit",
    "SD": "sd",
    "SDP": "sd",
    "SF": "sf",
    "SFG": "sf",
    "SEA": "sea",
    "STL": "stl",
    "TB": "tb",
    "TBR": "tb",
    "TEX": "tex",
    "TOR": "tor",
    "WSH": "wsh",
    "WAS": "wsh",
    "WSN": "wsh",
}

MLB_POLY_TO_NAME: dict[str, str] = {
    "ari": "Arizona Diamondbacks",
    "atl": "Atlanta Braves",
    "bal": "Baltimore Orioles",
    "bos": "Boston Red Sox",
    "chc": "Chicago Cubs",
    "cws": "Chicago White Sox",
    "cin": "Cincinnati Reds",
    "cle": "Cleveland Guardians",
    "col": "Colorado Rockies",
    "det": "Detroit Tigers",
    "hou": "Houston Astros",
    "kc": "Kansas City Royals",
    "laa": "Los Angeles Angels",
    "lad": "Los Angeles Dodgers",
    "mia": "Miami Marlins",
    "mil": "Milwaukee Brewers",
    "min": "Minnesota Twins",
    "nym": "New York Mets",
    "nyy": "New York Yankees",
    "oak": "Athletics",
    "phi": "Philadelphia Phillies",
    "pit": "Pittsburgh Pirates",
    "sd": "San Diego Padres",
    "sf": "San Francisco Giants",
    "sea": "Seattle Mariners",
    "stl": "St. Louis Cardinals",
    "tb": "Tampa Bay Rays",
    "tex": "Texas Rangers",
    "tor": "Toronto Blue Jays",
    "wsh": "Washington Nationals",
}

WNBA_BETTING_TO_POLY: dict[str, str] = {
    "ATL": "atl",
    "CHI": "chi",
    "CON": "conn",
    "CONN": "conn",
    "DAL": "dal",
    "GS": "gsv",
    "GSV": "gsv",
    "IND": "ind",
    "LA": "la",
    "LAS": "la",  # Los Angeles Sparks when LAS used for LA
    "LV": "las",
    "LVA": "las",
    "MIN": "min",
    "NY": "nyl",
    "NYL": "nyl",
    "PHX": "phx",
    "PHO": "phx",
    "POR": "por",
    "SEA": "sea",
    "TOR": "tor",
    "WAS": "wsh",
    "WSH": "wsh",
}

# Prefer explicit Sparks vs Aces when both LAS/LV appear; override after.
WNBA_POLY_TO_NAME: dict[str, str] = {
    "atl": "Atlanta Dream",
    "chi": "Chicago Sky",
    "conn": "Connecticut Sun",
    "dal": "Dallas Wings",
    "gsv": "Golden State Valkyries",
    "ind": "Indiana Fever",
    "la": "Los Angeles Sparks",
    "las": "Las Vegas Aces",
    "min": "Minnesota Lynx",
    "nyl": "New York Liberty",
    "phx": "Phoenix Mercury",
    "por": "Portland Fire",
    "sea": "Seattle Storm",
    "tor": "Toronto Tempo",
    "wsh": "Washington Mystics",
}

# Polymarket NFL tokens: Rams=`la`, Chargers=`lac`, Commanders=`was`, Jaguars=`jax`.
NFL_BETTING_TO_POLY: dict[str, str] = {
    "ARI": "ari",
    "ATL": "atl",
    "BAL": "bal",
    "BUF": "buf",
    "CAR": "car",
    "CHI": "chi",
    "CIN": "cin",
    "CLE": "cle",
    "DAL": "dal",
    "DEN": "den",
    "DET": "det",
    "GB": "gb",
    "GBP": "gb",
    "HOU": "hou",
    "IND": "ind",
    "JAC": "jax",
    "JAX": "jax",
    "KC": "kc",
    "KCC": "kc",
    "LA": "la",  # Rams; Chargers are LAC
    "LAR": "la",
    "LAC": "lac",
    "LV": "lv",
    "LVR": "lv",
    "MIA": "mia",
    "MIN": "min",
    "NE": "ne",
    "NEP": "ne",
    "NO": "no",
    "NOS": "no",
    "NYG": "nyg",
    "NYJ": "nyj",
    "PHI": "phi",
    "PIT": "pit",
    "SEA": "sea",
    "SF": "sf",
    "SFO": "sf",
    "TB": "tb",
    "TBB": "tb",
    "TEN": "ten",
    "WAS": "was",
    "WSH": "was",
}

NFL_POLY_TO_NAME: dict[str, str] = {
    "ari": "Arizona Cardinals",
    "atl": "Atlanta Falcons",
    "bal": "Baltimore Ravens",
    "buf": "Buffalo Bills",
    "car": "Carolina Panthers",
    "chi": "Chicago Bears",
    "cin": "Cincinnati Bengals",
    "cle": "Cleveland Browns",
    "dal": "Dallas Cowboys",
    "den": "Denver Broncos",
    "det": "Detroit Lions",
    "gb": "Green Bay Packers",
    "hou": "Houston Texans",
    "ind": "Indianapolis Colts",
    "jax": "Jacksonville Jaguars",
    "kc": "Kansas City Chiefs",
    "la": "Los Angeles Rams",
    "lac": "Los Angeles Chargers",
    "lv": "Las Vegas Raiders",
    "mia": "Miami Dolphins",
    "min": "Minnesota Vikings",
    "ne": "New England Patriots",
    "no": "New Orleans Saints",
    "nyg": "New York Giants",
    "nyj": "New York Jets",
    "phi": "Philadelphia Eagles",
    "pit": "Pittsburgh Steelers",
    "sea": "Seattle Seahawks",
    "sf": "San Francisco 49ers",
    "tb": "Tampa Bay Buccaneers",
    "ten": "Tennessee Titans",
    "was": "Washington Commanders",
}


@dataclass(frozen=True, slots=True)
class TeamRef:
    """Resolved team identity for matching."""

    betting_abbr: str
    poly_code: str
    full_name: str


def _cfb_team_map():
    """Lazy-load data-aggregation/cfb_team_map.py (canonical school names)."""
    try:
        import cfb_team_map as module  # type: ignore
        return module
    except ImportError:
        agg = Path(__file__).resolve().parents[3] / "data-aggregation"
        if str(agg) not in sys.path:
            sys.path.insert(0, str(agg))
        import cfb_team_map as module  # type: ignore
        return module


def resolve_team(league: str, abbr_or_name: str) -> TeamRef | None:
    """Resolve a betting abbr (or full name) to Polymarket code + display name."""
    raw = abbr_or_name.strip()
    if not raw:
        return None
    league_l = league.strip().lower()
    upper = raw.upper()

    if league_l == "mlb":
        code = MLB_BETTING_TO_POLY.get(upper)
        if code is None:
            code = _name_to_poly(raw, MLB_POLY_TO_NAME)
        if code is None:
            return None
        return TeamRef(upper if upper in MLB_BETTING_TO_POLY else raw, code, MLB_POLY_TO_NAME[code])

    if league_l == "wnba":
        # Disambiguate Las Vegas: prefer LV/LVA → las; LA Sparks → la.
        if upper in ("LV", "LVA"):
            return TeamRef(upper, "las", WNBA_POLY_TO_NAME["las"])
        if upper == "LAS":
            # Some feeds use LAS for Sparks; Polymarket uses `la` for Sparks and `las` for Aces.
            return TeamRef(upper, "la", WNBA_POLY_TO_NAME["la"])
        code = WNBA_BETTING_TO_POLY.get(upper)
        if code is None:
            code = _name_to_poly(raw, WNBA_POLY_TO_NAME)
        if code is None:
            return None
        return TeamRef(upper if upper in WNBA_BETTING_TO_POLY else raw, code, WNBA_POLY_TO_NAME[code])

    if league_l == "ufc":
        # Fighters have no Polymarket team codes; match later by full name.
        parts = raw.split()
        code = parts[-1].lower() if parts else raw.lower()
        return TeamRef(raw, code, raw)

    if league_l in {"ncaaf", "cfb"}:
        cfb = _cfb_team_map()
        name = cfb.canonical_name(raw) or raw
        abbr = cfb.canonical_abbr(raw) or upper
        code = cfb.poly_code(raw) or str(abbr).lower()
        return TeamRef(str(abbr), code, name)

    if league_l == "nfl":
        code = NFL_BETTING_TO_POLY.get(upper)
        if code is None:
            code = _name_to_poly(raw, NFL_POLY_TO_NAME)
        if code is None:
            return None
        return TeamRef(upper if upper in NFL_BETTING_TO_POLY else raw, code, NFL_POLY_TO_NAME[code])

    return None


def _name_to_poly(text: str, poly_to_name: dict[str, str]) -> str | None:
    key = " ".join(text.strip().lower().split())
    for code, name in poly_to_name.items():
        if name.lower() == key:
            return code
        # nickname match: "Diamondbacks", "Aces", …
        nick = name.lower().rsplit(" ", 1)[-1]
        if key == nick or key.endswith(" " + nick):
            return code
    return None


_MATCHUP_SPLIT = re.compile(r"\s*@\s*|\s+vs\.?\s+|\s+v\s+", re.IGNORECASE)


def parse_matchup(matchup: str) -> tuple[str, str] | None:
    """Parse `AZ @ ATL` / `AZ@ATL` / `AZ vs ATL` into (away, home) raw tokens."""
    parts = _MATCHUP_SPLIT.split(matchup.strip(), maxsplit=1)
    if len(parts) != 2:
        return None
    away, home = parts[0].strip(), parts[1].strip()
    if not away or not home:
        return None
    return away, home
