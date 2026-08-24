"""Format and post X tweets for detected Polymarket whale trades."""

from __future__ import annotations

import os
from typing import Any

from detector import Signal
from registry import WatchedMarket

TWEET_CHAR_LIMIT = 280

_TWO_WORD_NICKS = (
    "red sox",
    "white sox",
    "blue jays",
    "trail blazers",
)

_CTA = "Follow to track the biggest whale trades as they happen"
_HASHTAG_TAIL = "#Polymarket #Gambling𝕏 #SportsBettingX"


def whale_posts_enabled() -> bool:
    return os.environ.get("X_WHALE_POSTS", "").strip().lower() in {"1", "true", "yes"}


def team_nickname(outcome: str) -> str:
    """Short team nick for tweet copy: 'Chicago Cubs' → 'Cubs'."""
    name = (outcome or "").strip()
    if not name:
        return ""
    lower = name.lower()
    for nick in _TWO_WORD_NICKS:
        if lower.endswith(nick):
            return nick.title()
    return name.rsplit(" ", 1)[-1]


def play_label(market: WatchedMarket | None, sig: Signal) -> str:
    outcome = ""
    if market is not None:
        try:
            outcome = market.outcome_for_side(sig.side)
        except ValueError:
            outcome = ""
    if not outcome or outcome.lower() in {"yes", "no"}:
        fallback = (sig.label or (market.label if market else "") or "ML").strip()
        if fallback.upper().endswith(" ML"):
            return fallback
        return f"{fallback} ML" if fallback != "ML" else "ML"
    return f"{team_nickname(outcome)} ML"


def format_amount(size_usd: float) -> str:
    return f"${int(round(float(size_usd))):,}"


def format_price(price: float) -> str:
    """Polymarket 0–1 price as cents, e.g. 0.46 → 46¢."""
    cents = int(round(float(price) * 100))
    return f"{cents}¢"


def format_whale_tweet(sig: Signal, market: WatchedMarket | None = None) -> str:
    league = (sig.league or "").strip().upper() or "MLB"
    detail = sig.detail or {}
    size_usd = float(detail.get("size_usd") or 0)
    raw_price = detail.get("price")
    price = float(raw_price) if raw_price is not None else 0.0
    play = play_label(market, sig)
    body = (
        f"🐋 WHALE {league} PLAY\n"
        f"\n"
        f"A Polymarket trader just placed {format_amount(size_usd)} on {play} at {format_price(price)}\n"
        f"\n"
        f"{_CTA}\n"
        f"\n"
        f"#{league} {_HASHTAG_TAIL}"
    )
    if len(body) > TWEET_CHAR_LIMIT:
        body = body[:TWEET_CHAR_LIMIT]
    return body


def maybe_post_whale(
    sig: Signal,
    market: WatchedMarket | None = None,
    *,
    dry_run: bool = False,
    poster: Any = None,
) -> dict[str, Any] | None:
    """Post a whale tweet if the kill switch and credentials are set.

    Never raises — logs and returns a status dict. `poster` is injectable for tests.
    """
    if sig.signal_type != "whale_trade":
        return None
    if not whale_posts_enabled() and not dry_run:
        print("[whale-tweet] skip: X_WHALE_POSTS is not enabled")
        return {"action": "skipped", "detail": "X_WHALE_POSTS disabled"}

    from polymaker.x_client import credentials_ready, post_tweet

    if poster is None and not credentials_ready() and not dry_run:
        print("[whale-tweet] skip: X OAuth credentials missing")
        return {"action": "skipped", "detail": "missing X credentials"}

    text = format_whale_tweet(sig, market)
    send = poster if poster is not None else post_tweet
    try:
        result = send(text, dry_run=dry_run)
    except Exception as exc:  # noqa: BLE001
        print(f"[whale-tweet] post failed: {exc}")
        return {"action": "failed", "detail": str(exc), "text": text}

    url = getattr(result, "url", "") or ""
    print(f"[whale-tweet] posted {url or '(dry-run)'}")
    return {
        "action": "dry_run" if dry_run else "posted",
        "url": url,
        "text": text,
    }
