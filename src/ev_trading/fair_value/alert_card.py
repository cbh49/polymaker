"""Pillow 1080×1350 +EV sportsbook card for Discord and X."""

from __future__ import annotations

import json
import urllib.request
from pathlib import Path
from typing import Any

from PIL import Image, ImageDraw, ImageFont

from ev_trading.fair_value.ev_alerts import (
    PUBLIC_DIR,
    SportsbookEvAlert,
    book_logo_path,
    format_american,
    parse_matchup_abbrs,
    team_full_name,
)

_BOT_ROOT = Path(__file__).resolve().parents[3]
TEAM_LOGOS_JSON = PUBLIC_DIR / "nfl-team-logos.json"
DEFAULT_CACHE = _BOT_ROOT / "output" / "ev" / ".logo-cache"

W, H = 1080, 1350
MARGIN = 36
BG = (10, 11, 13)
HEADER = (18, 22, 28)
PANEL = (20, 22, 26)
PANEL_ALT = (30, 34, 40)
CHIP = (42, 46, 54)
WHITE = (255, 255, 255)
MUTED = (168, 174, 184)
LIME = (80, 230, 105)
PILL_FAIR = (36, 40, 48)
PILL_PLAY = (18, 78, 46)
ACCENT = (80, 230, 105)

FONT_CANDIDATES = (
    "/usr/share/fonts/truetype/liberation/LiberationSans-Bold.ttf",
    "/usr/share/fonts/truetype/liberation/LiberationSans-Regular.ttf",
    "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
    "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
    "/System/Library/Fonts/Supplemental/Arial Bold.ttf",
    "/System/Library/Fonts/Supplemental/Arial Black.ttf",
    "/System/Library/Fonts/Supplemental/Arial.ttf",
    "/System/Library/Fonts/Helvetica.ttc",
)


def _load_font(size: int, *, bold: bool = True) -> Any:
    preferred = FONT_CANDIDATES if bold else (
        "/usr/share/fonts/truetype/liberation/LiberationSans-Regular.ttf",
        "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
        "/System/Library/Fonts/Supplemental/Arial.ttf",
        *FONT_CANDIDATES,
    )
    for path in preferred:
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


def _fit_font(text: str, max_width: int, size: int, *, min_size: int = 28) -> Any:
    while size >= min_size:
        font = _load_font(size, bold=True)
        width, _ = _text_size(font, text)
        if width <= max_width:
            return font
        size -= 2
    return _load_font(min_size, bold=True)


def _rounded(draw: ImageDraw.ImageDraw, box: tuple[int, int, int, int], radius: int, fill: tuple[int, int, int]) -> None:
    draw.rounded_rectangle(box, radius=radius, fill=fill)


def _fit_image(img: Image.Image, box_w: int, box_h: int) -> Image.Image:
    src = img.convert("RGBA")
    src.thumbnail((box_w, box_h), Image.Resampling.LANCZOS)
    canvas = Image.new("RGBA", (box_w, box_h), (0, 0, 0, 0))
    canvas.alpha_composite(
        src,
        ((box_w - src.width) // 2, (box_h - src.height) // 2),
    )
    return canvas


def _http_get(url: str) -> bytes:
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0 (BretonEVAlerts)"})
    with urllib.request.urlopen(req, timeout=20) as resp:
        return bytes(resp.read())


def _load_team_logos() -> dict[str, Any]:
    if not TEAM_LOGOS_JSON.is_file():
        return {}
    raw = json.loads(TEAM_LOGOS_JSON.read_text(encoding="utf-8"))
    return raw if isinstance(raw, dict) else {}


def _fetch_team_logo(abbr: str, *, cache_dir: Path) -> Image.Image | None:
    logos = _load_team_logos()
    name = team_full_name(abbr)
    entry = logos.get(name)
    if not isinstance(entry, dict):
        for row in logos.values():
            if isinstance(row, dict) and str(row.get("abbr") or "").upper() == abbr.upper():
                entry = row
                break
    if not isinstance(entry, dict):
        return None
    url = str(entry.get("logo_url") or "").strip()
    if not url:
        return None
    cache_dir.mkdir(parents=True, exist_ok=True)
    slug = str(entry.get("abbr") or abbr).lower()
    cache_path = cache_dir / f"{slug}.png"
    if not cache_path.is_file():
        try:
            cache_path.write_bytes(_http_get(url))
        except Exception:
            return None
    try:
        return Image.open(cache_path).convert("RGBA")
    except Exception:
        return None


def _paste(base: Image.Image, overlay: Image.Image, xy: tuple[int, int]) -> None:
    if overlay.mode != "RGBA":
        overlay = overlay.convert("RGBA")
    layer = Image.new("RGBA", base.size, (0, 0, 0, 0))
    layer.alpha_composite(overlay, xy)
    composed = Image.alpha_composite(base.convert("RGBA"), layer)
    base.paste(composed.convert("RGB"))


def _logo_chip(logo: Image.Image, box_w: int, box_h: int, *, pad: int = 10) -> Image.Image:
    """Sit a sportsbook mark on a lighter chip so dark logos still read."""
    chip = Image.new("RGBA", (box_w, box_h), (0, 0, 0, 0))
    draw = ImageDraw.Draw(chip)
    draw.rounded_rectangle((0, 0, box_w - 1, box_h - 1), radius=14, fill=(*CHIP, 255))
    fitted = _fit_image(logo, box_w - pad * 2, box_h - pad * 2)
    chip.alpha_composite(fitted, (pad, pad))
    return chip


def render_alert_card(
    alert: SportsbookEvAlert,
    out_path: str | Path,
    *,
    cache_dir: Path | None = None,
    public_dir: Path | None = None,
) -> Path:
    dest = Path(out_path)
    dest.parent.mkdir(parents=True, exist_ok=True)
    logos_dir = public_dir or PUBLIC_DIR
    cache = cache_dir or DEFAULT_CACHE

    img = Image.new("RGB", (W, H), BG)
    draw = ImageDraw.Draw(img)
    inner = W - MARGIN * 2

    quotes = [q for q in alert.quotes if book_logo_path(q.book, public_dir=logos_dir)]
    if not quotes:
        quotes = list(alert.quotes)

    title_font = _fit_font(alert.title, inner - 32, 56, min_size=30)
    tw, th = _text_size(title_font, alert.title)
    team_box = 132
    header_h = 36 + th + 20 + team_box + 28
    header_top = MARGIN
    _rounded(draw, (MARGIN, header_top, W - MARGIN, header_top + header_h), 28, HEADER)
    draw.rounded_rectangle(
        (MARGIN, header_top, MARGIN + 10, header_top + header_h),
        radius=8,
        fill=ACCENT,
    )
    draw.text(((W - tw) // 2, header_top + 28), alert.title, font=title_font, fill=WHITE)

    y = header_top + 28 + th + 18
    abbrs = parse_matchup_abbrs(alert.matchup)
    if abbrs:
        away, home = abbrs
        away_logo = _fetch_team_logo(away, cache_dir=cache)
        home_logo = _fetch_team_logo(home, cache_dir=cache)
        gap = 36
        at_font = _load_font(36, bold=True)
        at_w, at_h = _text_size(at_font, "@")
        row_w = team_box + gap + at_w + gap + team_box
        x0 = (W - row_w) // 2
        if away_logo is not None:
            _paste(img, _fit_image(away_logo, team_box, team_box), (x0, y))
        if home_logo is not None:
            _paste(
                img,
                _fit_image(home_logo, team_box, team_box),
                (x0 + team_box + gap + at_w + gap, y),
            )
        draw = ImageDraw.Draw(img)
        draw.text(
            (x0 + team_box + gap, y + (team_box - at_h) // 2 - 4),
            "@",
            font=at_font,
            fill=MUTED,
        )
    else:
        match_font = _load_font(34, bold=True)
        mw, mh = _text_size(match_font, alert.matchup)
        draw.text(((W - mw) // 2, y + (team_box - mh) // 2), alert.matchup, font=match_font, fill=MUTED)

    y = header_top + header_h + 18

    cols = 2
    cell_w = (inner - 12) // cols
    cell_h = 128
    rows_n = max(1, (len(quotes) + cols - 1) // cols)
    grid_pad = 14
    grid_h = rows_n * cell_h + grid_pad * 2
    _rounded(draw, (MARGIN, y, W - MARGIN, y + grid_h), 24, PANEL)
    odds_font = _load_font(40, bold=True)
    logo_w, logo_h = 168, 88
    for i, quote in enumerate(quotes):
        col = i % cols
        row = i // cols
        cx = MARGIN + grid_pad + col * cell_w
        cy = y + grid_pad + row * cell_h
        cell_fill = PANEL_ALT if (row + col) % 2 else (24, 26, 30)
        _rounded(draw, (cx, cy, cx + cell_w - 10, cy + cell_h - 10), 18, cell_fill)
        logo_path = book_logo_path(quote.book, public_dir=logos_dir)
        if logo_path is not None:
            chip = _logo_chip(Image.open(logo_path), logo_w, logo_h)
            _paste(img, chip, (cx + 14, cy + (cell_h - 10 - logo_h) // 2))
            draw = ImageDraw.Draw(img)
        odds_text = quote.american
        ow, oh = _text_size(odds_font, odds_text)
        draw.text(
            (cx + cell_w - 28 - ow, cy + (cell_h - 10 - oh) // 2),
            odds_text,
            font=odds_font,
            fill=WHITE,
        )

    fy = y + grid_h + 16
    footer_h = 300
    _rounded(draw, (MARGIN, fy, W - MARGIN, fy + footer_h), 28, PANEL_ALT)
    draw.rounded_rectangle(
        (MARGIN, fy, W - MARGIN, fy + 8),
        radius=6,
        fill=ACCENT,
    )

    pill_w = (inner - 36) // 2
    pill_h = 72
    px = MARGIN + 18
    py = fy + 28
    fair_txt = format_american(float(alert.fair_american))
    play_txt = format_american(alert.book_odds)
    _rounded(draw, (px, py, px + pill_w, py + pill_h), 16, PILL_FAIR)
    _rounded(draw, (px + pill_w + 16, py, px + pill_w * 2 + 16, py + pill_h), 16, PILL_PLAY)

    label_font = _load_font(18, bold=True)
    value_font = _load_font(34, bold=True)
    for label, value, left in (
        ("FAIR IMPLIED", fair_txt, px),
        ("PLAY IMPLIED", play_txt, px + pill_w + 16),
    ):
        lw, _ = _text_size(label_font, label)
        vw, vh = _text_size(value_font, value)
        draw.text((left + (pill_w - lw) // 2, py + 8), label, font=label_font, fill=MUTED)
        draw.text(
            (left + (pill_w - vw) // 2, py + pill_h - vh - 8),
            value,
            font=value_font,
            fill=WHITE,
        )

    ev_font = _fit_font("+EV Play", inner - 40, 64, min_size=40)
    ev_w, ev_h = _text_size(ev_font, "+EV Play")
    ev_y = py + pill_h + 16
    draw.text(((W - ev_w) // 2, ev_y), "+EV Play", font=ev_font, fill=LIME)

    winner_logo = book_logo_path(alert.book, public_dir=logos_dir)
    win_odds = format_american(alert.book_odds)
    win_font = _load_font(52, bold=True)
    ww, wh = _text_size(win_font, win_odds)
    win_logo_w, win_logo_h = 200, 96
    row_w = (win_logo_w + 28 + ww) if winner_logo is not None else ww
    rx = (W - row_w) // 2
    ry = ev_y + ev_h + 12
    if winner_logo is not None:
        chip = _logo_chip(Image.open(winner_logo), win_logo_w, win_logo_h, pad=12)
        _paste(img, chip, (rx, ry))
        draw = ImageDraw.Draw(img)
        draw.text((rx + win_logo_w + 28, ry + (win_logo_h - wh) // 2), win_odds, font=win_font, fill=WHITE)
    else:
        draw.text((rx, ry), win_odds, font=win_font, fill=WHITE)

    bottom = fy + footer_h + MARGIN
    cropped = img.crop((0, 0, W, min(H, max(bottom, 900))))
    cropped.save(dest, format="PNG", optimize=True)
    return dest
