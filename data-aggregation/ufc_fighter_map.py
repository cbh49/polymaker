"""Canonical UFC fighter names used by the betting-splits scrapers.

UFC has no stable team abbreviations. Sources also disagree on corner order
(DK "Dyer vs Reed" vs VSiN/TheSpread "Reed vs Dyer"), so matchups are keyed
as unordered pairs and sides are swapped on merge when needed.
"""

from __future__ import annotations

import re
from typing import Any

_WS = re.compile(r"\s+")
_NICK = re.compile(r'["“”\'].*?["“”\']')
_VS = re.compile(r"\s+vs\.?\s+", re.IGNORECASE)
_PARTICLES = {"de", "da", "dos", "das", "del", "della", "van", "von", "di", "la", "le", "st", "st."}

# Source-specific spelling quirks (normalized key -> display name).
NAME_ALIASES: dict[str, str] = {
    "sergey spivak": "Serghei Spivac",
    "serghei spivak": "Serghei Spivac",
}


def _norm_key(text: str) -> str:
    cleaned = _NICK.sub(" ", text or "")
    cleaned = cleaned.replace(".", " ")
    return _WS.sub(" ", cleaned.strip().lower())


def canonical_name(text: str) -> str | None:
    """Normalize a DK / VSiN / TheSpread / Polymarket fighter label."""
    raw = (text or "").strip()
    if not raw:
        return None
    key = _norm_key(raw)
    if not key:
        return None
    if key in NAME_ALIASES:
        return NAME_ALIASES[key]
    return _WS.sub(" ", raw.strip())


def last_name(text: str) -> str:
    parts = _norm_key(text).split()
    if not parts:
        return ""
    if len(parts) >= 2 and parts[-2] in _PARTICLES:
        return " ".join(parts[-2:])
    return parts[-1]


def first_token(text: str) -> str:
    parts = _norm_key(text).split()
    return parts[0] if parts else ""


def names_match(a: str | None, b: str | None) -> bool:
    if not a or not b:
        return False
    ca = canonical_name(a) or a
    cb = canonical_name(b) or b
    ka, kb = _norm_key(ca), _norm_key(cb)
    if not ka or not kb:
        return False
    if ka == kb:
        return True
    if ka in kb or kb in ka:
        return True
    la, lb = last_name(ca), last_name(cb)
    if not la or la != lb:
        return False
    fa, fb = first_token(ca), first_token(cb)
    if not fa or not fb:
        return True
    if fa == fb or fa.startswith(fb[0]) or fb.startswith(fa[0]):
        return True
    # Last-name-only labels ("Hernandez") match the full name.
    return ka == la or kb == lb


def pair_key(away: str | None, home: str | None) -> tuple[str, str] | None:
    if not away or not home:
        return None
    ca = canonical_name(away) or away
    cb = canonical_name(home) or home
    keys = tuple(sorted((_norm_key(ca), _norm_key(cb))))
    if not keys[0] or not keys[1] or keys[0] == keys[1]:
        return None
    return keys  # type: ignore[return-value]


def parse_vs_title(text: str) -> tuple[str, str] | None:
    raw = _WS.sub(" ", (text or "").strip())
    if not raw:
        return None
    parts = _VS.split(raw, maxsplit=1)
    if len(parts) != 2:
        return None
    away = canonical_name(parts[0])
    home = canonical_name(parts[1])
    if not away or not home:
        return None
    return away, home


def sides_swapped(
    dest_away: str | None,
    dest_home: str | None,
    src_away: str | None,
    src_home: str | None,
) -> bool | None:
    """True if src corners are reversed vs dest. None if the fights do not match."""
    same = names_match(dest_away, src_away) and names_match(dest_home, src_home)
    flipped = names_match(dest_away, src_home) and names_match(dest_home, src_away)
    if same:
        return False
    if flipped:
        return True
    return None


def swap_market_sides(block: dict[str, Any] | None) -> dict[str, Any] | None:
    """Swap away/home on a moneyline or spread market dict."""
    if not isinstance(block, dict):
        return block
    away = block.get("away")
    home = block.get("home")
    out = dict(block)
    if isinstance(home, dict) or home is None:
        out["away"] = home
    if isinstance(away, dict) or away is None:
        out["home"] = away
    return out


def align_game_to(src: dict[str, Any], dest: dict[str, Any]) -> dict[str, Any] | None:
    """Return a copy of src with away/home aligned to dest, or None if no match.

    UFC boards often put names and moneyline prices on opposite corners.
    After matching the bout by fighter names, moneyline/spread payloads are
    re-paired to dest using live (or vsin) American odds so open/live stay
    on the fighter they belong to.
    """
    swapped = sides_swapped(
        dest.get("away"),
        dest.get("home"),
        src.get("away"),
        src.get("home"),
    )
    if swapped is None:
        return None
    if not swapped:
        aligned = src
    else:
        aligned = dict(src)
        aligned["away"] = src.get("home")
        aligned["home"] = src.get("away")
        aligned["away_abbr"] = src.get("home_abbr") or aligned["away"]
        aligned["home_abbr"] = src.get("away_abbr") or aligned["home"]
        if src.get("away") and src.get("home"):
            aligned["matchup"] = f"{aligned['away']} vs {aligned['home']}"
        for market in ("moneyline", "spread"):
            aligned[market] = swap_market_sides(
                src.get(market) if isinstance(src.get(market), dict) else None
            )
    return realign_markets_by_price(aligned, dest)


def _side_american(side: Any, keys: tuple[str, ...] = ("live", "vsin_line")) -> float | None:
    if not isinstance(side, dict):
        return None
    for key in keys:
        val = side.get(key)
        if isinstance(val, (int, float)):
            return float(val)
    return None


def _implied_prob(odds: float) -> float:
    if odds >= 0:
        return 100.0 / (odds + 100.0)
    return abs(odds) / (abs(odds) + 100.0)


def _pair_distance(a: float, b: float) -> float:
    return abs(_implied_prob(a) - _implied_prob(b))


def moneyline_prices_are_flipped(src: dict[str, Any], dest: dict[str, Any]) -> bool:
    """True when src ML live prices match dest sides better after a swap."""
    src_ml = src.get("moneyline") if isinstance(src.get("moneyline"), dict) else None
    dest_ml = dest.get("moneyline") if isinstance(dest.get("moneyline"), dict) else None
    if not src_ml or not dest_ml:
        return False
    src_away = _side_american(src_ml.get("away"), ("live", "open", "vsin_line"))
    src_home = _side_american(src_ml.get("home"), ("live", "open", "vsin_line"))
    dest_away = _side_american(dest_ml.get("away"), ("live", "vsin_line"))
    dest_home = _side_american(dest_ml.get("home"), ("live", "vsin_line"))
    if None in (src_away, src_home, dest_away, dest_home):
        return False
    same = _pair_distance(src_away, dest_away) + _pair_distance(src_home, dest_home)
    flipped = _pair_distance(src_away, dest_home) + _pair_distance(src_home, dest_away)
    return flipped < same - 1e-9


def realign_markets_by_price(src: dict[str, Any], dest: dict[str, Any]) -> dict[str, Any]:
    """Swap src moneyline/spread if American odds fit dest corners better flipped."""
    if not moneyline_prices_are_flipped(src, dest):
        return src
    aligned = dict(src)
    for market in ("moneyline", "spread"):
        aligned[market] = swap_market_sides(
            src.get(market) if isinstance(src.get(market), dict) else None
        )
    return aligned
