"""Trade a sharp play only when it matches a profitable review system.

Signal keys match review_sharp_trades.extract_signals / pair_keys. A play is
sent when any of those keys clears the trading bar: at least 40 decided bets,
+0.10 units per bet, and a Wilson win-rate floor of 0.50. An exchange-edge
band may also clear a lower bar (60 decided, +0.05 units, Wilson 0.45).
League, market, and price-side slices never qualify. Market × gap cells are
judged by the primary bar. A play that matches a positive key is still
dropped when it also matches an avoid key (40 decided, −0.10 units per bet
or worse). League overrides return before that check. Both lists are read
from the published sharp_trade_review report, then from
config/sharp_trade_systems.json.

Three league overrides are explicit exceptions, not systems, and they are
not published into that list. A qualified NCAAB order passes. A qualified
NCAAF spread passes, and an NCAAF total passes only when its edge is blank
or under 1.5 and its composite gap is 25 or more. An NBA spread under 1.5
edge passes on top of the system list. NFL and NHL use the list only.
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
MIN_TRADE_DECIDED = 40
MIN_WILSON_LOWER = 0.50
# Keep in sync with review_sharp_trades. Exchange-edge-only second tier.
EDGE_MIN_UNITS_PER_BET = 0.05
EDGE_MIN_TRADE_DECIDED = 60
EDGE_MIN_WILSON_LOWER = 0.45
WILSON_Z = 1.96
# Keep in sync with review_sharp_trades.EXCLUDED_TRADE_FAMILIES.
EXCLUDED_TRADE_FAMILIES = frozenset(
    {
        "league",
        "market",
        "bet_side",
        "pair:league+market",
        "favorite_vs_dog",
        "odds_band",
        "low_volume_dog",
    }
)
# Keep this tuple identical to review_sharp_trades.METRIC_FAMILIES.
METRIC_FAMILIES = (
    "tier",
    "rlm_confirmed",
    "n_sources",
    "agreeing_sources",
    "composite_gap",
    "primary_gap",
    "vsin_gap",
    "sbd_gap",
    "sao_gap",
    "plus_pp",
    "handle_pct",
    "public_pct",
    "contrarian_public",
    "line_toward_bet",
    "line_move_abs",
    "low_volume_dog",
    "exchange_edge",
    "rlm_source_used",
    "exchange_rlm_confirmed",
    "low_liquidity",
    "sbd_override",
    "ml_spread_divergence",
    "model_confidence",
    "source_dk",
    "source_sbd",
    "source_vsin",
    "source_sao",
)
CONTEXT_FAMILIES = frozenset(
    {"league", "market", "bet_side", "favorite_vs_dog", "odds_band"}
)
AVOID_MAX_UNITS_PER_BET = -0.10
_SOURCE_FLAGS = (
    ("source_dk", frozenset({"primary", "dk", "draftkings", "playerprops"})),
    ("source_sbd", frozenset({"sbd", "sportsbettingdime"})),
    ("source_vsin", frozenset({"vsin"})),
    ("source_sao", frozenset({"sao", "scoresandodds"})),
)
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
        ("exchange_edge", "line_move_abs", "exchange_edge+line_move_abs"),
        ("exchange_edge", "sbd_gap", "exchange_edge+sbd_gap"),
    )
    keys: list[str] = []
    for left, right, label in pairs:
        if left not in signals or right not in signals:
            continue
        keys.append(
            f"pair:{label}:{signal_label(signals[left])}|{signal_label(signals[right])}"
        )
    league = signals.get("league")
    if league is not None:
        for metric in METRIC_FAMILIES:
            if metric not in signals:
                continue
            keys.append(
                f"pair:league+{metric}:{signal_label(league)}|{signal_label(signals[metric])}"
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


def slice_family(key: str) -> str:
    """Family of a review slice key. Pairs keep the pair label, not the bands."""
    if key.startswith("pair:"):
        label, _, _ = key[len("pair:") :].partition(":")
        return f"pair:{label}" if label else "pair:"
    family, _, _ = key.partition(":")
    return family


def tradable_family(key: str) -> bool:
    """True when this key is a metric the bot may trade or avoid."""
    family = slice_family(key)
    if family in EXCLUDED_TRADE_FAMILIES:
        return False
    if family.startswith("pair:league+"):
        metric = family[len("pair:league+") :]
        if metric in EXCLUDED_TRADE_FAMILIES or metric in CONTEXT_FAMILIES:
            return False
    return True


def wilson_lower(wins: int, decided: int) -> float | None:
    if decided <= 0:
        return None
    phat = wins / decided
    z2 = WILSON_Z * WILSON_Z
    denom = 1 + z2 / decided
    center = phat + z2 / (2 * decided)
    margin = WILSON_Z * math.sqrt((phat * (1 - phat) + z2 / (4 * decided)) / decided)
    return round((center - margin) / denom, 4)


def tradable_systems(systems: Sequence[dict[str, Any] | str]) -> list[dict[str, Any]]:
    """Systems that clear the sample, units, and Wilson bar, and are metrics."""
    out: list[dict[str, Any]] = []
    for item in systems:
        if isinstance(item, str) and item.strip():
            row: dict[str, Any] = {"key": item.strip()}
        elif isinstance(item, dict) and str(item.get("key") or "").strip():
            key = str(item["key"]).strip()
            row = {"key": key, **{k: v for k, v in item.items() if k != "key"}}
        else:
            continue
        if _system_clears_bar(row):
            out.append(row)
    return out


def _system_clears_bar(row: dict[str, Any]) -> bool:
    key = str(row.get("key") or "")
    if not tradable_family(key):
        return False
    per = row.get("units_per_bet")
    if isinstance(per, bool) or not isinstance(per, (int, float)):
        return False
    decided = _as_int(row.get("decided"))
    wins = _as_int(row.get("wins"))
    if decided is None or wins is None or wins < 0:
        return False
    wilson = row.get("wilson_lower")
    if isinstance(wilson, bool) or not isinstance(wilson, (int, float)):
        wilson = wilson_lower(wins, decided)
    if wilson is None:
        return False
    if decided >= MIN_TRADE_DECIDED and per >= MIN_UNITS_PER_BET and wilson >= MIN_WILSON_LOWER:
        return True
    return (
        slice_family(key) == "exchange_edge"
        and decided >= EDGE_MIN_TRADE_DECIDED
        and per >= EDGE_MIN_UNITS_PER_BET
        and wilson >= EDGE_MIN_WILSON_LOWER
    )


def system_key_set(systems: Sequence[dict[str, Any] | str]) -> set[str]:
    return {str(item["key"]) for item in tradable_systems(systems)}


def _avoid_clears_bar(row: dict[str, Any]) -> bool:
    key = str(row.get("key") or "")
    if not tradable_family(key):
        return False
    per = row.get("units_per_bet")
    if isinstance(per, bool) or not isinstance(per, (int, float)):
        return False
    decided = _as_int(row.get("decided"))
    if decided is None or decided < MIN_TRADE_DECIDED:
        return False
    return per <= AVOID_MAX_UNITS_PER_BET


def active_avoid(systems: Sequence[dict[str, Any] | str]) -> list[dict[str, Any]]:
    """Avoid keys that clear the loss bar and are metrics."""
    out: list[dict[str, Any]] = []
    for item in systems:
        if isinstance(item, str) and item.strip():
            row: dict[str, Any] = {"key": item.strip()}
        elif isinstance(item, dict) and str(item.get("key") or "").strip():
            key = str(item["key"]).strip()
            row = {"key": key, **{k: v for k, v in item.items() if k != "key"}}
        else:
            continue
        if _avoid_clears_bar(row):
            out.append(row)
    return out


def avoid_key_set(systems: Sequence[dict[str, Any] | str]) -> set[str]:
    return {str(item["key"]) for item in active_avoid(systems)}


# Log labels for the league exceptions. They are not trade_systems keys.
NCAAB_OVERRIDE = "league_override:NCAAB"
NCAAF_SPREAD_OVERRIDE = "league_override:NCAAF:spread"
NCAAF_TOTAL_OVERRIDE = "league_override:NCAAF:total"
NBA_SPREAD_LT_EDGE_OVERRIDE = "league_override:NBA:spread_lt_1_5"
_NCAAF_TOTAL_GAPS = frozenset({"25_to_40", "ge_40"})


def _canonical_league(name: str | None) -> str:
    key = str(name or "").strip().upper()
    if key == "CFB":
        return "NCAAF"
    if key == "CBB":
        return "NCAAB"
    return key


def _keyed_band(keys: Sequence[str], family: str) -> str | None:
    prefix = f"{family}:"
    for key in keys:
        if key.startswith(prefix):
            return key[len(prefix) :]
    return None


def _league_override(
    snap: dict[str, Any],
    league: str | None,
    keys: Sequence[str],
    matched: list[str],
) -> tuple[str, list[str]] | None:
    """Explicit league exceptions. None means use the published systems.

    NFL and NHL fall through to system_key_set. Do not add a branch or a
    league key for them, and do not publish these labels as systems.
    """
    name = _canonical_league(str(snap.get("league") or league or ""))
    market = str(snap.get("market") or "").strip().lower()
    if name == "NCAAB":
        return "traded", [NCAAB_OVERRIDE]
    if name == "NCAAF":
        if market == "spread":
            return "traded", [NCAAF_SPREAD_OVERRIDE]
        edge = _keyed_band(keys, "exchange_edge")
        gap = _keyed_band(keys, "composite_gap")
        if market == "total" and edge in {None, "lt_1_5"} and gap in _NCAAF_TOTAL_GAPS:
            return "traded", [NCAAF_TOTAL_OVERRIDE]
        return "dropped", []
    if name == "NBA" and market == "spread" and _keyed_band(keys, "exchange_edge") == "lt_1_5":
        systems = list(matched)
        if NBA_SPREAD_LT_EDGE_OVERRIDE not in systems:
            systems.append(NBA_SPREAD_LT_EDGE_OVERRIDE)
        return "traded", systems
    return None


def classify_play(
    play: dict[str, Any],
    systems: Sequence[dict[str, Any] | str],
    *,
    league: str | None = None,
    avoid: Sequence[dict[str, Any] | str] | None = None,
) -> dict[str, Any]:
    """traded when a system or a league override matches. Otherwise dropped.

    A positive match is dropped when an avoid key matches too. League
    overrides return before that check.
    """
    allowed = system_key_set(systems)
    blocked = avoid_key_set(avoid or [])
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
    override = _league_override(snap, league, keys, matched)
    if override is not None:
        decision, override_keys = override
        if decision == "traded":
            return {
                "decision": "traded",
                "matched_systems": override_keys,
                "drop_reasons": [],
                "trade_data": _json_safe(snap),
            }
        return {
            "decision": "dropped",
            "matched_systems": [],
            "drop_reasons": ["review_gate"],
            "trade_data": _json_safe(snap),
        }
    if not allowed:
        return {
            "decision": "dropped",
            "matched_systems": [],
            "drop_reasons": ["no_trade_systems"],
            "trade_data": _json_safe(snap),
        }
    if matched:
        avoided = [key for key in keys if key in blocked]
        if avoided:
            return {
                "decision": "dropped",
                "matched_systems": matched,
                "drop_reasons": ["avoid_system"],
                "trade_data": _json_safe(snap),
            }
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
    avoid: Sequence[dict[str, Any] | str] | None = None,
) -> list[SharpPlay]:
    """Keep plays whose order matches a profitable system and no avoid key."""
    kept: list[SharpPlay] = []
    for play in plays:
        verdict = classify_play(
            qualifying_trade_data(play), systems, league=play.league, avoid=avoid
        )
        if verdict["decision"] == "traded":
            kept.append(play)
    return kept


def split_slate(
    output: dict[str, Any],
    systems: Sequence[dict[str, Any] | str],
    *,
    logged_at: int,
    avoid: Sequence[dict[str, Any] | str] | None = None,
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    """Tradable slate for alerts, plus one Convex row for every finder play."""
    league = str(output.get("league") or "")
    slate_date = output.get("date")
    tradable: list[dict[str, Any]] = []
    rows: list[dict[str, Any]] = []
    for play in output.get("plays") or []:
        if not isinstance(play, dict):
            continue
        verdict = classify_play(play, systems, league=league, avoid=avoid)
        rows.append(_candidate_row(play, verdict, league=league, slate_date=slate_date, logged_at=logged_at))
        if verdict["decision"] == "traded":
            tradable.append(play)
    posted = dict(output)
    posted["plays"] = tradable
    posted["play_count"] = len(tradable)
    return posted, rows


def candidate_for_skipped_order(
    play: dict[str, Any],
    systems: Sequence[dict[str, Any] | str],
    *,
    league: str,
    slate_date: Any,
    detail: str,
    logged_at: int,
    avoid: Sequence[dict[str, Any] | str] | None = None,
) -> dict[str, Any]:
    """Candidate row for a gate-approved play the order path did not send.

    The decision is dropped and the skip detail is kept, so the review can
    grade the pass. Matched systems stay on the row.
    """
    verdict = classify_play(play, systems, league=league, avoid=avoid)
    reason = detail.strip() or "skipped"
    reasons = [item for item in verdict["drop_reasons"] if item != reason]
    reasons.append(reason)
    return _candidate_row(
        play,
        {
            "decision": "dropped",
            "matched_systems": verdict["matched_systems"],
            "drop_reasons": reasons,
            "trade_data": verdict["trade_data"],
        },
        league=league,
        slate_date=slate_date,
        logged_at=logged_at,
    )


def load_trade_systems() -> list[dict[str, Any]]:
    """Published review systems that clear the bar, or the checked-in fallback.

    An empty published list is a real answer: nothing currently clears the bar.
    The fallback is only for a missed fetch.
    """
    trade, _avoid = load_review_lists()
    return trade


def load_avoid_systems() -> list[dict[str, Any]]:
    """Published avoid keys, or the checked-in fallback.

    A published report with no avoid list means nothing is currently cold
    enough. The fallback file is only for a missed fetch.
    """
    _trade, avoid = load_review_lists()
    return avoid


def load_review_lists() -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Positive systems and avoid keys from one published report."""
    remote = _fetch_remote_payload()
    if remote is not None:
        trade = _clean_systems(remote.get("trade_systems"))
        avoid = _clean_avoid(remote.get("avoid_systems"))
        return trade or [], avoid or []
    return _fallback_systems(), _fallback_avoid()


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
    exchange_rlm = None
    low_liq = None
    if isinstance(confirmation, dict):
        edge = _as_float(confirmation.get("exchange_edge_pct"))
        exchange_rlm = confirmation.get("exchange_rlm_confirmed")
        if not isinstance(exchange_rlm, bool):
            exchange_rlm = None
        low_liq = confirmation.get("low_liquidity")
        if not isinstance(low_liq, bool):
            low_liq = None
    if edge is None:
        edge = _as_float(play.get("exchange_edge_pct"))
    if low_liq is None and isinstance(play.get("polymarket_low_liquidity"), bool):
        low_liq = play.get("polymarket_low_liquidity")
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
    put("sao_gap", gap_band(_as_float(play.get("sao_gap"))))
    rlm_source = str(play.get("rlm_source_used") or "").strip().lower()
    put("rlm_source_used", rlm_source or None)
    put("exchange_rlm_confirmed", exchange_rlm)
    put("low_liquidity", low_liq)
    sbd_override = play.get("sbd_override")
    put("sbd_override", sbd_override if isinstance(sbd_override, bool) else None)
    divergence = play.get("ml_spread_divergence")
    put("ml_spread_divergence", divergence if isinstance(divergence, bool) else None)
    put("model_confidence", _confidence_band(_confidence_number(play.get("model_confidence"))))
    for key, flag in _source_flags(play.get("agreeing_sources")).items():
        put(key, flag)
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


def _fetch_remote_payload() -> dict[str, Any] | None:
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
    return payload if isinstance(payload, dict) else None


def _read_fallback() -> dict[str, Any] | None:
    if not FALLBACK_PATH.is_file():
        return None
    try:
        raw = json.loads(FALLBACK_PATH.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    return raw if isinstance(raw, dict) else None


def _fallback_systems() -> list[dict[str, Any]]:
    raw = _read_fallback()
    if raw is None:
        return []
    systems = raw.get("systems") if isinstance(raw, dict) else raw
    return _clean_systems(systems) or []


def _fallback_avoid() -> list[dict[str, Any]]:
    raw = _read_fallback()
    if raw is None:
        return []
    return _clean_avoid(raw.get("avoid")) or []


def _clean_systems(systems: Any) -> list[dict[str, Any]] | None:
    if not isinstance(systems, list):
        return None
    return tradable_systems(systems)


def _clean_avoid(systems: Any) -> list[dict[str, Any]] | None:
    if not isinstance(systems, list):
        return None
    return active_avoid(systems)


def _source_flags(value: Any) -> dict[str, bool]:
    """One boolean per feed. Primary is the DraftKings or PlayerProps vote."""
    if not isinstance(value, list):
        return {}
    names = {str(item).strip().lower() for item in value if str(item).strip()}
    return {
        key: any(name in names for name in aliases)
        for key, aliases in _SOURCE_FLAGS
    }


def _confidence_number(value: Any) -> float | None:
    if isinstance(value, dict):
        for key in ("score", "model_confidence", "confidence", "value"):
            number = _as_float(value.get(key))
            if number is not None:
                return number
        return None
    return _as_float(value)


def _confidence_band(value: float | None) -> str | None:
    """Bands for the finder score, which sits between 70 and 100."""
    if value is None:
        return None
    if value < 78:
        return "lt_78"
    if value < 85:
        return "78_to_85"
    if value < 92:
        return "85_to_92"
    return "ge_92"


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
