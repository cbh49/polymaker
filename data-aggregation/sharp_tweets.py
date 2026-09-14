#!/usr/bin/env python3
"""Post up to two A/A+ sharp-money plays per Pacific day to X.

Uses the same OAuth 1.0a credentials as whale tweets. Discord still posts
every A/A+; this path is capped at MAX_PER_DAY.

Enabled when X_SHARP_POSTS=1, or (if unset) when X_WHALE_POSTS is on so the
production monitor switch also turns on sharp-money tweets. Set
X_SHARP_POSTS=0 to disable without touching whale posts.
"""

from __future__ import annotations

import json
import os
import re
import sys
import time
from datetime import datetime
from pathlib import Path
from typing import Any, Callable
from zoneinfo import ZoneInfo

from discord_sharp_alerts import (
    _as_float,
    _format_american,
    _format_number_line,
    _matchup_line,
    alert_plays,
    resolve_play_label,
)

try:
    from dotenv import load_dotenv
except ImportError:
    load_dotenv = None  # type: ignore[misc, assignment]

SCRIPT_DIR = Path(__file__).resolve().parent
BOT_ROOT = SCRIPT_DIR.parent
_SRC = BOT_ROOT / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))
PAGE_TZ = ZoneInfo("America/Los_Angeles")
DEFAULT_QUOTA = SCRIPT_DIR / "output" / ".x_sharp_posted.json"

TWEET_CHAR_LIMIT = 280
T_CO_URL_LEN = 23
MAX_PER_DAY = 2
SUBSCRIBE_URL = "https://www.bretonanalytics.com/#/subscribe"
SUBSCRIBE_CTA = (
    "To get all sharp money plays, subscribe to the discord via bretonpicks website: "
    f"{SUBSCRIBE_URL}"
)
_URL_RE = re.compile(r"https?://\S+")

TweetFn = Callable[..., Any]


def _load_env() -> None:
    if load_dotenv is None:
        return
    load_dotenv(BOT_ROOT / ".env")
    load_dotenv()


def _flag(name: str) -> str:
    return (os.environ.get(name) or "").strip().lower()


def sharp_posts_enabled() -> bool:
    """X_SHARP_POSTS wins; otherwise inherit the whale kill switch."""
    explicit = _flag("X_SHARP_POSTS")
    if explicit in {"0", "false", "no"}:
        return False
    if explicit in {"1", "true", "yes"}:
        return True
    return _flag("X_WHALE_POSTS") in {"1", "true", "yes"}


def pacific_today_iso() -> str:
    return datetime.now(PAGE_TZ).date().isoformat()


def x_weighted_len(text: str) -> int:
    """X counts each URL as 23 characters regardless of the raw length."""
    return len(_URL_RE.sub("x" * T_CO_URL_LEN, text or ""))


def tweet_play_key(play: dict[str, Any], league: str | None) -> str:
    """Stable id without tier so an A → A+ upgrade does not consume a second slot."""
    league_s = str(league or play.get("league") or "").strip().upper() or "?"
    if league_s == "CFB":
        league_s = "NCAAF"
    date_s = str(play.get("date") or "").strip() or "?"
    event = str(play.get("event_id") or play.get("matchup") or "").strip() or "?"
    market = str(play.get("market") or "").strip().lower() or "?"
    side = str(play.get("side") or "").strip() or "?"
    return f"{league_s}|{date_s}|{event}|{market}|{side}"


def _normalize_league(value: str | None) -> str:
    key = str(value or "").strip().upper()
    if key == "CFB":
        return "NCAAF"
    return key or "SHARP"


class DailyQuota:
    """At most MAX_PER_DAY tweets on a Pacific calendar day."""

    def __init__(self, path: Path, *, day: str | None = None) -> None:
        self.path = path
        self.day = day or pacific_today_iso()
        self.keys: list[str] = []
        if path.is_file():
            try:
                raw = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                raw = {}
            if isinstance(raw, dict) and str(raw.get("date") or "") == self.day:
                stored = raw.get("keys") or []
                if isinstance(stored, list):
                    self.keys = [str(k) for k in stored]

    @property
    def remaining(self) -> int:
        return max(0, MAX_PER_DAY - len(self.keys))

    def save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "date": self.day,
            "updated_at": datetime.now(PAGE_TZ).isoformat(),
            "keys": self.keys,
        }
        self.path.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")


def _play_line_text(play: dict[str, Any]) -> str:
    label = resolve_play_label(play)
    market = str(play.get("market") or "").strip().lower()
    juice = _format_american(_as_float(play.get("play_odds")))
    if market != "moneyline" and juice:
        return f"{label} ({juice})"
    return label


def _steam_line(play: dict[str, Any]) -> str | None:
    pub_name = str(play.get("public_favors_name") or "").strip()
    pub_pct = _as_float(play.get("public_favors_bet_pct"))
    handle_pct = _as_float(play.get("handle_bet_pct"))
    sharp_pub = _as_float(play.get("public_bet_pct"))
    home_away = str(play.get("home_away") or "").strip().lower()
    if home_away in {"over", "under"}:
        handle_name = home_away.capitalize()
    elif home_away == "away":
        handle_name = str(play.get("away_team") or play.get("side") or "").strip()
    elif home_away == "home":
        handle_name = str(play.get("home_team") or play.get("side") or "").strip()
    else:
        handle_name = str(play.get("side") or "").strip()
    if pub_name and pub_pct is not None and handle_name and handle_pct is not None:
        steam = None if sharp_pub is None else handle_pct - sharp_pub
        extra = f" (+{steam:.0f}pp)" if steam is not None and steam > 0 else ""
        return f"Public {pub_pct:.0f}% {pub_name} · Handle {handle_pct:.0f}% {handle_name}{extra}"
    if handle_pct is not None and sharp_pub is not None:
        return f"Tickets {sharp_pub:.0f}% · Handle {handle_pct:.0f}%"
    return None


def _line_move_text(play: dict[str, Any]) -> str | None:
    market = str(play.get("market") or "").strip().lower()
    signed = market == "spread"
    opened = _format_number_line(_as_float(play.get("open")), signed=signed)
    live = _format_number_line(_as_float(play.get("live")), signed=signed)
    open_juice = _format_american(_as_float(play.get("open_odds")))
    live_juice = _format_american(
        _as_float(play.get("play_odds") if market != "moneyline" else play.get("live"))
    )
    if market == "moneyline":
        opened = _format_american(_as_float(play.get("open")))
        live = _format_american(_as_float(play.get("live")))
        open_juice = live_juice = None
    if not opened or not live:
        return None
    left = f"{opened} ({open_juice})" if open_juice else opened
    right = f"{live} ({live_juice})" if live_juice else live
    return f"{left} → {right}"


def format_sharp_tweet(play: dict[str, Any], *, league: str | None = None) -> str:
    """Eye-catching tweet; CTA is never dropped. Fits X's 280-char weighted limit."""
    league_s = _normalize_league(league or play.get("league"))
    tier = str(play.get("tier") or "A").strip().upper()
    header = f"💰 SHARP MONEY · {league_s} · {tier}"
    matchup = _matchup_line(play)
    play_txt = _play_line_text(play)
    steam = _steam_line(play)
    moved = _line_move_text(play)
    details = "\n".join(p for p in (steam, moved) if p) or None
    tags = f"#{league_s} #Gambling𝕏 #SportsBettingX"

    def _join(parts: list[str | None]) -> str:
        blocks = [p for p in parts if p]
        return "\n\n".join(blocks)

    candidates = [
        _join([header, f"{matchup}\nPLAY: {play_txt}", details, tags, SUBSCRIBE_CTA]),
        _join([header, f"{matchup}\nPLAY: {play_txt}", steam, tags, SUBSCRIBE_CTA]),
        _join([header, f"{matchup}\nPLAY: {play_txt}", tags, SUBSCRIBE_CTA]),
        _join([header, f"PLAY: {play_txt}", tags, SUBSCRIBE_CTA]),
        _join([header, f"PLAY: {play_txt}", SUBSCRIBE_CTA]),
    ]
    for body in candidates:
        if x_weighted_len(body) <= TWEET_CHAR_LIMIT:
            return body
    # Last resort: keep CTA, trim the play line.
    room = TWEET_CHAR_LIMIT - x_weighted_len(SUBSCRIBE_CTA) - 2
    head = f"{header}\nPLAY: {play_txt}"[: max(0, room)]
    return f"{head}\n\n{SUBSCRIBE_CTA}"


def _rank_key(play: dict[str, Any]) -> tuple[int, float, float]:
    tier = str(play.get("tier") or "").strip().upper()
    gap = _as_float(play.get("composite_gap")) or 0.0
    conf = _as_float(play.get("model_confidence")) or 0.0
    return (0 if tier == "A+" else 1, -gap, -conf)


def collect_alert_plays(outputs: list[dict[str, Any]] | dict[str, Any]) -> list[tuple[str, dict[str, Any]]]:
    if isinstance(outputs, dict):
        outputs = [outputs]
    rows: list[tuple[str, dict[str, Any]]] = []
    for payload in outputs:
        if not isinstance(payload, dict):
            continue
        league = _normalize_league(str(payload.get("league") or ""))
        for play in alert_plays(list(payload.get("plays") or [])):
            rows.append((league, play))
    rows.sort(key=lambda item: _rank_key(item[1]))
    return rows


def post_sharp_tweets(
    outputs: list[dict[str, Any]] | dict[str, Any],
    *,
    dry_run: bool = False,
    quota_path: Path | None = None,
    day: str | None = None,
    poster: TweetFn | None = None,
) -> dict[str, Any]:
    """Tweet the best remaining A/A+ plays until today's cap is hit. Never raises."""
    _load_env()
    rows = collect_alert_plays(outputs)
    if not rows:
        print("X: no A/A+ plays to tweet.")
        return {"posted": 0, "skipped": 0, "reason": "no_alerts"}

    quota = DailyQuota(quota_path or DEFAULT_QUOTA, day=day)
    already = set(quota.keys)
    slots = quota.remaining
    if slots <= 0:
        print(f"X: daily cap of {MAX_PER_DAY} sharp-money tweets already hit.")
        return {"posted": 0, "skipped": len(rows), "reason": "daily_cap"}

    if not dry_run and not sharp_posts_enabled():
        print("X: skip sharp tweets (set X_SHARP_POSTS=1 or X_WHALE_POSTS=1).")
        return {"posted": 0, "skipped": len(rows), "reason": "disabled"}

    from polymaker.x_client import credentials_ready, post_tweet

    send = poster if poster is not None else post_tweet
    if poster is None and not dry_run and not credentials_ready():
        print("X: skip sharp tweets (missing OAuth credentials).")
        return {"posted": 0, "skipped": len(rows), "reason": "missing_credentials"}

    posted = 0
    skipped = 0
    texts: list[str] = []
    for league, play in rows:
        key = tweet_play_key(play, league)
        if key in already:
            skipped += 1
            continue
        if posted >= slots:
            skipped += 1
            continue
        text = format_sharp_tweet(play, league=league)
        try:
            if posted and not dry_run:
                time.sleep(1.1)
            result = send(text, dry_run=dry_run)
        except Exception as exc:  # noqa: BLE001
            print(f"X: tweet failed: {exc}", flush=True)
            return {
                "posted": posted,
                "skipped": skipped,
                "reason": "error",
                "texts": texts,
            }
        quota.keys.append(key)
        already.add(key)
        posted += 1
        texts.append(text)
        url = getattr(result, "url", "") or ""
        print(f"X: posted {url or '(dry-run)'}  {resolve_play_label(play)}")

    if posted and not dry_run:
        quota.save()
    elif dry_run:
        for text in texts:
            print(text)
            print("---")
        print(f"X dry-run: {posted} tweet(s), skipped {skipped}, cap {MAX_PER_DAY}/day.")

    if posted == 0 and skipped:
        print(f"X: nothing new to tweet ({skipped} already used today's slots or keys).")
    return {"posted": posted, "skipped": skipped, "reason": "ok" if posted else "noop", "texts": texts}
