"""Post tweets to X (Twitter) via OAuth 1.0a user context (text + images).

League jobs outside this satellite should import packages/breton_x instead.
Whale-monitor tweets stay on this client; EV alerts attach a PNG via v2 media upload.
"""

from __future__ import annotations

import mimetypes
import os
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

try:
    import tweepy
except Exception:  # pragma: no cover
    tweepy = None

OAuth1Session: Any = None
try:
    from requests_oauthlib import OAuth1Session as _OAuth1Session
    OAuth1Session = _OAuth1Session
except Exception:  # pragma: no cover
    pass


REQUIRED_ENV = (
    "X_API_KEY",
    "X_API_KEY_SECRET",
    "X_ACCESS_TOKEN",
    "X_ACCESS_TOKEN_SECRET",
)

V2_MEDIA_UPLOAD_URL = "https://api.x.com/2/media/upload"


@dataclass(frozen=True)
class PostResult:
    id: str
    text: str
    url: str


def _env(name: str) -> str:
    return (os.getenv(name) or "").strip()


def x_posts_enabled() -> bool:
    """Kill switch: X_POSTS=0/false/off skips the live API."""
    value = _env("X_POSTS").lower()
    return value not in ("0", "false", "no", "off")


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


def get_oauth1_session() -> Any:
    if OAuth1Session is None:
        raise RuntimeError("requests-oauthlib is not installed — pip install requests-oauthlib")
    creds = load_x_credentials()
    return OAuth1Session(
        client_key=creds["X_API_KEY"],
        client_secret=creds["X_API_KEY_SECRET"],
        resource_owner_key=creds["X_ACCESS_TOKEN"],
        resource_owner_secret=creds["X_ACCESS_TOKEN_SECRET"],
    )


def _forbidden_detail(exc: BaseException) -> str:
    resp = getattr(exc, "response", None)
    if resp is None:
        return str(exc)
    body = getattr(resp, "text", "") or ""
    return f"{exc} | body={body[:500]}"


def _guess_media_type(path: Path) -> str:
    guessed, _ = mimetypes.guess_type(str(path))
    return guessed or "application/octet-stream"


def upload_media(paths: Sequence[Path]) -> list[str]:
    session = get_oauth1_session()
    media_ids: list[str] = []
    for path in list(paths)[:4]:
        p = Path(path)
        if not p.is_file():
            raise FileNotFoundError(f"Media file not found: {p}")
        media_type = _guess_media_type(p)
        with p.open("rb") as f:
            resp = session.post(
                V2_MEDIA_UPLOAD_URL,
                files={"media": (p.name, f, media_type)},
                data={
                    "media_category": "tweet_image",
                    "media_type": media_type,
                },
                timeout=120,
            )
        if resp.status_code >= 400:
            raise RuntimeError(
                f"v2 media upload failed for {p.name}: "
                f"{resp.status_code} {resp.text[:500]}"
            )
        payload = resp.json()
        data = payload.get("data") if isinstance(payload, dict) else None
        media_id = None
        if isinstance(data, dict):
            media_id = data.get("id") or data.get("media_id_string") or data.get("media_id")
        if media_id is None and isinstance(payload, dict):
            media_id = payload.get("id") or payload.get("media_id_string")
        if media_id is None:
            raise RuntimeError(f"v2 media upload missing id in response: {payload}")
        media_ids.append(str(media_id))
    return media_ids


def post_tweet(
    text: str,
    *,
    dry_run: bool = False,
    media_paths: Sequence[Path] | None = None,
    allow_text_fallback: bool = True,
) -> PostResult:
    """Create a post on X as the authenticated user.

    dry_run: print payload, do not call the API.
    """
    body = (text or "").strip()
    if not body:
        raise ValueError("Tweet text is empty")

    paths = [Path(p) for p in (media_paths or []) if p]
    if dry_run or not x_posts_enabled():
        media_note = ", ".join(str(p) for p in paths) if paths else "(none)"
        mode = "dry-run" if dry_run else "X_POSTS=0"
        print(f"[{mode}] would post ({len(body)} chars), media={media_note}")
        print(body)
        return PostResult(id=mode, text=body, url=f"({mode})")

    client = get_tweepy_client()
    media_ids: list[str] | None = None
    if paths:
        try:
            media_ids = upload_media(paths)
        except Exception as e:
            detail = _forbidden_detail(e) if not str(e).startswith("v2 media") else str(e)
            if allow_text_fallback:
                print(
                    "media upload blocked — posting text only.\n"
                    f"  detail: {detail}",
                    flush=True,
                )
                media_ids = None
            else:
                raise RuntimeError(f"media upload failed: {detail}") from e

    try:
        resp = client.create_tweet(text=body, media_ids=media_ids or None)
    except Exception as e:
        raise RuntimeError(f"create_tweet failed: {_forbidden_detail(e)}") from e

    data = getattr(resp, "data", None) or {}
    tweet_id = str(data["id"])
    url = f"https://x.com/i/web/status/{tweet_id}"
    return PostResult(id=tweet_id, text=body, url=url)
