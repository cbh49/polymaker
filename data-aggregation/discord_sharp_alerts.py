#!/usr/bin/env python3
"""Post Tier A / A+ sharp-money plays to a Discord incoming webhook.

Called from find_sharp_money.py and scripts/run_sharp_pipeline.py. Missing
webhook, network errors, and Discord 4xx never fail the finder.

  DISCORD_SHARP_WEBHOOK_URL   preferred
  DISCORD_WEBHOOK_URL         fallback
"""

from __future__ import annotations

import json
import os
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable
from zoneinfo import ZoneInfo

try:
    from dotenv import load_dotenv
except ImportError:
    load_dotenv = None  # type: ignore[misc, assignment]

SCRIPT_DIR = Path(__file__).resolve().parent
BOT_ROOT = SCRIPT_DIR.parent
DEFAULT_CACHE = SCRIPT_DIR / "output" / ".discord_sent.json"
PAGE_TZ = ZoneInfo("America/Los_Angeles")

WEBHOOK_ENV = ("DISCORD_SHARP_WEBHOOK_URL", "DISCORD_WEBHOOK_URL")
ALERT_TIERS = frozenset({"A", "A+"})
EMBEDS_PER_MESSAGE = 10
COLOR_A_PLUS = 0xF5C518
COLOR_A = 0x00E676
SOURCE_LABELS = {"primary": "DK", "vsin": "VSiN", "sbd": "SBD"}
PRIMARY_LABELS = {"draftkings": "DK", "playerprops": "PlayerProps"}
RLM_LABELS = {"eva": "EVA", "thespread": "TheSpread", "polymarket": "Polymarket"}

PostFn = Callable[[str, dict[str, Any]], Any]


def _load_env() -> None:
    if load_dotenv is None:
        return
    load_dotenv(BOT_ROOT / ".env")
    load_dotenv()


def webhook_url_from_env() -> str | None:
    _load_env()
    for key in WEBHOOK_ENV:
        raw = (os.environ.get(key) or "").strip()
        if raw and "discord.com/api/webhooks/" in raw:
            return raw
    return None


def _as_float(value: Any) -> float | None:
    if value is None or value == "":
        return None
    try:
        out = float(value)
    except (TypeError, ValueError):
        return None
    if out != out:
        return None
    return out


def _as_int(value: Any) -> int | None:
    num = _as_float(value)
    if num is None:
        return None
    return int(round(num))


def pct_bar(pct: float | None, width: int = 10) -> str:
    if pct is None:
        return "░" * width
    filled = int(round(max(0.0, min(100.0, pct)) / 100.0 * width))
    filled = max(0, min(width, filled))
    return "█" * filled + "░" * (width - filled)


def _format_american(odds: float | None) -> str | None:
    val = _as_float(odds)
    if val is None:
        return None
    n = int(round(val))
    return f"+{n}" if n > 0 else str(n)


def _format_number_line(value: float | None, *, signed: bool) -> str | None:
    val = _as_float(value)
    if val is None:
        return None
    if val == int(val):
        n = int(val)
        if signed and n > 0:
            return f"+{n}"
        return str(n)
    text = f"{val:g}"
    if signed and val > 0 and not text.startswith(("+", "-")):
        return f"+{text}"
    return text


def _with_juice(number: str | None, odds: float | None) -> str | None:
    if not number:
        return None
    juice = _format_american(odds)
    return f"{number} ({juice})" if juice else number


def alert_plays(plays: list[Any]) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for play in plays:
        if not isinstance(play, dict):
            continue
        if str(play.get("tier") or "").strip().upper() in ALERT_TIERS:
            out.append(play)
    return out


def play_key(play: dict[str, Any], league: str | None) -> str:
    league_s = str(league or play.get("league") or "").strip().upper() or "?"
    date_s = str(play.get("date") or "").strip() or "?"
    event = str(play.get("event_id") or play.get("matchup") or "").strip() or "?"
    market = str(play.get("market") or "").strip().lower() or "?"
    side = str(play.get("side") or "").strip() or "?"
    tier = str(play.get("tier") or "").strip().upper() or "?"
    return f"{league_s}|{date_s}|{event}|{market}|{side}|{tier}"


class SentCache:
    """Persist posted play keys so the 30-minute timer does not re-spam."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self.keys: set[str] = set()
        if path.is_file():
            try:
                raw = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                raw = {}
            stored = raw.get("keys") if isinstance(raw, dict) else raw
            if isinstance(stored, list):
                self.keys = {str(k) for k in stored}

    def save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "updated_at": datetime.now(timezone.utc).isoformat(),
            "keys": sorted(self.keys),
        }
        self.path.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")


def _source_names(play: dict[str, Any], config: dict[str, Any] | None) -> str:
    agreeing = play.get("agreeing_sources") or []
    if not isinstance(agreeing, list):
        agreeing = []
    primary = PRIMARY_LABELS.get(str((config or {}).get("primary_source") or "").lower(), "DK")
    labels: list[str] = []
    for src in agreeing:
        key = str(src).strip().lower()
        if key == "primary":
            labels.append(primary)
        else:
            labels.append(SOURCE_LABELS.get(key, str(src)))
    n = play.get("n_sources_agreeing")
    try:
        n_i = int(n) if n is not None else len(labels)
    except (TypeError, ValueError):
        n_i = len(labels)
    joined = " · ".join(labels) if labels else "—"
    return f"{joined} ({n_i}/{max(n_i, len(labels) or 1)})"


def _matchup_line(play: dict[str, Any]) -> str:
    display = str(play.get("matchup_display") or "").strip()
    if display:
        return display
    away = str(play.get("away_team") or "").strip()
    home = str(play.get("home_team") or "").strip()
    if away and home:
        return f"{away} @ {home}"
    return str(play.get("matchup") or "Matchup").strip()


def resolve_play_label(play: dict[str, Any]) -> str:
    existing = str(play.get("play_label") or "").strip()
    if existing:
        return existing
    market = str(play.get("market") or "moneyline").strip().lower()
    home_away = str(play.get("home_away") or "").strip().lower()
    if home_away in {"over", "under"}:
        name = home_away.capitalize()
    elif home_away == "away":
        name = str(play.get("away_team") or play.get("side") or "Away")
    elif home_away == "home":
        name = str(play.get("home_team") or play.get("side") or "Home")
    else:
        name = str(play.get("side") or "Play")
    line = _as_float(play.get("play_line"))
    if line is None:
        line = _as_float(play.get("live"))
    odds = _as_float(play.get("play_odds"))
    if market == "moneyline":
        formatted = _format_american(odds if odds is not None else line)
        return f"{name} {formatted}" if formatted else name
    formatted = _format_number_line(line, signed=(market == "spread"))
    return f"{name} {formatted}" if formatted else name


def _steam_field(play: dict[str, Any]) -> str | None:
    pub_name = str(play.get("public_favors_name") or "").strip()
    handle_name = resolve_play_label(play).rsplit(" ", 1)[0] if play.get("play_label") else ""
    home_away = str(play.get("home_away") or "").strip().lower()
    if home_away in {"over", "under"}:
        handle_name = home_away.capitalize()
    elif home_away == "away":
        handle_name = str(play.get("away_team") or play.get("side") or handle_name)
    elif home_away == "home":
        handle_name = str(play.get("home_team") or play.get("side") or handle_name)
    pub_pct = _as_float(play.get("public_favors_bet_pct"))
    handle_pct = _as_float(play.get("handle_bet_pct"))
    sharp_pub = _as_float(play.get("public_bet_pct"))
    if pub_pct is None and sharp_pub is None and handle_pct is None:
        return None
    lines: list[str] = []
    if pub_name and pub_pct is not None:
        lines.append(f"Public on **{pub_name}**  `{pct_bar(pub_pct)}`  {pub_pct:.0f}%")
    elif sharp_pub is not None:
        lines.append(f"Public tickets  `{pct_bar(sharp_pub)}`  {sharp_pub:.0f}%")
    if handle_name and handle_pct is not None:
        steam = None
        if sharp_pub is not None:
            steam = handle_pct - sharp_pub
        steam_txt = f"   **{steam:+.0f}pp**" if steam is not None else ""
        lines.append(f"Handle on **{handle_name}**  `{pct_bar(handle_pct)}`  {handle_pct:.0f}%{steam_txt}")
    extra: list[str] = []
    vsin_h = _as_float(play.get("vsin_handle_bet_pct"))
    vsin_p = _as_float(play.get("vsin_public_bet_pct"))
    if vsin_h is not None or vsin_p is not None:
        extra.append(
            f"VSiN {vsin_p:.0f}% tickets / {vsin_h:.0f}% handle"
            if vsin_p is not None and vsin_h is not None
            else "VSiN split"
        )
    sbd_h = _as_float(play.get("sbd_handle_bet_pct"))
    sbd_p = _as_float(play.get("sbd_public_bet_pct"))
    if sbd_h is not None or sbd_p is not None:
        extra.append(
            f"SBD {sbd_p:.0f}% tickets / {sbd_h:.0f}% handle"
            if sbd_p is not None and sbd_h is not None
            else "SBD split"
        )
    if extra:
        lines.append("_" + " · ".join(extra) + "_")
    return "\n".join(lines) if lines else None


def _line_field(play: dict[str, Any]) -> str | None:
    market = str(play.get("market") or "").strip().lower()
    signed = market == "spread"
    opened = _with_juice(_format_number_line(_as_float(play.get("open")), signed=signed), _as_float(play.get("open_odds")))
    live = _with_juice(
        _format_number_line(_as_float(play.get("live")), signed=signed),
        _as_float(play.get("play_odds") if market != "moneyline" else play.get("live")),
    )
    if market == "moneyline":
        opened = _format_american(_as_float(play.get("open")))
        live = _format_american(_as_float(play.get("live")))
    if not opened and not live:
        return None
    lines = []
    if opened:
        lines.append(f"**Opening**  {opened}")
    if live:
        lines.append(f"**Current**  {live}")
    move = _as_float(play.get("line_move"))
    toward = str(play.get("play_label") or play.get("side") or "").strip()
    name = toward.rsplit(" ", 1)[0] if toward else "play"
    if home_away := str(play.get("home_away") or ""):
        if home_away == "away":
            name = str(play.get("away_team") or name)
        elif home_away == "home":
            name = str(play.get("home_team") or name)
        elif home_away in {"over", "under"}:
            name = home_away.capitalize()
    if move is not None and live and opened:
        abs_move = abs(move)
        if market == "moneyline":
            lines.append(f"moved toward {name}")
        else:
            pretty = f"{abs_move:g}" if abs_move != int(abs_move) else str(int(abs_move))
            lines.append(f"moved {pretty} toward {name}")
    return "\n".join(lines)


def _confidence_field(play: dict[str, Any]) -> str:
    conf = _as_int(play.get("model_confidence"))
    if conf is None:
        conf = 70
    bar = pct_bar(float(conf), width=10)
    lines = [f"`{bar}`  **{conf}**"]
    fair = _as_float(play.get("implied_fair_prob"))
    if fair is not None:
        lines.append(f"Fair {fair * 100:.1f}%")
    return "\n".join(lines)


def _exchange_field(play: dict[str, Any]) -> str | None:
    block = play.get("exchange_confirmation")
    block = block if isinstance(block, dict) else {}
    edge = _as_float(block.get("exchange_edge_pct"))
    liq = _as_float(block.get("polymarket_liquidity"))
    if liq is None:
        liq = _as_float(play.get("polymarket_rlm_liquidity"))
    parts: list[str] = []
    if edge is not None:
        parts.append(f"{edge:+.1f}pp vs books")
    if liq is not None:
        if liq >= 1000:
            parts.append(f"${liq / 1000:.0f}k liq")
        else:
            parts.append(f"${liq:.0f} liq")
    return " · ".join(parts) if parts else None


def _warning_field(play: dict[str, Any]) -> str | None:
    notes: list[str] = []
    if play.get("low_volume_dog_flag"):
        notes.append("Low-volume ML dog")
    if play.get("ml_spread_divergence"):
        notes.append("Spread does not confirm this ML")
    if play.get("rlm_source_conflict"):
        notes.append("RLM sources disagree")
    conf = play.get("exchange_confirmation")
    conf = conf if isinstance(conf, dict) else {}
    if play.get("polymarket_low_liquidity") is True or conf.get("low_liquidity") is True:
        notes.append("Thin Polymarket liquidity")
    extra = str(play.get("confidence_note") or "").strip()
    if extra and extra not in notes:
        notes.append(extra)
    return "\n".join(f"• {n}" for n in notes) if notes else None


def build_embed(
    play: dict[str, Any],
    *,
    league: str | None = None,
    config: dict[str, Any] | None = None,
) -> dict[str, Any]:
    tier = str(play.get("tier") or "A").strip().upper()
    market = str(play.get("market") or "moneyline").strip().lower()
    league_s = str(league or play.get("league") or "").strip().upper()
    kickoff = str(play.get("game_time_local") or "").strip()
    play_label = resolve_play_label(play)
    juice = _format_american(_as_float(play.get("play_odds")))
    if market == "moneyline":
        play_value = f">>> **{play_label}**"
    elif juice:
        play_value = f">>> **{play_label}**  ({juice})"
    else:
        play_value = f">>> **{play_label}**"

    desc_bits = [f"**{_matchup_line(play)}**"]
    meta = " · ".join(p for p in (kickoff, league_s, market) if p)
    if meta:
        desc_bits.append(meta)

    fields: list[dict[str, Any]] = [
        {"name": "PLAY", "value": play_value, "inline": False},
    ]
    steam = _steam_field(play)
    if steam:
        fields.append({"name": "Steam", "value": steam, "inline": False})
    line = _line_field(play)
    if line:
        fields.append({"name": "Line", "value": line, "inline": True})
    fields.append({"name": "Confidence", "value": _confidence_field(play), "inline": True})
    fields.append({"name": "Sources", "value": _source_names(play, config), "inline": True})
    exchange = _exchange_field(play)
    if exchange:
        fields.append({"name": "Exchange", "value": exchange, "inline": True})
    warn = _warning_field(play)
    if warn:
        fields.append({"name": "Flags", "value": warn, "inline": False})

    rlm = str(play.get("rlm_source_used") or "").strip().lower()
    footer = f"RLM via {RLM_LABELS.get(rlm, rlm.upper() or '—')}"
    gap = _as_float(play.get("composite_gap"))
    if gap is not None:
        footer += f" · gap {gap:.0f}"

    return {
        "title": f"{tier}  ·  SHARP MONEY",
        "description": "\n".join(desc_bits),
        "color": COLOR_A_PLUS if tier == "A+" else COLOR_A,
        "fields": fields,
        "footer": {"text": footer},
    }


def _chunks(items: list[dict[str, Any]], size: int) -> list[list[dict[str, Any]]]:
    return [items[i : i + size] for i in range(0, len(items), size)]


def _default_post(url: str, payload: dict[str, Any]) -> Any:
    import requests

    resp = requests.post(url, json=payload, timeout=30)
    return resp


def post_sharp_alerts(
    output: dict[str, Any],
    *,
    webhook_url: str | None = None,
    dry_run: bool = False,
    force: bool = False,
    cache_path: Path | None = None,
    post_fn: PostFn | None = None,
) -> dict[str, Any]:
    """Filter A/A+, skip already-sent keys, POST embeds. Never raises to caller."""
    league = str(output.get("league") or "").strip().upper()
    if league == "CFB":
        league = "NCAAF"
    config = output.get("config") if isinstance(output.get("config"), dict) else {}
    plays = alert_plays(list(output.get("plays") or []))
    if not plays:
        print("Discord: no A/A+ plays to post.")
        return {"posted": 0, "skipped": 0, "reason": "no_alerts"}

    cache = SentCache(cache_path or DEFAULT_CACHE)
    fresh: list[dict[str, Any]] = []
    skipped = 0
    for play in plays:
        key = play_key(play, league)
        if not force and key in cache.keys:
            skipped += 1
            continue
        fresh.append(play)
    if not fresh:
        print(f"Discord: all {len(plays)} A/A+ play(s) already posted.")
        return {"posted": 0, "skipped": skipped, "reason": "deduped"}

    embeds = [build_embed(p, league=league, config=config) for p in fresh]
    url = webhook_url or webhook_url_from_env()
    if dry_run:
        print(json.dumps({"username": "Sharp Money", "embeds": embeds}, indent=2))
        print(f"Discord dry-run: {len(embeds)} embed(s), skipped {skipped}.")
        return {"posted": 0, "skipped": skipped, "reason": "dry_run", "embeds": embeds}

    if not url:
        print("Discord: no DISCORD_SHARP_WEBHOOK_URL (skipping).")
        return {"posted": 0, "skipped": skipped, "reason": "no_webhook"}

    sender = post_fn or _default_post
    posted = 0
    try:
        for i, batch in enumerate(_chunks(embeds, EMBEDS_PER_MESSAGE)):
            if i:
                time.sleep(1.1)
            payload = {"username": "Sharp Money", "embeds": batch}
            resp = sender(url, payload)
            status = getattr(resp, "status_code", 200)
            if status is not None and int(status) >= 400:
                body = getattr(resp, "text", "")
                print(f"Discord error {status}: {body}", flush=True)
                return {"posted": posted, "skipped": skipped, "reason": f"http_{status}"}
            posted += len(batch)
            for play in fresh[i * EMBEDS_PER_MESSAGE : i * EMBEDS_PER_MESSAGE + len(batch)]:
                cache.keys.add(play_key(play, league))
        cache.save()
    except Exception as exc:  # noqa: BLE001
        print(f"Discord alerts failed: {exc}", flush=True)
        return {"posted": posted, "skipped": skipped, "reason": "error"}

    print(f"Discord: posted {posted} A/A+ play(s), skipped {skipped}.")
    return {"posted": posted, "skipped": skipped, "reason": "ok"}


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description="Post A/A+ sharp-money plays to Discord")
    parser.add_argument("--input", type=Path, default=SCRIPT_DIR / "output" / "ncaaf_sharp_money.json")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--force", action="store_true")
    args = parser.parse_args()
    payload = json.loads(args.input.read_text(encoding="utf-8"))
    post_sharp_alerts(payload, dry_run=args.dry_run, force=args.force)
