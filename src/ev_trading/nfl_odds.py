"""NFL odds join helpers: names, market-type maps, consensus, closest-alt pick."""

from __future__ import annotations

import re
import unicodedata
from collections.abc import Mapping, Sequence
from typing import Any

TEAM_ABBR_TO_NAME: dict[str, str] = {
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
    "KCC": "KC",
    "KC": "KC",
    "LVR": "LV",
    "LV": "LV",
    "NEP": "NE",
    "NE": "NE",
    "NOS": "NO",
    "NO": "NO",
    "SFO": "SF",
    "SF": "SF",
    "TBB": "TB",
    "TB": "TB",
}

ROTOWIRE_PROP_TO_CANONICAL: dict[str, str] = {
    "passyds": "passing_yards",
    "recyds": "receiving_yards",
    "rushyds": "rushing_yards",
    "rushrec": "rushing_receiving_yards",
    "anytd": "anytime_td",
    "firsttd": "first_td",
    "twotd": "2plus_td",
}

CANONICAL_PROP_TYPES: frozenset[str] = frozenset(ROTOWIRE_PROP_TO_CANONICAL.values())

YARDAGE_PROP_TYPES: frozenset[str] = frozenset(
    {
        "passing_yards",
        "receiving_yards",
        "rushing_yards",
        "rushing_receiving_yards",
    }
)

_PUNCT = re.compile(r"[^a-z0-9\s]+")
_SPACE = re.compile(r"\s+")
_PLAYER_SUFFIX = re.compile(r"\b(jr|sr|ii|iii|iv)\b", re.I)
_KALSHI_SUB = re.compile(r"^([A-Z]{2,3})\s+vs\s+([A-Z]{2,3})\b")
_SPREAD_SUB = re.compile(
    r"^(?P<team>.+?)\s+wins by over\s+(?P<line>\d+(?:\.\d+)?)\s+points",
    re.I,
)
_TOTAL_SUB = re.compile(r"^Over\s+(?P<line>\d+(?:\.\d+)?)\s+points", re.I)
_TICKER_TEAM = re.compile(r"-([A-Z]{2,3})(?:\d+)?$")


def normalize_text(value: str) -> str:
    text = unicodedata.normalize("NFKD", value or "")
    text = text.encode("ascii", "ignore").decode("ascii").lower()
    text = _PUNCT.sub(" ", text)
    return _SPACE.sub(" ", text).strip()


def canonical_abbr(raw: str) -> str:
    upper = (raw or "").strip().upper()
    return ABBR_ALIASES.get(upper, upper)


def game_key(away_abbr: str, home_abbr: str) -> tuple[str, str]:
    return canonical_abbr(away_abbr), canonical_abbr(home_abbr)


def team_name(abbr: str) -> str:
    return TEAM_ABBR_TO_NAME.get(canonical_abbr(abbr), abbr)


def normalize_player_name(name: str) -> str:
    text = normalize_text(name)
    text = _PLAYER_SUFFIX.sub("", text)
    return _SPACE.sub(" ", text).strip()


def player_initial_key(name: str) -> tuple[str, str] | None:
    parts = normalize_player_name(name).split()
    if len(parts) < 2 or not parts[0] or not parts[-1]:
        return None
    return parts[-1], parts[0][0]


def is_dst_name(name: str) -> bool:
    key = normalize_text(name)
    return key.endswith("d st") or key.endswith("dst") or " d st" in key


def parse_kalshi_sub_title(sub_title: str) -> tuple[str, str] | None:
    """'ATL vs PIT (Sep 13)' → ('ATL', 'PIT') away/home."""
    match = _KALSHI_SUB.match((sub_title or "").strip())
    if not match:
        return None
    return match.group(1), match.group(2)


def parse_kalshi_spread(yes_sub_title: str) -> tuple[str | None, float | None]:
    """'Pittsburgh wins by over 3.5 points' → ('Pittsburgh', 3.5)."""
    match = _SPREAD_SUB.match((yes_sub_title or "").strip())
    if not match:
        return None, None
    return match.group("team").strip(), float(match.group("line"))


def parse_kalshi_total(yes_sub_title: str) -> float | None:
    """'Over 44.5 points scored' → 44.5."""
    match = _TOTAL_SUB.match((yes_sub_title or "").strip())
    if not match:
        return None
    return float(match.group("line"))


def kalshi_ticker_team(ticker: str) -> str | None:
    """KXNFLGAME-...-PIT / KXNFLSPREAD-...-PIT4 → PIT. Totals like -21 → None."""
    match = _TICKER_TEAM.search(ticker or "")
    if not match:
        return None
    return match.group(1)


def _label_team_score(key: str, full_name: str, abbr: str) -> int:
    name = normalize_text(full_name)
    can = canonical_abbr(abbr)
    abbr_key = normalize_text(can)
    nick = name.split()[-1] if name else ""
    city = " ".join(name.split()[:-1]) if name else ""
    if key in {abbr_key, can.lower(), normalize_text(abbr)}:
        return 100
    if name and key == name:
        return 90
    if nick and key == nick:
        return 80
    if city and key == city:
        return 70
    if name and key and name.startswith(key) and len(key) >= 4:
        return 60
    if name and key and key.startswith(name) and len(name) >= 4:
        return 50
    return 0


def match_team_side(
    label: str,
    *,
    away_abbr: str,
    home_abbr: str,
    away_name: str = "",
    home_name: str = "",
    team_abbr: str | None = None,
) -> str | None:
    """Map a Kalshi team label onto away/home for a known game."""
    if team_abbr:
        can = canonical_abbr(team_abbr)
        if can == canonical_abbr(away_abbr):
            return "away"
        if can == canonical_abbr(home_abbr):
            return "home"
    key = normalize_text(label)
    if not key:
        return None
    away_score = _label_team_score(key, away_name or team_name(away_abbr), away_abbr)
    home_score = _label_team_score(key, home_name or team_name(home_abbr), home_abbr)
    if away_score > home_score and away_score > 0:
        return "away"
    if home_score > away_score and home_score > 0:
        return "home"
    return None


def median_line(values: Sequence[float]) -> float | None:
    nums = [float(v) for v in values]
    if not nums:
        return None
    nums.sort()
    mid = len(nums) // 2
    if len(nums) % 2:
        return nums[mid]
    return (nums[mid - 1] + nums[mid]) / 2.0


def pick_closest(
    alts: Sequence[Mapping[str, Any]],
    target: float,
    *,
    line_key: str = "line",
) -> Mapping[str, Any] | None:
    usable: list[tuple[float, Mapping[str, Any]]] = []
    for alt in alts:
        raw = alt.get(line_key)
        if raw is None:
            continue
        try:
            line = float(raw)
        except (TypeError, ValueError):
            continue
        usable.append((abs(line - target), alt))
    if not usable:
        return None
    usable.sort(key=lambda row: row[0])
    return usable[0][1]


def kalshi_mid(bid: Any, ask: Any) -> float | None:
    bid_f = _as_float(bid)
    ask_f = _as_float(ask)
    if bid_f is None and ask_f is None:
        return None
    if bid_f is None:
        return ask_f
    if ask_f is None:
        return bid_f
    return (bid_f + ask_f) / 2.0


def pick_closest_to_even(
    alts: Sequence[Mapping[str, Any]],
    *,
    mid_of,
) -> Mapping[str, Any] | None:
    best: Mapping[str, Any] | None = None
    best_dist: float | None = None
    for alt in alts:
        mid = mid_of(alt)
        if mid is None:
            continue
        dist = abs(float(mid) - 0.5)
        if best_dist is None or dist < best_dist:
            best = alt
            best_dist = dist
    return best


def american_to_implied_prob(odds: Any) -> float | None:
    value = _as_float(odds)
    if value is None:
        return None
    if value >= 0:
        return 100.0 / (value + 100.0)
    return abs(value) / (abs(value) + 100.0)


def kalshi_spread_home_line(
    market: Mapping[str, Any],
    *,
    away_abbr: str,
    home_abbr: str,
    away_name: str = "",
    home_name: str = "",
) -> float | None:
    """Convert a Kalshi margin contract into a sportsbook-style home spread."""
    margin = _as_float(market.get("line"))
    team = str(market.get("team") or "")
    if margin is None:
        parsed_team, parsed_line = parse_kalshi_spread(str(market.get("yes_sub_title") or ""))
        if parsed_team and not team:
            team = parsed_team
        margin = parsed_line
    if margin is None:
        return None
    team_abbr = market.get("team_abbr") or kalshi_ticker_team(str(market.get("ticker") or ""))
    side = match_team_side(
        team,
        away_abbr=away_abbr,
        home_abbr=home_abbr,
        away_name=away_name,
        home_name=home_name,
        team_abbr=str(team_abbr) if team_abbr else None,
    )
    if side == "home":
        return -float(margin)
    if side == "away":
        return float(margin)
    return None


def kalshi_total_line(market: Mapping[str, Any]) -> float | None:
    line = _as_float(market.get("line"))
    if line is not None:
        return line
    return parse_kalshi_total(str(market.get("yes_sub_title") or ""))


def _as_float(value: Any) -> float | None:
    if value is None or value == "":
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None
