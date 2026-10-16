"""Trade a sharp play only when it matches a profitable review system.

Signal keys match review_sharp_trades.extract_signals / pair_keys. A play is
sent when any of those keys is in the latest trade_systems list (slices with
at least +0.10 units per bet). The list is read from the published
sharp_trade_review report, then from config/sharp_trade_systems.json.
"""

from __future__ import annotations

import json
import math
import os
from collections.abc import Sequence
from pathlib import Path
from typing import Any

from polymaker.trading.sharp import SharpPlay, qualifying_trade_data

MIN_UNITS_PER_BET = 0.10
FALLBACK_PATH = Path(__file__).resolve().parents[3] / "config" / "sharp_trade_systems.json"


def gap_band(value: float | None) -> str | None:
    if value is None:
        return None
    if value < 15:
        return "lt_15"
    if value < 25:
        return "15_to_25"
    if value < 40:
        return "25_to_40"
    return "ge_40"


def handle_band(value: float | None) -> str | None:
    if value is None:
        return None
    if value < 40:
        return "lt_40"
    if value < 55:
        return "40_to_55"
    if value < 70:
        return "55_to_70"
    return "ge_70"


def public_band(value: float | None) -> str | None:
    if value is None:
        return None
    if value < 30:
        return "lt_30"
    if value < 45:
        return "30_to_45"
    if value < 55:
        return "45_to_55"
    return "ge_55"


def line_band(value: float | None) -> str | None:
    if value is None:
        return None
    size = abs(value)
    if size == 0:
        return "0"
    if size <= 1:
        return "0_to_1"
    if size <= 2.5:
        return "1_to_2_5"
    return "gt_2_5"


def edge_band(value: float | None) -> str | None:
    if value is None:
        return None
    if value < 1.5:
        return "lt_1_5"
    if value < 3:
        return "1_5_to_3"
    return "ge_3"


def odds_band(american: int | None) -> str | None:
    if american is None:
        return None
    if american <= -200:
        return "heavy_favorite"
    if american <= -110:
        return "favorite"
    if american < 110:
        return "pickem"
    if american < 200:
        return "dog"
    return "long_dog"


def favorite_vs_dog(market: str, line: float | None, american: int | None) -> str | None:
    if market == "spread" and line is not None:
        if line < 0:
            return "favorite"
        if line > 0:
            return "dog"
        return "pick"
    if market == "moneyline" and american is not None:
        if american < 0:
            return "favorite"
        if american > 0:
            return "dog"
    return None


def signal_label(value: Any) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    return str(value)


def pair_keys(signals: dict[str, Any]) -> list[str]:
    pairs = (
        ("tier", "rlm_confirmed", "tier+rlm"),
        ("market", "composite_gap", "market+gap"),
        ("league", "market", "league+market"),
        ("contrarian_public", "plus_pp", "contrarian+plus_pp"),
    )
    keys: list[str] = []
    for left, right, label in pairs:
        if left not in signals or right not in signals:
            continue
        keys.append(
            f"pair:{label}:{signal_label(signals[left])}|{signal_label(signals[right])}"
        )
    return keys


def order_snapshot(play: dict[str, Any]) -> dict[str, Any] | None:
    """Signal row for the order the bot would send.

    A moneyline tagged trade_market=spread is judged as that spread.
    trade_market=skip has no order.
    """
    raw = dict(play)
    trade_market = str(raw.get("trade_market") or "").strip().lower()
    if trade_market == "skip":
        return None
    if trade_market != "spread":
        return raw
    try:
        line = float(raw["trade_line"])
    except (KeyError, TypeError, ValueError):
        return None
    if not math.isfinite(line):
        return None
    home_away = str(raw.get("trade_home_away") or raw.get("home_away") or "").strip().lower()
    ordered = dict(raw)
    ordered["signal_market"] = raw.get("signal_market") or raw.get("market")
    ordered["signal_open"] = raw.get("open")
    ordered["signal_live"] = raw.get("live")
    ordered["market"] = "spread"
    ordered["open"] = line
    ordered["live"] = line
    ordered["play_line"] = line
    if raw.get("trade_odds") is not None:
        ordered["play_odds"] = raw.get("trade_odds")
    if home_away:
        ordered["home_away"] = home_away
    return ordered


def signal_keys(play: dict[str, Any], *, league: str | None = None) -> list[str]:
    """Review slice keys for the order snapshot. Empty when the play is skipped."""
    snap = order_snapshot(play)
    if snap is None:
        return []
    if league and not snap.get("league"):
        snap = dict(snap)
        snap["league"] = league
    signals = _signals(snap)
    keys = [f"{key}:{signal_label(value)}" for key, value in signals.items()]
    keys.extend(pair_keys(signals))
    return keys


def system_key_set(systems: Sequence[dict[str, Any] | str]) -> set[str]:
    keys: set[str] = set()
    for item in systems:
        if isinstance(item, str) and item.strip():
            keys.add(item.strip())
        elif isinstance(item, dict) and str(item.get("key") or "").strip():
            keys.add(str(item["key"]).strip())
    return keys


def classify_play(
    play: dict[str, Any],
    systems: Sequence[dict[str, Any] | str],
    *,
    league: str | None = None,
) -> dict[str, Any]:
    """traded when any signal key is a profitable system. Otherwise dropped."""
    allowed = system_key_set(systems)
    if play.get("qualified") is False:
        reasons = [str(item) for item in (play.get("reject_reasons") or []) if str(item) != "line_move"]
        return {
            "decision": "dropped",
            "matched_systems": [],
            "drop_reasons": reasons or ["unqualified"],
            "trade_data": _json_safe(play),
        }
    snap = order_snapshot(play)
    if snap is None:
        return {
            "decision": "dropped",
            "matched_systems": [],
            "drop_reasons": ["trade_market_skip"],
            "trade_data": _json_safe(play),
        }
    if league and not snap.get("league"):
        snap = dict(snap)
        snap["league"] = league
    keys = signal_keys(snap)
    matched = [key for key in keys if key in allowed]
    if not allowed:
        return {
            "decision": "dropped",
            "matched_systems": [],
            "drop_reasons": ["no_trade_systems"],
            "trade_data": _json_safe(snap),
        }
    if matched:
        return {
            "decision": "traded",
            "matched_systems": matched,
            "drop_reasons": [],
            "trade_data": _json_safe(snap),
        }
    return {
        "decision": "dropped",
        "matched_systems": [],
        "drop_reasons": ["review_gate"],
        "trade_data": _json_safe(snap),
    }


def filter_sharp_plays(
    plays: Sequence[SharpPlay],
    systems: Sequence[dict[str, Any] | str],
) -> list[SharpPlay]:
    """Keep plays whose order matches a profitable system."""
    kept: list[SharpPlay] = []
    for play in plays:
        verdict = classify_play(qualifying_trade_data(play), systems, league=play.league)
        if verdict["decision"] == "traded":
            kept.append(play)
    return kept


def split_slate(
    output: dict[str, Any],
    systems: Sequence[dict[str, Any] | str],
    *,
    logged_at: int,
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    """Tradable slate for alerts, plus one Convex row for every finder play."""
    league = str(output.get("league") or "")
    slate_date = output.get("date")
    tradable: list[dict[str, Any]] = []
    rows: list[dict[str, Any]] = []
    for play in output.get("plays") or []:
        if not isinstance(play, dict):
            continue
        verdict = classify_play(play, systems, league=league)
        rows.append(_candidate_row(play, verdict, league=league, slate_date=slate_date, logged_at=logged_at))
        if verdict["decision"] == "traded":
            tradable.append(play)
    posted = dict(output)
    posted["plays"] = tradable
    posted["play_count"] = len(tradable)
    return posted, rows


def load_trade_systems() -> list[dict[str, Any]]:
    """Published review systems, or the checked-in fallback."""
    remote = _fetch_remote_systems()
    if remote:
        return remote
    return _fallback_systems()


def _signals(play: dict[str, Any]) -> dict[str, Any]:
    market = str(play.get("market") or "").strip().lower()
    public = _as_float(play.get("public_bet_pct"))
    handle = _as_float(play.get("handle_bet_pct"))
    plus_pp = handle - public if handle is not None and public is not None else None
    if plus_pp is None:
        plus_pp = _as_float(play.get("primary_gap"))
    line = _as_float(play.get("live"))
    if line is None:
        line = _as_float(play.get("play_line"))
    odds = _american(play.get("play_odds"))
    if market == "moneyline" and odds is None:
        live_odds = _american(play.get("live"))
        if live_odds is not None and abs(live_odds) >= 100:
            odds = live_odds
    elif odds is None:
        odds = -110
    home_away = str(play.get("home_away") or "").strip().lower() or None
    moved_toward = str(play.get("line_moved_toward") or "").strip().lower()
    toward_bet: bool | None = None
    if moved_toward and home_away:
        toward_bet = moved_toward == home_away
    confirmation = play.get("exchange_confirmation")
    edge = None
    if isinstance(confirmation, dict):
        edge = _as_float(confirmation.get("exchange_edge_pct"))
    if edge is None:
        edge = _as_float(play.get("exchange_edge_pct"))
    low_volume = play.get("low_volume_dog_flag")
    if not isinstance(low_volume, bool):
        low_volume = None
    rlm = play.get("rlm_confirmed")
    if not isinstance(rlm, bool):
        rlm = None
    league = str(play.get("league") or "").strip().upper() or None
    signals: dict[str, Any] = {}

    def put(key: str, value: Any) -> None:
        if value is None or value == "":
            return
        signals[key] = value

    put("league", league)
    put("market", market or None)
    put("tier", str(play.get("tier") or "").strip().upper() or None)
    put("rlm_confirmed", rlm)
    put("n_sources", _as_int(play.get("n_sources_agreeing")))
    put("agreeing_sources", _source_key(play.get("agreeing_sources")))
    put("composite_gap", gap_band(_as_float(play.get("composite_gap"))))
    put("primary_gap", gap_band(_as_float(play.get("primary_gap"))))
    put("vsin_gap", gap_band(_as_float(play.get("vsin_gap"))))
    put("sbd_gap", gap_band(_as_float(play.get("sbd_gap"))))
    put("plus_pp", gap_band(plus_pp))
    put("handle_pct", handle_band(handle))
    put("public_pct", public_band(public))
    put("contrarian_public", public < 50 if public is not None else None)
    put("line_toward_bet", toward_bet)
    put("line_move_abs", line_band(_as_float(play.get("line_move"))))
    put("favorite_vs_dog", favorite_vs_dog(market, line, odds))
    put("odds_band", odds_band(odds))
    put("low_volume_dog", low_volume)
    put("exchange_edge", edge_band(edge))
    put("bet_side", home_away)
    return signals


def _candidate_row(
    play: dict[str, Any],
    verdict: dict[str, Any],
    *,
    league: str,
    slate_date: Any,
    logged_at: int,
) -> dict[str, Any]:
    date_s = str(slate_date or play.get("date") or "")
    return {
        "candidateKey": _candidate_key(play, league, date_s),
        "league": league or str(play.get("league") or ""),
        "slateDate": date_s,
        "matchup": str(play.get("matchup") or ""),
        "market": str(play.get("market") or ""),
        "side": str(play.get("side") or ""),
        "decision": verdict["decision"],
        "dropReasons": list(verdict["drop_reasons"]),
        "matchedSystems": list(verdict["matched_systems"]),
        "tradeData": verdict["trade_data"],
        "loggedAt": logged_at,
    }


def _candidate_key(play: dict[str, Any], league: str, slate_date: str) -> str:
    return "|".join(
        [
            league,
            slate_date,
            str(play.get("event_id") or play.get("matchup") or ""),
            str(play.get("signal_market") or play.get("market") or ""),
            str(play.get("home_away") or play.get("side") or ""),
        ]
    )


def _fetch_remote_systems() -> list[dict[str, Any]] | None:
    try:
        import requests
    except ImportError:
        return None
    url = (os.environ.get("CONVEX_HTTP_URL") or os.environ.get("CONVEX_SITE_URL") or "").rstrip("/")
    token = (os.environ.get("CONVEX_PUBLISH_TOKEN") or os.environ.get("PUBLISH_TOKEN") or "").strip()
    if not url or not token:
        return None
    try:
        response = requests.get(
            f"{url}/reports",
            params={"reportType": "sharp_trade_review"},
            headers={"Authorization": f"Bearer {token}", "Accept": "application/json"},
            timeout=20,
        )
    except requests.RequestException:
        return None
    if response.status_code != 200:
        return None
    try:
        body = response.json()
    except ValueError:
        return None
    payload = body.get("payload") if isinstance(body, dict) else None
    systems = payload.get("trade_systems") if isinstance(payload, dict) else None
    return _clean_systems(systems)


def _fallback_systems() -> list[dict[str, Any]]:
    if not FALLBACK_PATH.is_file():
        return []
    try:
        raw = json.loads(FALLBACK_PATH.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return []
    systems = raw.get("systems") if isinstance(raw, dict) else raw
    return _clean_systems(systems) or []


def _clean_systems(systems: Any) -> list[dict[str, Any]] | None:
    if not isinstance(systems, list):
        return None
    out: list[dict[str, Any]] = []
    for item in systems:
        if isinstance(item, str) and item.strip():
            out.append({"key": item.strip()})
        elif isinstance(item, dict) and str(item.get("key") or "").strip():
            out.append({"key": str(item["key"]).strip(), **{k: v for k, v in item.items() if k != "key"}})
    return out or None


def _source_key(value: Any) -> str | None:
    if not isinstance(value, list):
        return None
    names = sorted({str(item).strip().lower() for item in value if str(item).strip()})
    return "+".join(names) if names else None


def _as_float(value: Any) -> float | None:
    if isinstance(value, bool) or value is None:
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(number):
        return None
    return number


def _as_int(value: Any) -> int | None:
    number = _as_float(value)
    if number is None:
        return None
    return int(number)


def _american(value: Any) -> int | None:
    number = _as_float(value)
    if number is None:
        return None
    return int(round(number))


def _json_safe(value: Any) -> Any:
    if isinstance(value, bool) or value is None or isinstance(value, str):
        return value
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        return value if math.isfinite(value) else None
    if isinstance(value, dict):
        return {str(key): _json_safe(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_json_safe(item) for item in value]
    return str(value)
