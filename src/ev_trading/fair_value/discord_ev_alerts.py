"""Post sportsbook +EV plays to a Discord incoming webhook.

Missing webhook, network errors, and Discord 4xx never fail the EV pipeline.

  DISCORD_EV_WEBHOOK_URL   required for live posts
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

from ev_trading.fair_value.ev_alerts import (
    SportsbookEvAlert,
    book_label,
    format_american,
    format_ev_message,
    load_env,
)

WEBHOOK_ENV = "DISCORD_EV_WEBHOOK_URL"
COLOR_EV = 0x00E676
USERNAME = "+EV Plays"


def webhook_url_from_env() -> str | None:
    load_env()
    raw = (os.environ.get(WEBHOOK_ENV) or "").strip()
    if raw and "discord.com/api/webhooks/" in raw:
        return raw
    return None


def build_embed(alert: SportsbookEvAlert) -> dict[str, Any]:
    fields: list[dict[str, Any]] = [
        {"name": "PLAY", "value": f">>> **{alert.title}**", "inline": False},
        {"name": "Odds", "value": format_american(alert.book_odds), "inline": True},
        {"name": "Book", "value": book_label(alert.book), "inline": True},
        {
            "name": "Implied Fair Price",
            "value": format_american(float(alert.fair_american)),
            "inline": True,
        },
    ]
    return {
        "title": "+EV Play🚨",
        "description": f"**{alert.matchup}**",
        "color": COLOR_EV,
        "fields": fields,
        "image": {"url": "attachment://ev_play.png"},
    }


def post_ev_discord(
    alert: SportsbookEvAlert,
    image_path: Path,
    *,
    webhook_url: str | None = None,
    dry_run: bool = False,
) -> dict[str, Any]:
    embed = build_embed(alert)
    payload = {
        "username": USERNAME,
        "content": format_ev_message(alert),
        "embeds": [embed],
    }
    if dry_run:
        print(json.dumps(payload, indent=2))
        print(f"Discord dry-run: {alert.title}")
        return {"posted": 0, "reason": "dry_run"}

    url = webhook_url or webhook_url_from_env()
    if not url:
        print("Discord EV: no DISCORD_EV_WEBHOOK_URL (skipping).")
        return {"posted": 0, "reason": "no_webhook"}
    if not image_path.is_file():
        print(f"Discord EV: missing graphic {image_path}")
        return {"posted": 0, "reason": "no_image"}

    try:
        import requests

        multipart = [
            ("payload_json", (None, json.dumps(payload), "application/json")),
            ("files[0]", ("ev_play.png", image_path.read_bytes(), "image/png")),
        ]
        resp = requests.post(url, files=multipart, timeout=60)  # type: ignore[arg-type]
        status = getattr(resp, "status_code", 200)
        if status is not None and int(status) >= 400:
            body = getattr(resp, "text", "")
            print(f"Discord EV error {status}: {body}", flush=True)
            return {"posted": 0, "reason": f"http_{status}"}
        return {"posted": 1, "reason": "ok"}
    except Exception as exc:  # noqa: BLE001
        print(f"Discord EV: {type(exc).__name__}: {exc}", flush=True)
        return {"posted": 0, "reason": "error"}
