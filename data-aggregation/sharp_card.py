#!/usr/bin/env python3
"""PNG card for an A+ sharp-money post. Matches the site card, without tier or book names."""

from __future__ import annotations

import json
import re
import unicodedata
import urllib.request
from pathlib import Path
from typing import Any

from PIL import Image, ImageDraw, ImageFont

from discord_sharp_alerts import (
    _as_float,
    _format_american,
    _format_number_line,
    _matchup_line,
    format_game_start,
    resolve_play_label,
)

SCRIPT_DIR = Path(__file__).resolve().parent
BOT_ROOT = SCRIPT_DIR.parent
PUBLIC_DIR = BOT_ROOT / "public"
LOGO_CACHE = SCRIPT_DIR / "output" / ".sharp-logo-cache"

W, H = 800, 1200
BG = (17, 22, 29)
HEADER = (30, 42, 58)
HEADER_LINE = (58, 76, 98)
PANEL = (24, 33, 46)
PANEL_LINE = (64, 82, 104)
WHITE = (244, 247, 251)
MUTED = (139, 149, 165)
GREEN = (74, 222, 128)
GREEN_DIM = (14, 42, 32)
GREEN_LINE = (47, 122, 78)
BLUE = (96, 165, 250)
TRACK = (26, 33, 43)
AXIS = (42, 50, 62)
PILL = (28, 36, 48)
FOOT = (26, 26, 26)

LEAGUE_COLOR = {
    "NFL": (134, 239, 172),
    "NCAAF": (253, 230, 138),
    "CFB": (253, 230, 138),
    "MLB": (147, 197, 253),
    "WNBA": (249, 168, 212),
}

MLB_SLUG = {
    "ARI": "ari",
    "ATL": "atl",
    "BAL": "bal",
    "BOS": "bos",
    "CHC": "chc",
    "CWS": "chw",
    "CIN": "cin",
    "CLE": "cle",
    "COL": "col",
    "DET": "det",
    "HOU": "hou",
    "KC": "kc",
    "LAA": "laa",
    "LAD": "lad",
    "MIA": "mia",
    "MIL": "mil",
    "MIN": "min",
    "NYM": "nym",
    "NYY": "nyy",
    "ATH": "ath",
    "PHI": "phi",
    "PIT": "pit",
    "SD": "sd",
    "SF": "sf",
    "SEA": "sea",
    "STL": "stl",
    "TB": "tb",
    "TEX": "tex",
    "TOR": "tor",
    "WSH": "wsh",
}

WNBA_SLUG = {
    "ATL": "atl",
    "CHI": "chi",
    "CON": "con",
    "DAL": "dal",
    "GS": "gs",
    "IND": "ind",
    "LA": "la",
    "LV": "lv",
    "MIN": "min",
    "NY": "ny",
    "PHX": "phx",
    "POR": "por",
    "SEA": "sea",
    "TOR": "tor",
    "WAS": "was",
}

DM_SANS = PUBLIC_DIR / "fonts" / "DMSans.ttf"
FONT_BOLD = (
    "/System/Library/Fonts/Supplemental/Arial Bold.ttf",
    "/usr/share/fonts/truetype/liberation/LiberationSans-Bold.ttf",
    "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
)
FONT_REG = (
    "/System/Library/Fonts/Supplemental/Arial.ttf",
    "/usr/share/fonts/truetype/liberation/LiberationSans-Regular.ttf",
    "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
)

_LOGO_INDEX: dict[str, Any] | None = None


def _load_font(size: int, *, bold: bool = True) -> Any:
    """DM Sans, same family as the site. Weight 700 for emphasis, 500 otherwise."""
    weight = 700 if bold else 500
    if DM_SANS.is_file():
        try:
            font = ImageFont.truetype(str(DM_SANS), size=size)
            font.set_variation_by_axes([max(9, min(40, size)), weight])
            return font
        except OSError:
            pass
    for path in FONT_BOLD if bold else (*FONT_REG, *FONT_BOLD):
        if not Path(path).exists():
            continue
        try:
            return ImageFont.truetype(path, size=size)
        except OSError:
            continue
    return ImageFont.load_default()


def _text_size(font: Any, text: str) -> tuple[int, int]:
    bbox = font.getbbox(text)
    return bbox[2] - bbox[0], bbox[3] - bbox[1]


def _fold(value: str) -> str:
    text = unicodedata.normalize("NFD", value or "")
    text = "".join(ch for ch in text if unicodedata.category(ch) != "Mn")
    return re.sub(r"[^a-z0-9]+", " ", text.lower()).strip()


def _hex_rgb(value: str | None) -> tuple[int, int, int]:
    raw = str(value or "").strip().lstrip("#")
    if len(raw) == 3:
        raw = "".join(ch * 2 for ch in raw)
    if len(raw) != 6:
        return (61, 156, 240)
    try:
        return (int(raw[0:2], 16), int(raw[2:4], 16), int(raw[4:6], 16))
    except ValueError:
        return (61, 156, 240)


def _luma(rgb: tuple[int, int, int]) -> float:
    r, g, b = rgb
    return (0.2126 * r + 0.7152 * g + 0.0722 * b) / 255


def _ring_color(rgb: tuple[int, int, int]) -> tuple[int, int, int]:
    if _luma(rgb) < 0.18 or _luma(rgb) > 0.92:
        return (213, 220, 230)
    return rgb


def _logo_index() -> dict[str, Any]:
    global _LOGO_INDEX
    if _LOGO_INDEX is not None:
        return _LOGO_INDEX
    nfl: dict[str, dict[str, str]] = {}
    nfl_path = PUBLIC_DIR / "nfl-team-logos.json"
    if nfl_path.is_file():
        raw = json.loads(nfl_path.read_text(encoding="utf-8"))
        if isinstance(raw, dict):
            for name, entry in raw.items():
                if not isinstance(entry, dict):
                    continue
                nfl[_fold(name)] = {
                    "name": name,
                    "url": str(entry.get("logo_url") or ""),
                    "color": str(entry.get("primary_color") or ""),
                }
                abbr = str(entry.get("abbr") or "").upper()
                if abbr:
                    nfl[abbr.lower()] = nfl[_fold(name)]
    cfb: dict[str, dict[str, str]] = {}
    cfb_path = PUBLIC_DIR / "cfb-logos.json"
    if cfb_path.is_file():
        raw = json.loads(cfb_path.read_text(encoding="utf-8"))
        if isinstance(raw, dict):
            for name, entry in raw.items():
                if not isinstance(entry, dict) or not entry.get("logo"):
                    continue
                cfb[_fold(name)] = {
                    "name": name,
                    "url": str(entry.get("logo") or ""),
                    "color": str(entry.get("color") or ""),
                }
    _LOGO_INDEX = {"nfl": nfl, "cfb": cfb}
    return _LOGO_INDEX


def _lookup_named(table: dict[str, dict[str, str]], team: str) -> dict[str, str] | None:
    key = _fold(team)
    if not key:
        return None
    if key in table:
        return table[key]
    parts = key.split()
    nick = parts[-1] if parts else ""
    if nick and nick != key and nick in table:
        return table[nick]
    for fold_key, row in table.items():
        if len(key) >= 4 and (fold_key == key or fold_key.startswith(key + " ") or key.startswith(fold_key + " ")):
            return row
    return None


def _pro_mark(team: str, slug_map: dict[str, str], prefix: str) -> dict[str, str] | None:
    try:
        if prefix == "mlb":
            from mlb_team_map import ABBR_TO_NAME, canonical_abbr
        else:
            from wnba_team_map import ABBR_TO_NAME, canonical_abbr
    except Exception:
        return None
    abbr = ""
    try:
        abbr = str(canonical_abbr(team) or "").upper()
    except Exception:
        abbr = ""
    folded = _fold(team)
    if not abbr:
        for code, name in ABBR_TO_NAME.items():
            if _fold(name) == folded or folded == code.lower():
                abbr = code
                break
    slug = slug_map.get(abbr)
    if not slug:
        return None
    folder = "mlb" if prefix == "mlb" else "wnba"
    style = "" if prefix == "mlb" else "500-dark/"
    # mlb path is /500/{slug}; wnba path is /500-dark/{slug}
    if prefix == "mlb":
        url = f"https://a.espncdn.com/i/teamlogos/mlb/500/{slug}.png"
    else:
        url = f"https://a.espncdn.com/i/teamlogos/{folder}/{style}{slug}.png"
    return {"name": ABBR_TO_NAME.get(abbr, team), "url": url, "color": ""}


def team_mark(team: str, league: str) -> dict[str, str] | None:
    """Logo URL + color for a team name. None when we have no artwork."""
    code = (league or "").strip().upper()
    if code == "CFB":
        code = "NCAAF"
    index = _logo_index()
    if code == "NFL":
        return _lookup_named(index["nfl"], team)
    if code == "NCAAF":
        return _lookup_named(index["cfb"], team)
    if code == "MLB":
        return _pro_mark(team, MLB_SLUG, "mlb")
    if code == "WNBA":
        return _pro_mark(team, WNBA_SLUG, "wnba")
    return (
        _lookup_named(index["nfl"], team)
        or _lookup_named(index["cfb"], team)
        or _pro_mark(team, MLB_SLUG, "mlb")
        or _pro_mark(team, WNBA_SLUG, "wnba")
    )


def _http_get(url: str) -> bytes:
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0 (BretonSharpCard)"})
    with urllib.request.urlopen(req, timeout=20) as resp:
        return bytes(resp.read())


def _load_logo(mark: dict[str, str] | None, *, fetch: bool) -> Image.Image | None:
    if not mark or not fetch:
        return None
    url = str(mark.get("url") or "").strip()
    if not url:
        return None
    slug = re.sub(r"[^a-z0-9]+", "-", _fold(mark.get("name") or "team")) or "team"
    LOGO_CACHE.mkdir(parents=True, exist_ok=True)
    cache_path = LOGO_CACHE / f"{slug}.png"
    if not cache_path.is_file():
        try:
            cache_path.write_bytes(_http_get(url))
        except Exception:
            return None
    try:
        return Image.open(cache_path).convert("RGBA")
    except Exception:
        return None


def _side_name(play: dict[str, Any]) -> str:
    home_away = str(play.get("home_away") or "").strip().lower()
    if home_away == "away":
        return str(play.get("away_team") or play.get("side") or "Away")
    if home_away == "home":
        return str(play.get("home_team") or play.get("side") or "Home")
    if home_away in {"over", "under"}:
        return home_away.capitalize()
    return str(play.get("side") or "Play")


def _market_label(market: str) -> str:
    key = market.strip().lower()
    if key == "moneyline":
        return "ML"
    if key == "spread":
        return "SPREAD"
    if key == "total":
        return "TOTAL"
    return key.upper() or "BET"


def _format_line(market: str, number: Any, juice: Any) -> str:
    if market == "moneyline":
        return _format_american(_as_float(number)) or "—"
    signed = market == "spread"
    body = _format_number_line(_as_float(number), signed=signed)
    odds = _format_american(_as_float(juice))
    if not body:
        return odds or "—"
    return f"{body} ({odds})" if odds else body


def _move_note(play: dict[str, Any], market: str) -> str | None:
    move = _as_float(play.get("line_move"))
    opened = _as_float(play.get("open"))
    live = _as_float(play.get("live"))
    if move is None or opened is None or live is None:
        return None
    name = _side_name(play)
    if market == "moneyline":
        return f"Moved toward {name}"
    pretty = _format_number_line(abs(move), signed=False) or f"{abs(move):g}"
    return f"Moved {pretty} toward {name}"


def card_copy(play: dict[str, Any], *, league: str | None = None) -> dict[str, Any]:
    """Strings drawn on the card. No tier badge and no sportsbook names."""
    market = str(play.get("market") or "moneyline").strip().lower()
    league_s = str(league or play.get("league") or "").strip().upper()
    if league_s == "CFB":
        league_s = "NCAAF"
    label = resolve_play_label(play)
    odds = _format_american(_as_float(play.get("play_odds")))
    if market == "moneyline" or not odds:
        bet = label
    else:
        bet = f"{label}  {odds}"
    public_name = str(play.get("public_favors_name") or "").strip() or "Public"
    public_pct = _as_float(play.get("public_favors_bet_pct"))
    sharp_pub = _as_float(play.get("public_bet_pct"))
    if public_pct is None and sharp_pub is not None:
        public_pct = 100 - sharp_pub
    handle_pct = _as_float(play.get("handle_bet_pct"))
    handle_name = _side_name(play)
    steam = None
    if handle_pct is not None and sharp_pub is not None:
        steam = handle_pct - sharp_pub
    now_juice = play.get("live_odds")
    if now_juice is None:
        now_juice = play.get("play_odds")
    return {
        "matchup": _matchup_line(play),
        "league": league_s,
        "market": _market_label(market),
        "bet": bet,
        "public_name": public_name,
        "public_pct": public_pct,
        "handle_name": handle_name,
        "handle_pct": handle_pct,
        "steam": steam,
        "open_text": _format_line(market, play.get("open"), play.get("open_odds")),
        "now_text": _format_line(market, play.get("live"), now_juice),
        "move_note": _move_note(play, market),
        "starts": format_game_start(play),
        "open_value": _as_float(play.get("open")),
        "live_value": _as_float(play.get("live")),
        "line_move": _as_float(play.get("line_move")),
    }


def _fit_image(img: Image.Image, box: int) -> Image.Image:
    src = img.convert("RGBA")
    src.thumbnail((box, box), Image.Resampling.LANCZOS)
    canvas = Image.new("RGBA", (box, box), (0, 0, 0, 0))
    canvas.alpha_composite(src, ((box - src.width) // 2, (box - src.height) // 2))
    return canvas


def _paste(base: Image.Image, overlay: Image.Image, xy: tuple[int, int]) -> None:
    if overlay.mode != "RGBA":
        overlay = overlay.convert("RGBA")
    layer = Image.new("RGBA", base.size, (0, 0, 0, 0))
    layer.alpha_composite(overlay, xy)
    composed = Image.alpha_composite(base.convert("RGBA"), layer)
    base.paste(composed.convert("RGB"))


def _text(draw: ImageDraw.ImageDraw, xy: tuple[float, float], text: str, *, font: Any, fill: tuple[int, int, int]) -> None:
    draw.text(xy, text, font=font, fill=fill, anchor="lt")


def _ellipsize(font: Any, text: str, max_w: int) -> str:
    if _text_size(font, text)[0] <= max_w:
        return text
    trimmed = text
    while trimmed and _text_size(font, trimmed + "…")[0] > max_w:
        trimmed = trimmed[:-1]
    return (trimmed + "…") if trimmed else "…"


def _rounded(draw: ImageDraw.ImageDraw, box: tuple[int, int, int, int], radius: int, fill: tuple[int, int, int]) -> None:
    draw.rounded_rectangle(box, radius=radius, fill=fill)


def _panel(draw: ImageDraw.ImageDraw, box: tuple[int, int, int, int]) -> None:
    draw.rounded_rectangle(box, radius=18, fill=PANEL)
    draw.rounded_rectangle(box, radius=18, outline=PANEL_LINE, width=2)


def _draw_mark(
    base: Image.Image,
    draw: ImageDraw.ImageDraw,
    team: str,
    league: str,
    xy: tuple[int, int],
    size: int,
    *,
    ring: bool,
    fetch_logos: bool,
) -> None:
    mark = team_mark(team, league)
    color = _hex_rgb(mark.get("color") if mark else None)
    logo = _load_logo(mark, fetch=fetch_logos)
    x, y = xy
    if ring:
        pad = 6
        draw.ellipse(
            (x - pad, y - pad, x + size + pad, y + size + pad),
            outline=_ring_color(color),
            width=5,
        )
    if logo is not None:
        _paste(base, _fit_image(logo, size), (x, y))
        return
    _rounded(draw, (x, y, x + size, y + size), 18, (26, 33, 43))
    letters = "".join(part[0] for part in team.split() if part)[:3].upper() or "?"
    font = _load_font(28 if len(letters) > 2 else 34, bold=True)
    tw, th = _text_size(font, letters)
    _text(draw, (x + (size - tw) / 2, y + (size - th) / 2 - 4), letters, font=font, fill=WHITE)


def _bar(
    draw: ImageDraw.ImageDraw,
    *,
    x: int,
    y: int,
    width: int,
    label: str,
    name: str,
    pct: float | None,
    fill: tuple[int, int, int],
) -> int:
    label_font = _load_font(18, bold=True)
    name_font = _load_font(26, bold=True)
    pct_font = _load_font(28, bold=True)
    pct_text = "—" if pct is None else f"{round(pct)}%"
    pw, _ = _text_size(pct_font, pct_text)
    _text(draw, (x, y), label, font=label_font, fill=MUTED)
    name_x = x + 118
    name_w = width - 118 - pw - 16
    _text(draw, (name_x, y - 4), _ellipsize(name_font, name, name_w), font=name_font, fill=WHITE)
    _text(draw, (x + width - pw, y - 6), pct_text, font=pct_font, fill=WHITE)
    track_y = y + 40
    track_h = 16
    _rounded(draw, (x, track_y, x + width, track_y + track_h), 8, TRACK)
    if pct is not None and pct > 0:
        filled = max(track_h, int(width * max(0.0, min(100.0, pct)) / 100))
        _rounded(draw, (x, track_y, x + filled, track_y + track_h), 8, fill)
    return track_y + track_h


def _line_chart(
    draw: ImageDraw.ImageDraw,
    box: tuple[int, int, int, int],
    copy: dict[str, Any],
) -> None:
    x0, y0, x1, y1 = box
    opened = copy["open_value"]
    live = copy["live_value"]
    if opened is None and live is None:
        font = _load_font(24, bold=False)
        _text(draw, (x0, y0 + 24), "No opening or current number yet.", font=font, fill=MUTED)
        return
    a = opened if opened is not None else live
    b = live if live is not None else opened
    assert a is not None and b is not None
    pad_x = 28
    pad_y = 22
    # Higher number sits higher, same as the site chart.
    lo, hi = (a, b) if a <= b else (b, a)
    height = (y1 - y0) - pad_y * 2

    def y_of(value: float) -> float:
        if hi == lo:
            return y0 + (y1 - y0) / 2
        return y0 + pad_y + ((hi - value) / (hi - lo)) * height

    left = x0 + pad_x
    right = x1 - pad_x
    mid_y = (y0 + y1) / 2
    draw.line((left, mid_y, right, mid_y), fill=AXIS, width=2)
    moved = copy["line_move"] not in (None, 0)
    color = GREEN if moved else (147, 197, 253)
    ya, yb = y_of(a), y_of(b)
    draw.line((left, ya, right, yb), fill=color, width=6)
    r = 9
    draw.ellipse((left - r, ya - r, left + r, ya + r), fill=(147, 197, 253))
    draw.ellipse((right - r, yb - r, right + r, yb + r), fill=GREEN)


def render_sharp_card(
    play: dict[str, Any],
    out_path: str | Path,
    *,
    league: str | None = None,
    fetch_logos: bool = True,
) -> Path:
    """Draw the sharp-money card and return the PNG path."""
    dest = Path(out_path)
    dest.parent.mkdir(parents=True, exist_ok=True)
    copy = card_copy(play, league=league)
    league_s = str(copy["league"] or "")
    away = str(play.get("away_team") or "").strip()
    home = str(play.get("home_team") or "").strip()
    home_away = str(play.get("home_away") or "").strip().lower()
    pick_away = home_away == "away"
    pick_home = home_away == "home"

    img = Image.new("RGB", (W, H), BG)
    draw = ImageDraw.Draw(img)

    margin = 22
    pad = 18
    inner_x = margin + pad
    inner_w = W - margin * 2 - pad * 2
    header_h = 148
    draw.rectangle((0, 0, W, header_h), fill=HEADER)
    draw.line((0, header_h - 1, W, header_h - 1), fill=HEADER_LINE, width=2)

    logo = 72
    logo_y = (header_h - logo) // 2
    logo_x = margin
    _draw_mark(img, draw, away or "Away", league_s, (logo_x, logo_y), logo, ring=pick_away, fetch_logos=fetch_logos)
    at_font = _load_font(22, bold=True)
    at = "@"
    at_w, at_h = _text_size(at_font, at)
    at_x = logo_x + logo + 12
    _text(draw, (at_x, logo_y + (logo - at_h) / 2 - 2), at, font=at_font, fill=MUTED)
    home_x = at_x + at_w + 12
    _draw_mark(img, draw, home or "Home", league_s, (home_x, logo_y), logo, ring=pick_home, fetch_logos=fetch_logos)

    text_x = home_x + logo + 18
    text_right = W - margin
    title_font = _load_font(28, bold=True)
    title = _ellipsize(title_font, str(copy["matchup"]), text_right - text_x)
    _text(draw, (text_x, logo_y + 6), title, font=title_font, fill=WHITE)

    pill_y = logo_y + 44
    pill_x = text_x
    pill_font = _load_font(15, bold=True)
    for text, fg in (
        (league_s, LEAGUE_COLOR.get(league_s, MUTED)),
        (str(copy["market"]), MUTED),
    ):
        if not text:
            continue
        tw, th = _text_size(pill_font, text)
        pw, ph = tw + 22, th + 12
        _rounded(draw, (pill_x, pill_y, pill_x + pw, pill_y + ph), ph // 2, PILL)
        _text(draw, (pill_x + 11, pill_y + 4), text, font=pill_font, fill=fg)
        pill_x += pw + 8

    bet_top = header_h + 18
    bet_h = 100
    _rounded(draw, (margin, bet_top, W - margin, bet_top + bet_h), 18, GREEN_DIM)
    draw.rounded_rectangle(
        (margin, bet_top, W - margin, bet_top + bet_h),
        radius=18,
        outline=GREEN_LINE,
        width=2,
    )
    kicker = _load_font(15, bold=True)
    _text(draw, (margin + 20, bet_top + 16), "THE BET", font=kicker, fill=GREEN)
    bet_font = _load_font(36, bold=True)
    bet = _ellipsize(bet_font, str(copy["bet"]), W - margin * 2 - 40)
    _text(draw, (margin + 20, bet_top + 40), bet, font=bet_font, fill=WHITE)

    head_font = _load_font(15, bold=True)
    tickets_top = bet_top + bet_h + 16
    # Each bar is a label row (40px) plus a 16px track.
    bar_h = 56
    steam = copy["steam"]
    steam_h = 40 if steam is not None else 8
    tickets_h = pad + 34 + bar_h + 18 + bar_h + steam_h + 12
    tickets_box = (margin, tickets_top, W - margin, tickets_top + tickets_h)
    _panel(draw, tickets_box)

    _text(draw, (inner_x, tickets_top + pad), "TICKETS VS HANDLE", font=head_font, fill=MUTED)
    bar_y = tickets_top + pad + 34
    bar_y = _bar(
        draw,
        x=inner_x,
        y=bar_y,
        width=inner_w,
        label="PUBLIC",
        name=str(copy["public_name"]),
        pct=copy["public_pct"],
        fill=BLUE,
    )
    bar_y = _bar(
        draw,
        x=inner_x,
        y=bar_y + 18,
        width=inner_w,
        label="HANDLE",
        name=str(copy["handle_name"]),
        pct=copy["handle_pct"],
        fill=GREEN,
    )
    if steam is not None:
        steam_font = _load_font(20, bold=True)
        sign = "+" if steam > 0 else ""
        steam_text = f"Steam {sign}{round(steam)}pp on {copy['handle_name']}"
        steam_color = GREEN if steam > 0 else ((248, 113, 113) if steam < 0 else MUTED)
        _text(draw, (inner_x, bar_y + 14), steam_text, font=steam_font, fill=steam_color)

    chart_h = 132
    label_font = _load_font(14, bold=True)
    value_font = _load_font(24, bold=True)
    note_font = _load_font(18, bold=False)
    note_h = 28 if copy["move_note"] else 0
    line_top = tickets_box[3] + 14
    line_h = pad + 22 + 8 + chart_h + 8 + 22 + 28 + note_h + pad
    line_box = (margin, line_top, W - margin, line_top + line_h)
    _panel(draw, line_box)

    _text(draw, (inner_x, line_top + pad), "LINE MOVEMENT", font=head_font, fill=MUTED)
    chart_top = line_top + pad + 30
    chart_box = (inner_x, chart_top, inner_x + inner_w, chart_top + chart_h)
    _line_chart(draw, chart_box, copy)

    labels_y = chart_box[3] + 6
    value_y = labels_y + 20
    _text(draw, (inner_x, labels_y), "OPEN", font=label_font, fill=MUTED)
    _text(draw, (inner_x, value_y), str(copy["open_text"]), font=value_font, fill=WHITE)
    now_label = "NOW"
    nw, _ = _text_size(label_font, now_label)
    now_text = str(copy["now_text"])
    vw, _ = _text_size(value_font, now_text)
    _text(draw, (inner_x + inner_w - nw, labels_y), now_label, font=label_font, fill=MUTED)
    _text(draw, (inner_x + inner_w - vw, value_y), now_text, font=value_font, fill=WHITE)
    note_bottom = value_y + 30
    if copy["move_note"]:
        _text(draw, (inner_x, value_y + 34), str(copy["move_note"]), font=note_font, fill=MUTED)
        note_bottom = value_y + 58

    foot_y = line_box[3] + 16
    draw.line((margin, foot_y, W - margin, foot_y), fill=FOOT, width=2)
    foot_bottom = foot_y + 16
    if copy["starts"]:
        time_font = _load_font(18, bold=True)
        starts = str(copy["starts"])
        tw, _ = _text_size(time_font, starts)
        _text(draw, (W - margin - tw, foot_y + 12), starts, font=time_font, fill=MUTED)
        foot_bottom = foot_y + 40

    cropped = img.crop((0, 0, W, min(H, foot_bottom + 18)))
    cropped.save(dest, format="PNG")
    return dest
