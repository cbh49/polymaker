"""Canonical NCAAF / CFB school names and abbreviations.

Sources disagree on labels (DK "San Jose State", VSiN "San Jose ST Spartans",
SBD "Hawai'i" / HAW, Covers SJSU). Resolve to a stable abbr + school name so
splits merge, then fall back to fuzzy matching for FCS schools not in the map.
"""

from __future__ import annotations

import re
import unicodedata
from typing import Any

# Canonical betting abbr -> school name (not mascot).
ABBR_TO_NAME: dict[str, str] = {
    "AIR": "Air Force",
    "AKR": "Akron",
    "ALA": "Alabama",
    "APP": "Appalachian State",
    "ARIZ": "Arizona",
    "ARK": "Arkansas",
    "ARMY": "Army",
    "AUB": "Auburn",
    "BALL": "Ball State",
    "BAY": "Baylor",
    "BC": "Boston College",
    "BOIS": "Boise State",
    "BUFF": "Buffalo",
    "BYU": "BYU",
    "CAL": "California",
    "CCAR": "Coastal Carolina",
    "CHAR": "Charlotte",
    "CIN": "Cincinnati",
    "CLEM": "Clemson",
    "CMU": "Central Michigan",
    "COLO": "Colorado",
    "CONN": "UConn",
    "CSU": "Colorado State",
    "DEL": "Delaware",
    "DUKE": "Duke",
    "ECU": "East Carolina",
    "EMU": "Eastern Michigan",
    "FAU": "Florida Atlantic",
    "FIU": "FIU",
    "FLA": "Florida",
    "FRES": "Fresno State",
    "FSU": "Florida State",
    "GAST": "Georgia State",
    "GA": "Georgia",
    "GTECH": "Georgia Tech",
    "HAW": "Hawaii",
    "HOU": "Houston",
    "ILL": "Illinois",
    "IND": "Indiana",
    "IOWA": "Iowa",
    "ISU": "Iowa State",
    "JAXST": "Jacksonville State",
    "JMU": "James Madison",
    "KENT": "Kent State",
    "KENN": "Kennesaw State",
    "KU": "Kansas",
    "KSU": "Kansas State",
    "LIB": "Liberty",
    "LOU": "Louisville",
    "LSU": "LSU",
    "LT": "Louisiana Tech",
    "M-OH": "Miami (OH)",
    "MASS": "UMass",
    "MD": "Maryland",
    "MEM": "Memphis",
    "MIA": "Miami",
    "MICH": "Michigan",
    "MINN": "Minnesota",
    "MISS": "Ole Miss",
    "MIZ": "Missouri",
    "MOSU": "Missouri State",
    "MRSH": "Marshall",
    "MSST": "Mississippi State",
    "MSU": "Michigan State",
    "MTSU": "Middle Tennessee",
    "NAVY": "Navy",
    "NCST": "NC State",
    "ND": "Notre Dame",
    "NDSU": "North Dakota State",
    "NEB": "Nebraska",
    "NEV": "Nevada",
    "NIU": "Northern Illinois",
    "NMSU": "New Mexico State",
    "NW": "Northwestern",
    "OHIO": "Ohio",
    "OKLA": "Oklahoma",
    "OKST": "Oklahoma State",
    "ORE": "Oregon",
    "ORST": "Oregon State",
    "OSU": "Ohio State",
    "PITT": "Pittsburgh",
    "PSU": "Penn State",
    "PUR": "Purdue",
    "RICE": "Rice",
    "RUTG": "Rutgers",
    "SAC": "Sacramento State",
    "SC": "South Carolina",
    "SDSU": "San Diego State",
    "SHSU": "Sam Houston",
    "SJSU": "San Jose State",
    "SMU": "SMU",
    "STAN": "Stanford",
    "SYR": "Syracuse",
    "TA&M": "Texas A&M",
    "TCU": "TCU",
    "TEM": "Temple",
    "TENN": "Tennessee",
    "TEX": "Texas",
    "TLSA": "Tulsa",
    "TROY": "Troy",
    "TTU": "Texas Tech",
    "TULN": "Tulane",
    "TXST": "Texas State",
    "UAB": "UAB",
    "UCF": "UCF",
    "UCLA": "UCLA",
    "UK": "Kentucky",
    "UL": "Louisiana",
    "ULM": "UL Monroe",
    "UMASS": "UMass",
    "UNC": "North Carolina",
    "UNLV": "UNLV",
    "UNM": "New Mexico",
    "UNT": "North Texas",
    "USA": "South Alabama",
    "USC": "USC",
    "USF": "South Florida",
    "USM": "Southern Miss",
    "USU": "Utah State",
    "UTAH": "Utah",
    "UTEP": "UTEP",
    "UTSA": "UTSA",
    "UVA": "Virginia",
    "VAN": "Vanderbilt",
    "VT": "Virginia Tech",
    "WAKE": "Wake Forest",
    "WASH": "Washington",
    "WISC": "Wisconsin",
    "WKU": "Western Kentucky",
    "WMU": "Western Michigan",
    "WSU": "Washington State",
    "WVU": "West Virginia",
    "WYO": "Wyoming",
}

# Alternate codes that show up on DK, VSiN, SBD, EVA, Covers.
ABBR_ALIASES: dict[str, str] = {
    "AF": "AIR",
    "AFA": "AIR",
    "ALAB": "ALA",
    "APPST": "APP",
    "ARIZONA": "ARIZ",
    "ARKANSAS": "ARK",
    "AUBURN": "AUB",
    "BALLST": "BALL",
    "BC": "BC",
    "BOISE": "BOIS",
    "BSU": "BOIS",
    "CAL": "CAL",
    "CCU": "CCAR",
    "CMICH": "CMU",
    "COLOST": "CSU",
    "CSUS": "SAC",
    "EMU": "EMU",
    "EMICH": "EMU",
    "FAU": "FAU",
    "FSU": "FSU",
    "GT": "GTECH",
    "GATECH": "GTECH",
    "HAW": "HAW",
    "HAWAII": "HAW",
    "UH": "HAW",
    "ISU": "ISU",
    "IOWAST": "ISU",
    "JVST": "JAXST",
    "JAXST": "JAXST",
    "KENNESAW": "KENN",
    "KSU": "KSU",
    "KANST": "KSU",
    "LSU": "LSU",
    "MIAMI": "MIA",
    "MIAFL": "MIA",
    "MIAOH": "M-OH",
    "MICHST": "MSU",
    "MISSST": "MSST",
    "MOST": "MOSU",
    "NCST": "NCST",
    "NCSU": "NCST",
    "NDSU": "NDSU",
    "NMSU": "NMSU",
    "OKST": "OKST",
    "ORST": "ORST",
    "OHIOST": "OSU",
    "PSU": "PSU",
    "PENNST": "PSU",
    "RUTG": "RUTG",
    "RUTGER": "RUTG",
    "SACST": "SAC",
    "SDSU": "SDSU",
    "SJSU": "SJSU",
    "STAN": "STAN",
    "STNFRD": "STAN",
    "TAMU": "TA&M",
    "TA&M": "TA&M",
    "TEXAM": "TA&M",
    "TCU": "TCU",
    "TENN": "TENN",
    "TXST": "TXST",
    "UCONN": "CONN",
    "UGA": "GA",
    "UCLA": "UCLA",
    "UK": "UK",
    "ULL": "UL",
    "UL-L": "UL",
    "UMASS": "MASS",
    "MASS": "MASS",
    "UNC": "UNC",
    "UNLV": "UNLV",
    "USC": "USC",
    "USF": "USF",
    "USM": "USM",
    "UT": "TEX",
    "UVA": "UVA",
    "VA": "UVA",
    "VT": "VT",
    "VTECH": "VT",
    "WAKE": "WAKE",
    "WASH": "WASH",
    "WIS": "WISC",
    "WKU": "WKU",
    "WMU": "WMU",
    "WSU": "WSU",
    "WVU": "WVU",
}

NAME_ALIASES: dict[str, str] = {
    "air force": "Air Force",
    "alabama": "Alabama",
    "appalachian state": "Appalachian State",
    "app state": "Appalachian State",
    "arizona": "Arizona",
    "arkansas": "Arkansas",
    "army": "Army",
    "army west point": "Army",
    "auburn": "Auburn",
    "ball state": "Ball State",
    "baylor": "Baylor",
    "boston college": "Boston College",
    "boise state": "Boise State",
    "buffalo": "Buffalo",
    "byu": "BYU",
    "brigham young": "BYU",
    "california": "California",
    "cal": "California",
    "coastal carolina": "Coastal Carolina",
    "charlotte": "Charlotte",
    "cincinnati": "Cincinnati",
    "clemson": "Clemson",
    "central michigan": "Central Michigan",
    "colorado": "Colorado",
    "uconn": "UConn",
    "connecticut": "UConn",
    "colorado state": "Colorado State",
    "delaware": "Delaware",
    "duke": "Duke",
    "east carolina": "East Carolina",
    "eastern michigan": "Eastern Michigan",
    "e michigan": "Eastern Michigan",
    "e michigan eagles": "Eastern Michigan",
    "florida atlantic": "Florida Atlantic",
    "fiu": "FIU",
    "florida international": "FIU",
    "florida": "Florida",
    "fresno state": "Fresno State",
    "florida state": "Florida State",
    "florida st": "Florida State",
    "florida st seminoles": "Florida State",
    "georgia state": "Georgia State",
    "georgia": "Georgia",
    "georgia tech": "Georgia Tech",
    "hawaii": "Hawaii",
    "hawai'i": "Hawaii",
    "hawai i": "Hawaii",
    "hawaii rainbow warriors": "Hawaii",
    "houston": "Houston",
    "illinois": "Illinois",
    "indiana": "Indiana",
    "iowa": "Iowa",
    "iowa state": "Iowa State",
    "jacksonville state": "Jacksonville State",
    "jacksonville st": "Jacksonville State",
    "james madison": "James Madison",
    "kent state": "Kent State",
    "kennesaw state": "Kennesaw State",
    "kansas": "Kansas",
    "kansas state": "Kansas State",
    "liberty": "Liberty",
    "louisville": "Louisville",
    "lsu": "LSU",
    "louisiana tech": "Louisiana Tech",
    "miami oh": "Miami (OH)",
    "miami (oh)": "Miami (OH)",
    "miami ohio": "Miami (OH)",
    "umass": "UMass",
    "massachusetts": "UMass",
    "maryland": "Maryland",
    "memphis": "Memphis",
    "miami": "Miami",
    "miami fl": "Miami",
    "miami (fl)": "Miami",
    "michigan": "Michigan",
    "minnesota": "Minnesota",
    "ole miss": "Ole Miss",
    "mississippi": "Ole Miss",
    "missouri": "Missouri",
    "missouri state": "Missouri State",
    "marshall": "Marshall",
    "mississippi state": "Mississippi State",
    "michigan state": "Michigan State",
    "middle tennessee": "Middle Tennessee",
    "navy": "Navy",
    "nc state": "NC State",
    "n.c. state": "NC State",
    "north carolina state": "NC State",
    "nc state wolfpack": "NC State",
    "notre dame": "Notre Dame",
    "north dakota state": "North Dakota State",
    "n dakota st": "North Dakota State",
    "n dakota state": "North Dakota State",
    "nebraska": "Nebraska",
    "nevada": "Nevada",
    "northern illinois": "Northern Illinois",
    "new mexico state": "New Mexico State",
    "new mexico st": "New Mexico State",
    "new mexico st aggies": "New Mexico State",
    "northwestern": "Northwestern",
    "ohio": "Ohio",
    "oklahoma": "Oklahoma",
    "oklahoma state": "Oklahoma State",
    "oregon": "Oregon",
    "oregon state": "Oregon State",
    "ohio state": "Ohio State",
    "pittsburgh": "Pittsburgh",
    "pitt": "Pittsburgh",
    "penn state": "Penn State",
    "purdue": "Purdue",
    "rice": "Rice",
    "rutgers": "Rutgers",
    "sacramento state": "Sacramento State",
    "sacramento st": "Sacramento State",
    "south carolina": "South Carolina",
    "san diego state": "San Diego State",
    "sam houston": "Sam Houston",
    "sam houston state": "Sam Houston",
    "san jose state": "San Jose State",
    "san josé state": "San Jose State",
    "san jose st": "San Jose State",
    "san jose st spartans": "San Jose State",
    "smu": "SMU",
    "stanford": "Stanford",
    "syracuse": "Syracuse",
    "texas a&m": "Texas A&M",
    "texas a and m": "Texas A&M",
    "tcu": "TCU",
    "tcu horned frogs": "TCU",
    "temple": "Temple",
    "tennessee": "Tennessee",
    "texas": "Texas",
    "tulsa": "Tulsa",
    "troy": "Troy",
    "texas tech": "Texas Tech",
    "tulane": "Tulane",
    "texas state": "Texas State",
    "uab": "UAB",
    "ucf": "UCF",
    "ucla": "UCLA",
    "kentucky": "Kentucky",
    "louisiana": "Louisiana",
    "louisiana lafayette": "Louisiana",
    "ul monroe": "UL Monroe",
    "ulm": "UL Monroe",
    "north carolina": "North Carolina",
    "north carolina tar heels": "North Carolina",
    "unlv": "UNLV",
    "new mexico": "New Mexico",
    "north texas": "North Texas",
    "south alabama": "South Alabama",
    "usc": "USC",
    "usc trojans": "USC",
    "south florida": "South Florida",
    "southern miss": "Southern Miss",
    "southern mississippi": "Southern Miss",
    "utah state": "Utah State",
    "utah": "Utah",
    "utep": "UTEP",
    "utsa": "UTSA",
    "virginia": "Virginia",
    "virginia cavaliers": "Virginia",
    "vanderbilt": "Vanderbilt",
    "virginia tech": "Virginia Tech",
    "wake forest": "Wake Forest",
    "washington": "Washington",
    "wisconsin": "Wisconsin",
    "western kentucky": "Western Kentucky",
    "western michigan": "Western Michigan",
    "washington state": "Washington State",
    "west virginia": "West Virginia",
    "wyoming": "Wyoming",
}

# Polymarket event-slug tokens (cfb-{away}-{home}-YYYY-MM-DD) when known.
POLY_CODE: dict[str, str] = {
    "HAW": "hawaii",
    "STAN": "stan",
    "TCU": "tcu",
    "UNC": "unc",
    "SJSU": "sjsu",
    "USC": "usc",
    "NCST": "ncst",
    "UVA": "uva",
    "FSU": "fsu",
    "NMSU": "nmsu",
    "EMU": "emu",
    "MEM": "mem",
    "UNLV": "unlv",
    "MASS": "umass",
    "RUTG": "rutg",
    "AKR": "akr",
    "WAKE": "wake",
    "JAXST": "jaxst",
    "NDSU": "ndsu",
    "SAC": "sacst",
}

_GENERIC = frozenset(
    {
        "state",
        "univ",
        "university",
        "college",
        "tech",
        "a&m",
        "am",
        "st",
    }
)
_MASCOTS = frozenset(
    {
        "aggies",
        "bearcats",
        "bears",
        "bison",
        "broncos",
        "bruins",
        "buckeyes",
        "bulldogs",
        "cardinal",
        "cardinals",
        "cavaliers",
        "crimson",
        "cougars",
        "cowboys",
        "cyclones",
        "demon",
        "deacons",
        "ducks",
        "eagles",
        "falcons",
        "frogs",
        "gators",
        "hawkeyes",
        "hokies",
        "hurricanes",
        "husky",
        "huskies",
        "jayhawks",
        "longhorns",
        "lions",
        "mean",
        "green",
        "nittany",
        "owls",
        "panthers",
        "rainbow",
        "warriors",
        "razorbacks",
        "rebels",
        "red",
        "raiders",
        "seminoles",
        "spartans",
        "tar",
        "heels",
        "tigers",
        "trojans",
        "utes",
        "volunteers",
        "wolfpack",
        "wolverines",
        "horned",
    }
)
_COMPASS = {"n": "north", "s": "south", "e": "east", "w": "west", "c": "central"}
_WS = re.compile(r"\s+")


def _fold(text: str) -> str:
    nfkd = unicodedata.normalize("NFKD", text or "")
    ascii_text = nfkd.encode("ascii", "ignore").decode("ascii")
    ascii_text = ascii_text.replace("&", " and ").replace("'", "").replace("’", "")
    ascii_text = re.sub(r"[^a-zA-Z0-9+ ]+", " ", ascii_text)
    return _WS.sub(" ", ascii_text).strip().lower()


def _expand_school_tokens(key: str) -> str:
    parts = key.split()
    out: list[str] = []
    for i, part in enumerate(parts):
        if part in _COMPASS and i + 1 < len(parts) and parts[i + 1] not in _GENERIC:
            out.append(_COMPASS[part])
        elif part in {"st", "st."} and i > 0:
            out.append("state")
        else:
            out.append(part)
    return " ".join(out)


def _strip_mascot(key: str) -> str:
    parts = key.split()
    while parts and parts[-1] in _MASCOTS:
        parts.pop()
    return " ".join(parts) if parts else key


def _norm_key(text: str) -> str:
    return _strip_mascot(_expand_school_tokens(_fold(text)))


def _build_name_to_abbr() -> dict[str, str]:
    mapping: dict[str, str] = {}
    for abbr, name in ABBR_TO_NAME.items():
        mapping[_norm_key(name)] = abbr
        mapping.setdefault(_fold(name), abbr)
    full_to_abbr = {_norm_key(name): abbr for abbr, name in ABBR_TO_NAME.items()}
    for alias, name in NAME_ALIASES.items():
        mapping[_norm_key(alias)] = full_to_abbr.get(_norm_key(name), "")
        mapping[_fold(alias)] = full_to_abbr.get(_norm_key(name), "")
    return {k: v for k, v in mapping.items() if k and v}


NAME_TO_ABBR = _build_name_to_abbr()


def canonical_name(text: str) -> str | None:
    """Resolve a DK / VSiN / SBD / TheSpread / EVA / Covers label to a school name."""
    raw = (text or "").strip()
    if not raw:
        return None
    upper = re.sub(r"[^A-Za-z0-9&+]", "", raw).upper()
    if upper in ABBR_ALIASES:
        return ABBR_TO_NAME[ABBR_ALIASES[upper]]
    if upper in ABBR_TO_NAME:
        return ABBR_TO_NAME[upper]
    key = _norm_key(raw)
    folded = _fold(raw)
    if folded in NAME_ALIASES:
        return NAME_ALIASES[folded]
    if key in NAME_ALIASES:
        return NAME_ALIASES[key]
    abbr = NAME_TO_ABBR.get(key) or NAME_TO_ABBR.get(folded)
    if abbr:
        return ABBR_TO_NAME[abbr]
    cleaned = _strip_mascot(_expand_school_tokens(_fold(raw)))
    return cleaned.title() if cleaned else raw


def canonical_abbr(text: str) -> str | None:
    raw = (text or "").strip()
    if not raw:
        return None
    upper = re.sub(r"[^A-Za-z0-9&+]", "", raw).upper()
    if upper in ABBR_ALIASES:
        return ABBR_ALIASES[upper]
    if upper in ABBR_TO_NAME:
        return upper
    name = canonical_name(raw)
    if not name:
        return None
    mapped = NAME_TO_ABBR.get(_norm_key(name)) or NAME_TO_ABBR.get(_fold(name))
    if mapped:
        return mapped
    # Unknown FCS codes (STON, DSU, …) must stay uppercase so DK/SBD/VSiN keys match.
    if re.fullmatch(r"[A-Z0-9]{2,6}", upper):
        return upper
    return None


def poly_code(text: str) -> str | None:
    abbr = canonical_abbr(text)
    if not abbr:
        return None
    if abbr in POLY_CODE:
        return POLY_CODE[abbr]
    if abbr in ABBR_TO_NAME:
        return abbr.lower().replace("&", "")
    return _fold(str(abbr)).replace(" ", "") or None


def names_match(a: str | None, b: str | None) -> bool:
    if not a or not b:
        return False
    ca, cb = canonical_name(a) or a, canonical_name(b) or b
    aa, ab = canonical_abbr(a), canonical_abbr(b)
    if aa and ab and aa == ab and aa in ABBR_TO_NAME:
        return True
    ka, kb = _norm_key(ca), _norm_key(cb)
    if not ka or not kb:
        return False
    if ka == kb or _fold(ca) == _fold(cb):
        return True
    if ka in kb or kb in ka:
        return True
    a_last, b_last = ka.split()[-1], kb.split()[-1]
    if (
        a_last == b_last
        and a_last not in _GENERIC
        and len(a_last) > 3
        and (len(ka.split()) == 1 or len(kb.split()) == 1 or ka.split()[0] == kb.split()[0])
    ):
        return True
    return False


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
