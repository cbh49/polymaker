#!/usr/bin/env python3
"""POST sharp-money plays to Convex for the dashboard board.

Called from find_sharp_money.py, discord_sharp_alerts.py, and
scripts/run_sharp_pipeline.py. Missing credentials, network errors, and
Convex 4xx never fail the finder.

  CONVEX_HTTP_URL         https://<deployment>.convex.site
  CONVEX_PUBLISH_TOKEN    Bearer token matching dashboard PUBLISH_TOKEN
"""

from __future__ import annotations

import json
import math
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from discord_sharp_alerts import play_key, resolve_play_label

try:
    from dotenv import load_dotenv
except ImportError:
    load_dotenv = None  # type: ignore[misc, assignment]

SCRIPT_DIR = Path(__file__).resolve().parent
BOT_ROOT = SCRIPT_DIR.parent
PAGE_TZ = ZoneInfo("America/Los_Angeles")
KICKOFF_TZ = ZoneInfo("America/New_York")


def _load_env() -> None:
    if load_dotenv is None:
        return
    load_dotenv(BOT_ROOT / ".env")
    load_dotenv()


def _as_float(value: Any) -> float | None:
    if value is None or value == "":
        return None
    try:
        out = float(value)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(out):
        return None
    return out


def _as_int(value: Any) -> int | None:
    num = _as_float(value)
    if num is None:
        return None
    return int(round(num))


def _as_str(value: Any) -> str:
    if value is None:
        return ""
    text = str(value).strip()
    if text.lower() in {"nan", "none", "null"}:
        return ""
    return text


def _league_key(value: Any) -> str:
    key = _as_str(value).upper()
    if key == "CFB":
        return "NCAAF"
    return key


def start_time_ms(play: dict[str, Any]) -> int:
    utc = _as_str(play.get("game_time_utc"))
    if utc:
        try:
            dt = datetime.fromisoformat(utc.replace("Z", "+00:00"))
            return int(dt.timestamp() * 1000)
        except ValueError:
            pass

    date_s = _as_str(play.get("date"))
    local = _as_str(play.get("game_time_local"))
    clock = "07:00PM"
    if "," in local:
        clock = local.split(",", 1)[1].strip().replace(" ", "")
        if len(clock) >= 6 and clock[-2:] in {"AM", "PM"} and ":" in clock:
            clock = f"{clock[:-2]} {clock[-2:]}"
    if date_s:
        for fmt in ("%Y-%m-%d %I:%M%p", "%Y-%m-%d %I:%M %p"):
            try:
                dt = datetime.strptime(f"{date_s} {clock}", fmt).replace(tzinfo=KICKOFF_TZ)
                return int(dt.timestamp() * 1000)
            except ValueError:
                continue
        try:
            dt = datetime.fromisoformat(date_s[:10]).replace(hour=19, tzinfo=PAGE_TZ)
            return int(dt.timestamp() * 1000)
        except ValueError:
            pass
    return int((datetime.now(timezone.utc) + timedelta(hours=36)).timestamp() * 1000)


def convex_body_for_play(
    play: dict[str, Any],
    *,
    league: str | None = None,
    posted_at: int | None = None,
) -> dict[str, Any] | None:
    """JSON body for POST /sharp-money-plays."""
    posted_at = posted_at if posted_at is not None else int(datetime.now(timezone.utc).timestamp() * 1000)
    start = start_time_ms(play)
    # Pregame skip is off so in-progress games still land on the board.
    # if start <= posted_at:
    #     return None

    league_s = _league_key(league or play.get("league"))
    away = _as_str(play.get("away_team"))
    home = _as_str(play.get("home_team"))
    matchup = _as_str(play.get("matchup"))
    display = _as_str(play.get("matchup_display"))
    if away and home:
        if not display:
            display = f"{away} @ {home}"
        if not matchup:
            matchup = display
    elif not matchup:
        matchup = display or "Matchup"
        display = matchup
    else:
        display = display or matchup

    agreeing = play.get("agreeing_sources") or []
    if not isinstance(agreeing, list):
        agreeing = []
    sources = [_as_str(s) for s in agreeing if _as_str(s)]

    conf = play.get("exchange_confirmation")
    conf = conf if isinstance(conf, dict) else {}
    home_away = _as_str(play.get("home_away")).lower()
    market = _as_str(play.get("market")).lower() or "moneyline"

    body: dict[str, Any] = {
        "playKey": play_key(play, league_s),
        "league": league_s or "UNK",
        "matchup": matchup,
        "matchupDisplay": display,
        "awayTeam": away,
        "homeTeam": home,
        "market": market,
        "side": _as_str(play.get("side")) or resolve_play_label(play),
        "homeAway": home_away,
        "playLabel": resolve_play_label(play),
        "tier": _as_str(play.get("tier")).upper() or "B",
        "agreeingSources": sources,
        "rlmConfirmed": bool(play.get("rlm_confirmed")),
        "postedAt": posted_at,
        "startTime": start,
    }

    optional_nums = {
        "playLine": play.get("play_line"),
        "playOdds": play.get("play_odds"),
        "open": play.get("open"),
        "live": play.get("live"),
        "openOdds": play.get("open_odds"),
        "liveOdds": play.get("live_odds"),
        "lineMove": play.get("line_move"),
        "publicBetPct": play.get("public_bet_pct"),
        "handleBetPct": play.get("handle_bet_pct"),
        "publicFavorsBetPct": play.get("public_favors_bet_pct"),
        "vsinPublicBetPct": play.get("vsin_public_bet_pct"),
        "vsinHandleBetPct": play.get("vsin_handle_bet_pct"),
        "sbdPublicBetPct": play.get("sbd_public_bet_pct"),
        "sbdHandleBetPct": play.get("sbd_handle_bet_pct"),
        "compositeGap": play.get("composite_gap"),
        "impliedFairProb": play.get("implied_fair_prob"),
        "exchangeEdgePct": conf.get("exchange_edge_pct"),
    }
    for key, raw in optional_nums.items():
        num = _as_float(raw)
        if num is not None:
            body[key] = num

    confidence = _as_int(play.get("model_confidence"))
    if confidence is not None:
        body["modelConfidence"] = confidence
    n_src = _as_int(play.get("n_sources_agreeing"))
    if n_src is not None:
        body["nSourcesAgreeing"] = n_src

    public_name = _as_str(play.get("public_favors_name"))
    if public_name:
        body["publicFavorsName"] = public_name
    rlm_src = _as_str(play.get("rlm_source_used"))
    if rlm_src:
        body["rlmSourceUsed"] = rlm_src
    local = _as_str(play.get("game_time_local"))
    if local:
        body["gameTimeLocal"] = local
    date_s = _as_str(play.get("date"))
    if date_s:
        body["date"] = date_s
    return body


def _credentials() -> tuple[str, str]:
    _load_env()
    url = (os.environ.get("CONVEX_HTTP_URL") or os.environ.get("CONVEX_SITE_URL") or "").rstrip("/")
    token = (os.environ.get("CONVEX_PUBLISH_TOKEN") or os.environ.get("PUBLISH_TOKEN") or "").strip()
    return url, token


def _post_plays(plays: list[dict[str, Any]]) -> dict[str, Any]:
    """POST /sharp-money-plays without importing polymaker (finder can run standalone)."""
    import requests

    url, token = _credentials()
    if not url or not token:
        raise RuntimeError("CONVEX_HTTP_URL / CONVEX_PUBLISH_TOKEN missing")
    resp = requests.post(
        f"{url}/sharp-money-plays",
        headers={
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
            "Accept": "application/json",
        },
        data=json.dumps({"plays": plays}),
        timeout=30,
    )
    if resp.status_code != 200:
        raise RuntimeError(f"Convex /sharp-money-plays HTTP {resp.status_code}: {resp.text[:300]}")
    try:
        payload = resp.json()
    except ValueError as exc:
        raise RuntimeError("Convex /sharp-money-plays returned non-JSON") from exc
    if not isinstance(payload, dict):
        return {"ok": True, "count": len(plays)}
    return payload


def post_sharp_plays(
    output: dict[str, Any],
    *,
    dry_run: bool = False,
) -> dict[str, Any]:
    """POST every play in a finder payload to Convex. Never raises to caller."""
    league = _league_key(output.get("league"))
    plays = [p for p in (output.get("plays") or []) if isinstance(p, dict)]
    if not plays:
        print("Convex sharp: no plays to post.")
        return {"posted": 0, "skipped": 0, "reason": "no_plays"}

    posted_at = int(datetime.now(timezone.utc).timestamp() * 1000)
    bodies: list[dict[str, Any]] = []
    skipped = 0
    for play in plays:
        body = convex_body_for_play(play, league=league, posted_at=posted_at)
        if body is None:
            skipped += 1
            continue
        bodies.append(body)

    if not bodies:
        print(f"Convex sharp: nothing to post ({skipped} skipped).")
        return {"posted": 0, "skipped": skipped, "reason": "empty"}

    if dry_run:
        print(f"Convex sharp dry-run: {len(bodies)} play(s), skipped {skipped}.")
        return {"posted": 0, "skipped": skipped, "reason": "dry_run", "plays": bodies}

    try:
        raw = _post_plays(bodies)
    except Exception as exc:  # noqa: BLE001
        print(f"Convex sharp failed: {exc}", flush=True)
        return {"posted": 0, "skipped": skipped, "reason": "error", "error": str(exc)}

    inserted = int(raw.get("inserted") or 0)
    count = int(raw.get("count") or len(bodies))
    print(f"Convex sharp: posted {count} play(s) ({inserted} new), skipped {skipped}.")
    return {"posted": count, "skipped": skipped, "inserted": inserted, "reason": "ok"}


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description="Post sharp-money JSON plays to Convex")
    parser.add_argument("--input", type=Path, action="append", default=None)
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()
    paths = args.input or [
        SCRIPT_DIR / "output" / "ncaaf_sharp_money.json",
        SCRIPT_DIR / "output" / "nfl_sharp_money.json",
        SCRIPT_DIR / "output" / "mlb_sharp_money.json",
        SCRIPT_DIR / "output" / "wnba_sharp_money.json",
    ]
    for path in paths:
        if not path.is_file():
            print(f"skip {path}: not found")
            continue
        payload = json.loads(path.read_text(encoding="utf-8"))
        print(f"=== {path.name} ===")
        post_sharp_plays(payload, dry_run=args.dry_run)
