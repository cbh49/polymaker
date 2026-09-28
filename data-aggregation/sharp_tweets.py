#!/usr/bin/env python3
"""Post each A+ sharp-money play to X once, with a card image.

Discord still posts every A and A+ play. This path only tweets tier A+, skips
games that have already started, and does not repeat a play.

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
    _matchup_line,
    play_has_started,
    resolve_play_label,
)
from sharp_card import render_sharp_card

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
SHARP_URL = "https://www.bretonanalytics.com/#/sharp"
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


class PostedPlays:
    """Remember every play already tweeted so the timer does not re-post it."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self.keys: list[str] = []
        if path.is_file():
            try:
                raw = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                raw = {}
            stored = raw.get("keys") if isinstance(raw, dict) else raw
            if isinstance(stored, list):
                self.keys = [str(k) for k in stored]

    def save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "updated_at": datetime.now(PAGE_TZ).isoformat(),
            "keys": self.keys,
        }
        self.path.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")


def format_sharp_tweet(play: dict[str, Any], *, league: str | None = None) -> str:
    """Short A+ alert. The card image carries the splits and line move."""
    del league  # matchup and league live on the image
    label = resolve_play_label(play)
    matchup = _matchup_line(play)
    play_line = f"{matchup} — {label}" if matchup and matchup not in label else label
    odds = _format_american(_as_float(play.get("play_odds")))
    lines = ["🚨 Sharp Money Play", "", play_line]
    if odds:
        lines.append(f"Odds: {odds}")
    lines.extend(["", f"See all sharp plays at: {SHARP_URL}", "#Gambling𝕏 #SportsBettingX"])
    return "\n".join(lines)


def _rank_key(play: dict[str, Any]) -> tuple[int, float, float]:
    tier = str(play.get("tier") or "").strip().upper()
    gap = _as_float(play.get("composite_gap")) or 0.0
    conf = _as_float(play.get("model_confidence")) or 0.0
    return (0 if tier == "A+" else 1, -gap, -conf)


def _is_a_plus(play: dict[str, Any]) -> bool:
    return str(play.get("tier") or "").strip().upper() == "A+"


def collect_alert_plays(outputs: list[dict[str, Any]] | dict[str, Any]) -> list[tuple[str, dict[str, Any]]]:
    """A+ plays only, strongest gap first. Tier A stays on Discord."""
    if isinstance(outputs, dict):
        outputs = [outputs]
    rows: list[tuple[str, dict[str, Any]]] = []
    for payload in outputs:
        if not isinstance(payload, dict):
            continue
        league = _normalize_league(str(payload.get("league") or ""))
        for play in payload.get("plays") or []:
            if isinstance(play, dict) and _is_a_plus(play):
                rows.append((league, play))
    rows.sort(key=lambda item: _rank_key(item[1]))
    return rows


def _card_path(play: dict[str, Any], league: str, directory: Path) -> Path:
    key = tweet_play_key(play, league).replace("|", "_")
    safe = re.sub(r"[^A-Za-z0-9_.-]+", "_", key)[:140]
    return directory / f"{safe}.png"


def post_sharp_tweets(
    outputs: list[dict[str, Any]] | dict[str, Any],
    *,
    dry_run: bool = False,
    quota_path: Path | None = None,
    day: str | None = None,
    poster: TweetFn | None = None,
    now: datetime | None = None,
    card_dir: Path | None = None,
    fetch_logos: bool = True,
) -> dict[str, Any]:
    """Tweet every new A+ play that has not started. Never raises."""
    del day  # kept so older callers can still pass a Pacific day
    _load_env()
    rows = collect_alert_plays(outputs)
    if not rows:
        print("X: no A+ plays to tweet.")
        return {"posted": 0, "skipped": 0, "reason": "no_alerts"}

    upcoming: list[tuple[str, dict[str, Any]]] = []
    started = 0
    for league, play in rows:
        if play_has_started(play, now=now):
            started += 1
            continue
        upcoming.append((league, play))
    if not upcoming:
        print(f"X: skipping {started} A+ play(s) that already started.")
        return {"posted": 0, "skipped": started, "reason": "started"}

    posted_log = PostedPlays(quota_path or DEFAULT_QUOTA)
    already = set(posted_log.keys)

    if not dry_run and not sharp_posts_enabled():
        print("X: skip sharp tweets (set X_SHARP_POSTS=1 or X_WHALE_POSTS=1).")
        return {"posted": 0, "skipped": len(upcoming) + started, "reason": "disabled"}

    from polymaker.x_client import credentials_ready, post_tweet

    send = poster if poster is not None else post_tweet
    if poster is None and not dry_run and not credentials_ready():
        print("X: skip sharp tweets (missing OAuth credentials).")
        return {"posted": 0, "skipped": len(upcoming) + started, "reason": "missing_credentials"}

    images_dir = card_dir or (SCRIPT_DIR / "output" / "sharp_cards")
    posted = 0
    skipped = started
    texts: list[str] = []
    images: list[str] = []
    for league, play in upcoming:
        key = tweet_play_key(play, league)
        if key in already:
            skipped += 1
            continue
        text = format_sharp_tweet(play, league=league)
        media: list[Path] | None = None
        try:
            png = render_sharp_card(
                play,
                _card_path(play, league, images_dir),
                league=league,
                fetch_logos=fetch_logos,
            )
            media = [png]
        except Exception as exc:  # noqa: BLE001
            print(f"X: card render failed: {exc}", flush=True)
        try:
            if posted and not dry_run:
                time.sleep(1.1)
            result = send(text, dry_run=dry_run, media_paths=media)
        except Exception as exc:  # noqa: BLE001
            print(f"X: tweet failed: {exc}", flush=True)
            return {
                "posted": posted,
                "skipped": skipped,
                "reason": "error",
                "texts": texts,
                "images": images,
            }
        posted_log.keys.append(key)
        already.add(key)
        posted += 1
        texts.append(text)
        if media:
            images.append(str(media[0]))
        url = getattr(result, "url", "") or ""
        print(f"X: posted {url or '(dry-run)'}  {resolve_play_label(play)}")

    if posted and not dry_run:
        posted_log.save()
    elif dry_run:
        for text in texts:
            print(text)
            print("---")
        print(f"X dry-run: {posted} A+ tweet(s), skipped {skipped}.")

    if posted == 0 and skipped:
        print(f"X: nothing new to tweet ({skipped} already posted or started).")
    return {
        "posted": posted,
        "skipped": skipped,
        "reason": "ok" if posted else "noop",
        "texts": texts,
        "images": images,
    }
