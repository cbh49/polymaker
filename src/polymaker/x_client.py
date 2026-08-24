"""Post tweets to X (Twitter) via OAuth 1.0a user context (text only)."""

from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Any

try:
    import tweepy
except Exception:  # pragma: no cover
    tweepy = None


REQUIRED_ENV = (
    "X_API_KEY",
    "X_API_KEY_SECRET",
    "X_ACCESS_TOKEN",
    "X_ACCESS_TOKEN_SECRET",
)


@dataclass(frozen=True)
class PostResult:
    id: str
    text: str
    url: str


def _env(name: str) -> str:
    return (os.getenv(name) or "").strip()


def credentials_ready() -> bool:
    return all(_env(k) for k in REQUIRED_ENV)


def load_x_credentials() -> dict[str, str]:
    missing = [k for k in REQUIRED_ENV if not _env(k)]
    if missing:
        raise ValueError("Missing X OAuth 1.0a credentials in .env: " + ", ".join(missing))
    return {k: _env(k) for k in REQUIRED_ENV}


def get_tweepy_client() -> Any:
    """v2 Client for create_tweet."""
    if tweepy is None:
        raise RuntimeError("tweepy is not installed — pip install tweepy")
    creds = load_x_credentials()
    return tweepy.Client(
        consumer_key=creds["X_API_KEY"],
        consumer_secret=creds["X_API_KEY_SECRET"],
        access_token=creds["X_ACCESS_TOKEN"],
        access_token_secret=creds["X_ACCESS_TOKEN_SECRET"],
        wait_on_rate_limit=False,
    )


def _forbidden_detail(exc: BaseException) -> str:
    resp = getattr(exc, "response", None)
    if resp is None:
        return str(exc)
    body = getattr(resp, "text", "") or ""
    return f"{exc} | body={body[:500]}"


def post_tweet(text: str, *, dry_run: bool = False) -> PostResult:
    """Create a post on X as the authenticated user.

    dry_run: print payload, do not call the API.
    """
    body = (text or "").strip()
    if not body:
        raise ValueError("Tweet text is empty")

    if dry_run:
        print(f"[dry-run] would post ({len(body)} chars)")
        print(body)
        return PostResult(id="dry-run", text=body, url="(dry-run)")

    client = get_tweepy_client()
    try:
        resp = client.create_tweet(text=body)
    except Exception as e:
        raise RuntimeError(f"create_tweet failed: {_forbidden_detail(e)}") from e

    data = getattr(resp, "data", None) or {}
    tweet_id = str(data["id"])
    url = f"https://x.com/i/web/status/{tweet_id}"
    return PostResult(id=tweet_id, text=body, url=url)
