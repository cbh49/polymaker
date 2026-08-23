#!/usr/bin/env python3
"""
Pull Polymarket moneyline / spread / total prices and attach them to splits games.

Share price is stored as implied_prob_pct (percent). American odds are converted
from that share price. History is the CLOB 24h price series (collapsed to
line-change timestamps), same role as eva_history.

If a market is missing or liquidity is 0/null, the polymarket object is omitted.

Usage:
  python scrape_polymarket_odds.py
  python scrape_polymarket_odds.py --league WNBA --out output/polymarket_wnba_odds.json
"""

from __future__ import annotations

import argparse
import importlib.util
import json
import re
import sys
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import requests

SCRIPT_DIR = Path(__file__).resolve().parent
_TEAMS_PATH = SCRIPT_DIR.parent / "src" / "polymaker" / "trading" / "teams.py"


def _load_resolve_team():
    spec = importlib.util.spec_from_file_location("polymaker_trading_teams", _TEAMS_PATH)
    if spec is None or spec.loader is None:
        raise ImportError(f"Cannot load team map from {_TEAMS_PATH}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module.resolve_team


resolve_team = _load_resolve_team()

GAMMA_HOST = "https://gamma-api.polymarket.com"
CLOB_HOST = "https://clob.polymarket.com"
PAGE_TZ = ZoneInfo("America/New_York")
HISTORY_INTERVAL = "1d"
HISTORY_FIDELITY_MIN = 5
DEFAULT_OUT = {
    "MLB": SCRIPT_DIR / "output" / "polymarket_mlb_odds.json",
    "WNBA": SCRIPT_DIR / "output" / "polymarket_wnba_odds.json",
    "UFC": SCRIPT_DIR / "output" / "polymarket_ufc_odds.json",
}

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/128.0.0.0 Safari/537.36"
    ),
    "Accept": "application/json",
}

_EVENT_SLUG_RE = re.compile(
    r"^(?P<league>mlb|wnba|ufc)-(?P<away>[a-z0-9]+)-(?P<home>[a-z0-9]+)-(?P<ymd>\d{4}-\d{2}-\d{2})$"
)
_SPREAD_SLUG_RE = re.compile(
    r"^(?P<league>mlb|wnba|ufc)-(?P<away>[a-z0-9]+)-(?P<home>[a-z0-9]+)-(?P<ymd>\d{4}-\d{2}-\d{2})"
    r"-spread-(?P<favored>home|away)-(?P<pts>\d+(?:pt\d+)?)$"
)
_TOTAL_SLUG_RE = re.compile(
    r"^(?P<league>mlb|wnba|ufc)-(?P<away>[a-z0-9]+)-(?P<home>[a-z0-9]+)-(?P<ymd>\d{4}-\d{2}-\d{2})"
    r"-(?:total|totals)-(?P<pts>\d+(?:pt\d+)?)$"
)


def _json_list(value: Any) -> list[Any]:
    if value is None:
        return []
    if isinstance(value, list):
        return value
    try:
        parsed = json.loads(value)
        return parsed if isinstance(parsed, list) else []
    except (json.JSONDecodeError, TypeError):
        return []


def parse_pt_number(raw: str) -> float | None:
    text = (raw or "").strip().lower()
    if not text:
        return None
    if "pt" in text:
        left, _, right = text.partition("pt")
        try:
            return float(f"{left}.{right}")
        except ValueError:
            return None
    try:
        return float(text)
    except ValueError:
        return None


def share_to_american(share: float) -> int | None:
    """Convert a 0–1 share price (implied probability) to American odds."""
    if share <= 0 or share >= 1:
        return None
    if share >= 0.5:
        return int(round(-100.0 * share / (1.0 - share)))
    return int(round(100.0 * (1.0 - share) / share))


def _parse_share(value: Any) -> float | None:
    try:
        share = float(value)
    except (TypeError, ValueError):
        return None
    if share <= 0 or share >= 1:
        return None
    return share


def _liquidity(raw: dict[str, Any]) -> float | None:
    for key in ("liquidityNum", "liquidity"):
        val = raw.get(key)
        if val is None or val == "":
            continue
        try:
            num = float(val)
        except (TypeError, ValueError):
            continue
        if num > 0:
            return round(num, 2)
    return None


def _volume_24hr(raw: dict[str, Any]) -> float:
    for key in ("volume24hrClob", "volume24hr"):
        val = raw.get(key)
        if val is None or val == "":
            continue
        try:
            return round(float(val), 2)
        except (TypeError, ValueError):
            continue
    return 0.0


def implied_prob_pct(share: float) -> float:
    return round(share * 100.0, 4)


def candidate_event_dates(
    game: dict[str, Any],
    slate_day: date | None = None,
) -> list[date]:
    dates: list[date] = []
    if slate_day is not None:
        dates.append(slate_day)
    raw_date = game.get("date") or game.get("eva_date") or game.get("covers_date")
    if isinstance(raw_date, str) and raw_date:
        try:
            dates.append(date.fromisoformat(raw_date[:10]))
        except ValueError:
            pass
    ts = game.get("game_time_utc")
    if isinstance(ts, str) and ts:
        try:
            dt = datetime.fromisoformat(ts.replace("Z", "+00:00")).astimezone(UTC)
            et = dt.astimezone(PAGE_TZ).date()
            dates.extend(
                [
                    et,
                    dt.astimezone(ZoneInfo("America/Los_Angeles")).date(),
                    dt.date(),
                    et - timedelta(days=1),
                    et + timedelta(days=1),
                ]
            )
        except ValueError:
            pass
    seen: set[date] = set()
    out: list[date] = []
    for d in dates:
        if d not in seen:
            seen.add(d)
            out.append(d)
    return out


def _outcome_index(outcomes: list[Any], team_names: list[str], poly_code: str) -> int | None:
    keys = [n.strip().lower() for n in team_names if n]
    nick = keys[0].rsplit(" ", 1)[-1] if keys else ""
    for i, raw in enumerate(outcomes):
        label = str(raw or "").strip().lower()
        if not label:
            continue
        if label in keys or (nick and (label == nick or nick in label.split())):
            return i
        if any(k in label or label in k for k in keys):
            return i
        if poly_code and label == poly_code:
            return i
    return None


class _NameRef:
    """Minimal stand-in for TeamRef so UFC can reuse build_poly_sides."""

    def __init__(self, name: str, code: str = "") -> None:
        self.full_name = name
        self.poly_code = code or name


def _ufc_moneyline_outcomes(event: dict[str, Any]) -> list[str]:
    event_slug = str(event.get("slug") or "")
    for raw in event.get("markets") or []:
        if not isinstance(raw, dict) or raw.get("closed"):
            continue
        if str(raw.get("slug") or "") == event_slug:
            return [str(x) for x in _json_list(raw.get("outcomes")) if str(x).strip()]
    title = str(event.get("title") or "")
    m = re.search(r":\s*(.+?)\s+vs\.?\s+(.+?)\s*\(", title, re.IGNORECASE)
    if m:
        return [m.group(1).strip(), m.group(2).strip()]
    return []


def _match_ufc_event(
    events: list[dict[str, Any]],
    away: str,
    home: str,
    dates: list[date],
) -> dict[str, Any] | None:
    from ufc_fighter_map import names_match

    date_set = {d.isoformat() for d in dates}
    fallback: dict[str, Any] | None = None
    for event in events:
        names = _ufc_moneyline_outcomes(event)
        if len(names) < 2:
            continue
        a_hit = any(names_match(away, n) for n in names)
        h_hit = any(names_match(home, n) for n in names)
        if not (a_hit and h_hit):
            continue
        event_day = str(event.get("eventDate") or "")[:10]
        if event_day and event_day in date_set:
            return event
        if fallback is None:
            fallback = event
    return fallback


def _total_index(outcomes: list[Any], side: str) -> int | None:
    want = "over" if side == "over" else "under"
    for i, raw in enumerate(outcomes):
        if str(raw or "").strip().lower() == want:
            return i
    return 0 if side == "over" and outcomes else (1 if side == "under" and len(outcomes) > 1 else None)


def _snapshot(
    share: float,
    liquidity: float,
    volume_24hr: float,
    ts: str,
    market_id: str,
) -> dict[str, Any] | None:
    line = share_to_american(share)
    if line is None:
        return None
    return {
        "line": line,
        "implied_prob_pct": implied_prob_pct(share),
        "liquidity": liquidity,
        "volume_24hr": volume_24hr,
        "last_updated": ts,
        "market_id": market_id,
    }


def _history_point(snap: dict[str, Any]) -> dict[str, Any]:
    return {
        "ts": snap["last_updated"],
        "line": snap["line"],
        "implied_prob_pct": snap["implied_prob_pct"],
    }


def history_from_clob_points(points: list[Any] | None) -> list[dict[str, Any]]:
    """Convert CLOB {t, p} buckets into EVA-style line-change rows."""
    out: list[dict[str, Any]] = []
    last_line: int | None = None
    last_prob: float | None = None
    if not isinstance(points, list):
        return out
    for pt in points:
        if not isinstance(pt, dict):
            continue
        share = _parse_share(pt.get("p"))
        ts_raw = pt.get("t")
        if share is None or ts_raw is None:
            continue
        line = share_to_american(share)
        if line is None:
            continue
        try:
            unix = int(float(ts_raw))
        except (TypeError, ValueError):
            continue
        ts = datetime.fromtimestamp(unix, tz=UTC).astimezone(PAGE_TZ).isoformat(timespec="seconds")
        prob = implied_prob_pct(share)
        if out and last_line == line and last_prob == prob:
            continue
        out.append({"ts": ts, "line": line, "implied_prob_pct": prob})
        last_line = line
        last_prob = prob
    return out


def fetch_price_history(
    session: requests.Session,
    token_id: str,
    cache: dict[str, list[dict[str, Any]]],
) -> list[dict[str, Any]]:
    if not token_id:
        return []
    if token_id in cache:
        return cache[token_id]
    try:
        resp = session.get(
            f"{CLOB_HOST}/prices-history",
            params={
                "market": token_id,
                "interval": HISTORY_INTERVAL,
                "fidelity": HISTORY_FIDELITY_MIN,
            },
            headers=HEADERS,
            timeout=20,
        )
        resp.raise_for_status()
        payload = resp.json()
        raw = payload.get("history") if isinstance(payload, dict) else payload
        hist = history_from_clob_points(raw if isinstance(raw, list) else None)
    except (requests.RequestException, ValueError, TypeError):
        hist = []
    cache[token_id] = hist
    return hist


def _side_line_number(side: dict[str, Any] | None) -> float | None:
    if not isinstance(side, dict):
        return None
    for key in ("live", "eva_line", "sbd_line", "open"):
        val = side.get(key)
        if isinstance(val, (int, float)):
            return float(val)
    return None


def _market_id(raw: dict[str, Any]) -> str:
    if raw.get("id") is not None:
        return str(raw["id"])
    if raw.get("conditionId"):
        return str(raw["conditionId"])
    return str(raw.get("slug") or "")


def _classify_markets(event: dict[str, Any]) -> dict[str, Any]:
    event_slug = str(event.get("slug") or "")
    moneyline = None
    spreads: list[dict[str, Any]] = []
    totals: list[dict[str, Any]] = []
    for raw in event.get("markets") or []:
        if not isinstance(raw, dict) or raw.get("closed"):
            continue
        slug = str(raw.get("slug") or "")
        if slug == event_slug and _EVENT_SLUG_RE.match(slug):
            moneyline = raw
            continue
        spread_m = _SPREAD_SLUG_RE.match(slug)
        if spread_m:
            pts = parse_pt_number(spread_m.group("pts"))
            if pts is None:
                continue
            spreads.append(
                {
                    "raw": raw,
                    "favored": spread_m.group("favored"),
                    "points": pts,
                    "liquidity": _liquidity(raw) or 0.0,
                }
            )
            continue
        total_m = _TOTAL_SLUG_RE.match(slug)
        if total_m:
            pts = parse_pt_number(total_m.group("pts"))
            if pts is None:
                continue
            totals.append(
                {
                    "raw": raw,
                    "points": pts,
                    "liquidity": _liquidity(raw) or 0.0,
                }
            )
    return {"moneyline": moneyline, "spreads": spreads, "totals": totals}


def pick_spread_market(
    spreads: list[dict[str, Any]],
    dest_spread: dict[str, Any] | None,
) -> dict[str, Any] | None:
    usable = [s for s in spreads if (s.get("liquidity") or 0) > 0]
    if not usable:
        return None
    home_line = _side_line_number((dest_spread or {}).get("home"))
    away_line = _side_line_number((dest_spread or {}).get("away"))
    target = home_line if home_line is not None else (
        -away_line if away_line is not None else None
    )
    if target is not None:
        for row in usable:
            pts = float(row["points"])
            favored = row["favored"]
            implied_home = -pts if favored == "home" else pts
            if abs(implied_home - target) < 0.01:
                return row
    return max(usable, key=lambda r: r["liquidity"])


def pick_total_market(
    totals: list[dict[str, Any]],
    dest_total: dict[str, Any] | None,
) -> dict[str, Any] | None:
    usable = [t for t in totals if (t.get("liquidity") or 0) > 0]
    if not usable:
        return None
    target = _side_line_number((dest_total or {}).get("over"))
    if target is None:
        target = _side_line_number((dest_total or {}).get("under"))
    if target is not None:
        for row in usable:
            if abs(float(row["points"]) - target) < 0.01:
                return row
        return min(usable, key=lambda r: abs(float(r["points"]) - target))
    return max(usable, key=lambda r: r["liquidity"])


def _poly_for_outcome(
    raw: dict[str, Any],
    idx: int | None,
    ts: str,
    *,
    session: requests.Session | None = None,
    history_cache: dict[str, list[dict[str, Any]]] | None = None,
) -> dict[str, Any] | None:
    liq = _liquidity(raw)
    if liq is None:
        return None
    prices = _json_list(raw.get("outcomePrices"))
    if idx is None or idx < 0 or idx >= len(prices):
        return None
    share = _parse_share(prices[idx] if idx < len(prices) else None)
    if share is None:
        return None
    market_id = _market_id(raw)
    if not market_id:
        return None
    snap = _snapshot(share, liq, _volume_24hr(raw), ts, market_id)
    if snap is None:
        return None
    tokens = _json_list(raw.get("clobTokenIds"))
    token_id = str(tokens[idx]) if idx < len(tokens) else ""
    hist: list[dict[str, Any]] = []
    if session is not None:
        hist = fetch_price_history(session, token_id, history_cache if history_cache is not None else {})
    if not hist:
        hist = [_history_point(snap)]
    snap["history"] = hist
    return snap


def build_poly_sides(
    event: dict[str, Any],
    dest_game: dict[str, Any],
    *,
    away_ref: Any,
    home_ref: Any,
    ts: str,
    session: requests.Session | None = None,
    history_cache: dict[str, list[dict[str, Any]]] | None = None,
) -> dict[str, Any]:
    classified = _classify_markets(event)
    away_names = [away_ref.full_name, dest_game.get("away") or "", dest_game.get("away_abbr") or ""]
    home_names = [home_ref.full_name, dest_game.get("home") or "", dest_game.get("home_abbr") or ""]

    hist_kw = {"session": session, "history_cache": history_cache}

    moneyline: dict[str, Any] = {}
    ml = classified["moneyline"]
    if isinstance(ml, dict):
        outcomes = _json_list(ml.get("outcomes"))
        away_idx = _outcome_index(outcomes, away_names, away_ref.poly_code)
        home_idx = _outcome_index(outcomes, home_names, home_ref.poly_code)
        away_snap = _poly_for_outcome(ml, away_idx, ts, **hist_kw)
        home_snap = _poly_for_outcome(ml, home_idx, ts, **hist_kw)
        if away_snap:
            moneyline["away"] = away_snap
        if home_snap:
            moneyline["home"] = home_snap

    spread: dict[str, Any] = {}
    picked_sp = pick_spread_market(classified["spreads"], dest_game.get("spread"))
    if picked_sp:
        raw = picked_sp["raw"]
        outcomes = _json_list(raw.get("outcomes"))
        away_idx = _outcome_index(outcomes, away_names, away_ref.poly_code)
        home_idx = _outcome_index(outcomes, home_names, home_ref.poly_code)
        away_snap = _poly_for_outcome(raw, away_idx, ts, **hist_kw)
        home_snap = _poly_for_outcome(raw, home_idx, ts, **hist_kw)
        if away_snap:
            spread["away"] = away_snap
        if home_snap:
            spread["home"] = home_snap

    total: dict[str, Any] = {}
    picked_tot = pick_total_market(classified["totals"], dest_game.get("total"))
    if picked_tot:
        raw = picked_tot["raw"]
        outcomes = _json_list(raw.get("outcomes"))
        over_snap = _poly_for_outcome(raw, _total_index(outcomes, "over"), ts, **hist_kw)
        under_snap = _poly_for_outcome(raw, _total_index(outcomes, "under"), ts, **hist_kw)
        if over_snap:
            total["over"] = over_snap
        if under_snap:
            total["under"] = under_snap

    return {"moneyline": moneyline, "spread": spread, "total": total}


def fetch_event(session: requests.Session, slug: str) -> dict[str, Any] | None:
    try:
        resp = session.get(
            f"{GAMMA_HOST}/events",
            params={"slug": slug, "limit": 1, "active": "true", "closed": "false"},
            headers=HEADERS,
            timeout=20,
        )
        resp.raise_for_status()
        batch = resp.json()
        if isinstance(batch, list) and batch and isinstance(batch[0], dict):
            return batch[0]
    except (requests.RequestException, ValueError, IndexError):
        return None
    return None


def iter_series_events(
    session: requests.Session,
    league: str,
    *,
    limit: int = 50,
    max_pages: int = 20,
) -> list[dict[str, Any]]:
    events: list[dict[str, Any]] = []
    offset = 0
    for _ in range(max_pages):
        try:
            resp = session.get(
                f"{GAMMA_HOST}/events",
                params={
                    "series_slug": league.lower(),
                    "active": "true",
                    "closed": "false",
                    "limit": limit,
                    "offset": offset,
                },
                headers=HEADERS,
                timeout=30,
            )
            if resp.status_code in (400, 422):
                break
            resp.raise_for_status()
            batch = resp.json()
        except (requests.RequestException, ValueError):
            break
        if not isinstance(batch, list) or not batch:
            break
        events.extend(e for e in batch if isinstance(e, dict))
        if len(batch) < limit:
            break
        offset += limit
    return events


def _match_event_for_game(
    session: requests.Session,
    league: str,
    away_code: str,
    home_code: str,
    dates: list[date],
    cache: dict[str, dict[str, Any] | None],
    series_index: dict[tuple[str, str, str], dict[str, Any]] | None,
) -> dict[str, Any] | None:
    prefix = league.lower()
    for d in dates:
        slug = f"{prefix}-{away_code}-{home_code}-{d.isoformat()}"
        if slug not in cache:
            cache[slug] = fetch_event(session, slug)
        event = cache[slug]
        if event:
            return event
        if series_index is not None:
            found = series_index.get((away_code, home_code, d.isoformat()))
            if found:
                return found
    return None


def _index_series_events(events: list[dict[str, Any]]) -> dict[tuple[str, str, str], dict[str, Any]]:
    indexed: dict[tuple[str, str, str], dict[str, Any]] = {}
    for event in events:
        slug = str(event.get("slug") or "")
        m = _EVENT_SLUG_RE.match(slug)
        if not m:
            continue
        keys = {
            (m.group("away"), m.group("home"), m.group("ymd")),
            (m.group("away"), m.group("home"), str(event.get("eventDate") or "")[:10]),
        }
        for key in keys:
            if key[2]:
                indexed[key] = event
    return indexed


def scrape(
    league: str = "MLB",
    games: list[dict[str, Any]] | None = None,
    day: date | None = None,
) -> dict[str, Any]:
    league = (league or "MLB").upper()
    ts = datetime.now(PAGE_TZ).isoformat(timespec="seconds")
    dest_games = list(games or [])
    built: list[dict[str, Any]] = []

    with requests.Session() as session:
        session.trust_env = False
        cache: dict[str, dict[str, Any] | None] = {}
        history_cache: dict[str, list[dict[str, Any]]] = {}
        series_index: dict[tuple[str, str, str], dict[str, Any]] | None = None

        if not dest_games:
            events = iter_series_events(session, league)
            series_index = _index_series_events(events)
            # Standalone: one row per event in the date window.
            window = day or datetime.now(PAGE_TZ).date()
            for event in events:
                slug = str(event.get("slug") or "")
                m = _EVENT_SLUG_RE.match(slug)
                if not m or m.group("league") != league.lower():
                    continue
                event_day = str(event.get("eventDate") or m.group("ymd"))[:10]
                if event_day and abs(
                    (date.fromisoformat(event_day) - window).days
                ) > 1:
                    continue
                if league == "UFC":
                    names = _ufc_moneyline_outcomes(event)
                    if len(names) < 2:
                        continue
                    dest = {
                        "matchup": f"{names[0]} vs {names[1]}",
                        "away_abbr": names[0],
                        "home_abbr": names[1],
                        "away": names[0],
                        "home": names[1],
                        "date": event_day,
                    }
                    dest.update(
                        build_poly_sides(
                            event,
                            dest,
                            away_ref=_NameRef(names[0]),
                            home_ref=_NameRef(names[1]),
                            ts=ts,
                            session=session,
                            history_cache=history_cache,
                        )
                    )
                    dest["polymarket_event_slug"] = slug
                    built.append(dest)
                    continue
                dest = {
                    "matchup": f"{m.group('away').upper()} @ {m.group('home').upper()}",
                    "away_abbr": m.group("away").upper(),
                    "home_abbr": m.group("home").upper(),
                    "away": None,
                    "home": None,
                    "date": event_day,
                }
                away_ref = resolve_team(league, dest["away_abbr"])
                home_ref = resolve_team(league, dest["home_abbr"])
                if away_ref is None or home_ref is None:
                    continue
                dest["away"] = away_ref.full_name
                dest["home"] = home_ref.full_name
                dest["matchup"] = f"{away_ref.betting_abbr} @ {home_ref.betting_abbr}"
                dest["away_abbr"] = away_ref.betting_abbr
                dest["home_abbr"] = home_ref.betting_abbr
                dest.update(
                    build_poly_sides(
                        event,
                        dest,
                        away_ref=away_ref,
                        home_ref=home_ref,
                        ts=ts,
                        session=session,
                        history_cache=history_cache,
                    )
                )
                dest["polymarket_event_slug"] = slug
                built.append(dest)
        else:
            ufc_events: list[dict[str, Any]] | None = None
            if league == "UFC":
                ufc_events = iter_series_events(session, league)
            for game in dest_games:
                if league == "UFC":
                    away_name = str(game.get("away") or game.get("away_abbr") or "")
                    home_name = str(game.get("home") or game.get("home_abbr") or "")
                    if not away_name or not home_name:
                        continue
                    away_ref = _NameRef(away_name)
                    home_ref = _NameRef(home_name)
                    dates = candidate_event_dates(game, day)
                    event = _match_ufc_event(ufc_events or [], away_name, home_name, dates)
                    if event is None:
                        continue
                    row = {
                        "matchup": game.get("matchup") or f"{away_name} vs {home_name}",
                        "away_abbr": game.get("away_abbr") or away_name,
                        "home_abbr": game.get("home_abbr") or home_name,
                        "away": away_name,
                        "home": home_name,
                        "date": game.get("date"),
                        "polymarket_event_slug": event.get("slug"),
                    }
                    row.update(
                        build_poly_sides(
                            event,
                            game,
                            away_ref=away_ref,
                            home_ref=home_ref,
                            ts=ts,
                            session=session,
                            history_cache=history_cache,
                        )
                    )
                    built.append(row)
                    continue
                away_raw = str(game.get("away_abbr") or game.get("away") or "")
                home_raw = str(game.get("home_abbr") or game.get("home") or "")
                away_ref = resolve_team(league, away_raw)
                home_ref = resolve_team(league, home_raw)
                if away_ref is None or home_ref is None:
                    continue
                dates = candidate_event_dates(game, day)
                event = _match_event_for_game(
                    session,
                    league,
                    away_ref.poly_code,
                    home_ref.poly_code,
                    dates,
                    cache,
                    series_index,
                )
                if event is None:
                    if series_index is None:
                        series_index = _index_series_events(iter_series_events(session, league))
                    event = _match_event_for_game(
                        session,
                        league,
                        away_ref.poly_code,
                        home_ref.poly_code,
                        dates,
                        cache,
                        series_index,
                    )
                if event is None:
                    continue
                row = {
                    "matchup": game.get("matchup") or f"{game.get('away_abbr')} @ {game.get('home_abbr')}",
                    "away_abbr": game.get("away_abbr"),
                    "home_abbr": game.get("home_abbr"),
                    "away": game.get("away"),
                    "home": game.get("home"),
                    "date": game.get("date"),
                    "polymarket_event_slug": event.get("slug"),
                }
                row.update(
                    build_poly_sides(
                        event,
                        game,
                        away_ref=away_ref,
                        home_ref=home_ref,
                        ts=ts,
                        session=session,
                        history_cache=history_cache,
                    )
                )
                built.append(row)

    return {
        "source": "gamma-api.polymarket.com",
        "api": f"{GAMMA_HOST}/events",
        "date": (day or datetime.now(PAGE_TZ).date()).isoformat(),
        "league": league,
        "scraped_at": datetime.now(UTC).isoformat(),
        "game_count": len(built),
        "games": built,
    }


def load_previous_games(path: Path) -> dict[str, dict[str, Any]]:
    if not path.exists():
        return {}
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    games = raw.get("games") if isinstance(raw, dict) else None
    if not isinstance(games, list):
        return {}
    by_matchup: dict[str, dict[str, Any]] = {}
    for game in games:
        if isinstance(game, dict) and game.get("matchup"):
            by_matchup[str(game["matchup"])] = game
    return by_matchup


def merge_polymarket_into_game(
    game: dict[str, Any],
    poly_game: dict[str, Any],
    prev_game: dict[str, Any] | None = None,
) -> None:
    """Copy polymarket snapshots (including 24h CLOB history) onto matching sides."""
    _ = prev_game
    if poly_game.get("polymarket_event_slug"):
        game["polymarket_event_slug"] = poly_game["polymarket_event_slug"]
    for market in ("moneyline", "spread", "total"):
        src_market = poly_game.get(market)
        if not isinstance(src_market, dict):
            continue
        dst_market = game.get(market)
        if not isinstance(dst_market, dict):
            dst_market = {}
            game[market] = dst_market
        for side, snap in src_market.items():
            if not isinstance(snap, dict) or "line" not in snap:
                continue
            dst_side = dst_market.get(side)
            if not isinstance(dst_side, dict):
                dst_side = {"selection": side}
                dst_market[side] = dst_side
            dst_side["polymarket"] = dict(snap)


def main() -> None:
    parser = argparse.ArgumentParser(description="Scrape Polymarket moneyline/spread/total prices")
    parser.add_argument("--league", default="MLB", choices=["MLB", "WNBA", "UFC"])
    parser.add_argument("--date", default=None, help="Slate date YYYY-MM-DD")
    parser.add_argument("--out", type=Path, default=None)
    args = parser.parse_args()
    day = date.fromisoformat(args.date) if args.date else None
    result = scrape(league=args.league, day=day)
    out = args.out or DEFAULT_OUT[args.league]
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(result, indent=2) + "\n", encoding="utf-8")
    print(f"Wrote {result['game_count']} {args.league} Polymarket games → {out}")


if __name__ == "__main__":
    main()
