"""Match sharp-money plays to Kalshi moneyline / spread / total markets."""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import UTC, date, datetime
from typing import Any

from polymaker.catalog.sports import LINE_MATCH_TOLERANCE
from polymaker.trading.event_key import normalize_league
from polymaker.trading.match import (
    _cfb_names_match,
    _dummy_team,
    _fighter_names_match,
    _play_line_number,
    _play_target_line,
    candidate_event_dates,
)
from polymaker.trading.sharp import SharpPlay
from polymaker.trading.teams import TeamRef, parse_matchup, resolve_team
from ev_trading.fair_value.tradable_pricer import kalshi_no_ask, kalshi_yes_ask
from ev_trading.nfl_odds import (
    canonical_abbr,
    kalshi_spread_home_line,
    kalshi_ticker_team,
    kalshi_total_line,
    match_team_side,
    parse_kalshi_sub_title,
)
from ev_trading.venues.kalshi.api import KalshiClient

# First matching ticker that exists on Kalshi wins (404s are skipped).
GAME_LINE_SERIES: dict[str, dict[str, tuple[str, ...]]] = {
    "nfl": {
        "moneyline": ("KXNFLGAME",),
        "spread": ("KXNFLSPREAD",),
        "total": ("KXNFLTOTAL",),
    },
    "mlb": {
        "moneyline": ("KXMLBGAME",),
        "spread": ("KXMLBSPREAD",),
        "total": ("KXMLBTOTAL",),
    },
    "ncaaf": {
        "moneyline": ("KXCFBGAME", "KXNCAAFGAME"),
        "spread": ("KXCFBSPREAD", "KXNCAAFSPREAD"),
        "total": ("KXCFBTOTAL", "KXNCAAFTOTAL"),
    },
    "wnba": {
        "moneyline": ("KXWNBAGAME",),
        "spread": ("KXWNBASPREAD",),
        "total": ("KXWNBATOTAL",),
    },
    "ufc": {
        "moneyline": ("KXUFCGAME", "KXUFCFIGHT", "KXMMAFIGHT", "KXUFC"),
    },
}

_MONTHS = {
    "JAN": 1,
    "FEB": 2,
    "MAR": 3,
    "APR": 4,
    "MAY": 5,
    "JUN": 6,
    "JUL": 7,
    "AUG": 8,
    "SEP": 9,
    "OCT": 10,
    "NOV": 11,
    "DEC": 12,
}
_TICKER_DATE_RE = re.compile(
    r"(?P<yy>\d{2})(?P<mon>JAN|FEB|MAR|APR|MAY|JUN|JUL|AUG|SEP|OCT|NOV|DEC)(?P<dd>\d{2})",
    re.I,
)
_TITLE_VS_RE = re.compile(r"(.+?)\s+vs\.?\s+(.+?)(?:\s*\(|$)", re.I)
_TOTAL_SIDES = frozenset({"over", "under"})


@dataclass(frozen=True, slots=True)
class KalshiMatchedPlay:
    play: SharpPlay
    away: TeamRef
    home: TeamRef
    side_team: TeamRef
    event_date: date | None
    ticker: str | None
    kalshi_side: str | None  # yes | no
    line: float | None
    ask: float | None
    status: str  # matched | no_market | unsupported_market | unresolved_team
    detail: str = ""
    event_ticker: str | None = None
    close_time: str | None = None


def match_kalshi_plays(
    plays: list[SharpPlay],
    *,
    client: KalshiClient | None = None,
    markets: frozenset[str] = frozenset({"moneyline", "spread", "total"}),
    series_cache: dict[str, list[dict[str, Any]]] | None = None,
    resolved_series: dict[str, str | None] | None = None,
) -> list[KalshiMatchedPlay]:
    """Resolve each play to a Kalshi ticker + YES/NO side.

    `series_cache` maps series ticker → open events (with nested markets).
    Tests can pass a populated cache and omit `client`.
    """
    cache = series_cache if series_cache is not None else {}
    resolved = resolved_series if resolved_series is not None else {}
    owns_client = False
    trader = client
    if trader is None and series_cache is None:
        try:
            trader = KalshiClient.from_env(require_auth=False)
            owns_client = True
        except Exception:  # noqa: BLE001
            trader = None
    try:
        return [
            _match_one(
                play,
                client=trader,
                markets=markets,
                series_cache=cache,
                resolved_series=resolved,
            )
            for play in plays
        ]
    finally:
        if owns_client and trader is not None:
            trader.close()


def _match_one(
    play: SharpPlay,
    *,
    client: KalshiClient | None,
    markets: frozenset[str],
    series_cache: dict[str, list[dict[str, Any]]],
    resolved_series: dict[str, str | None],
) -> KalshiMatchedPlay:
    dummy = _dummy_team()
    if play.market not in markets:
        return KalshiMatchedPlay(
            play=play,
            away=dummy,
            home=dummy,
            side_team=dummy,
            event_date=None,
            ticker=None,
            kalshi_side=None,
            line=None,
            ask=None,
            status="unsupported_market",
            detail=f"market {play.market!r} not enabled",
        )

    parsed = parse_matchup(play.matchup)
    if parsed is None:
        return KalshiMatchedPlay(
            play=play,
            away=dummy,
            home=dummy,
            side_team=dummy,
            event_date=None,
            ticker=None,
            kalshi_side=None,
            line=None,
            ask=None,
            status="unresolved_team",
            detail=f"cannot parse matchup {play.matchup!r}",
        )
    away_raw, home_raw = parsed
    away = resolve_team(play.league, away_raw)
    home = resolve_team(play.league, home_raw)
    is_total = play.market == "total"
    if is_total:
        side = _dummy_team(play.side)
        if away is None or home is None:
            return _unresolved(play, away, home, side, away_raw, home_raw)
    else:
        side = resolve_team(play.league, play.side)
        if away is None or home is None or side is None:
            return _unresolved(play, away, home, side, away_raw, home_raw)

    assert away is not None and home is not None and side is not None
    event_dates = candidate_event_dates(play.game_time_utc)
    raw_date = play.raw.get("date") if isinstance(play.raw, dict) else None
    if isinstance(raw_date, str) and raw_date:
        try:
            extra = date.fromisoformat(raw_date[:10])
            if extra not in event_dates:
                event_dates.insert(0, extra)
        except ValueError:
            pass

    events = _events_for(
        play.league,
        play.market,
        client=client,
        series_cache=series_cache,
        resolved_series=resolved_series,
    )
    if not events:
        return KalshiMatchedPlay(
            play=play,
            away=away,
            home=home,
            side_team=side,
            event_date=event_dates[0] if event_dates else None,
            ticker=None,
            kalshi_side=None,
            line=None,
            ask=None,
            status="no_market",
            detail=f"no Kalshi {play.market} series for {play.league}",
        )

    event = _find_event(play, away=away, home=home, event_dates=event_dates, events=events)
    if event is None:
        return KalshiMatchedPlay(
            play=play,
            away=away,
            home=home,
            side_team=side,
            event_date=event_dates[0] if event_dates else None,
            ticker=None,
            kalshi_side=None,
            line=None,
            ask=None,
            status="no_market",
            detail=f"no Kalshi event for {play.matchup}",
        )

    picked, pick_detail = _pick_market(
        play,
        event,
        away=away,
        home=home,
        side=side,
    )
    if picked is None:
        return KalshiMatchedPlay(
            play=play,
            away=away,
            home=home,
            side_team=side,
            event_date=_event_date(event) or (event_dates[0] if event_dates else None),
            ticker=None,
            kalshi_side=None,
            line=None,
            ask=None,
            status="no_market",
            detail=pick_detail or f"no open Kalshi {play.market}",
            event_ticker=str(event.get("event_ticker") or "") or None,
        )

    ticker, kalshi_side, line, ask = picked
    return KalshiMatchedPlay(
        play=play,
        away=away,
        home=home,
        side_team=side,
        event_date=_event_date(event) or (event_dates[0] if event_dates else None),
        ticker=ticker,
        kalshi_side=kalshi_side,
        line=line,
        ask=ask,
        status="matched",
        detail="",
        event_ticker=str(event.get("event_ticker") or "") or None,
        close_time=str(event.get("close_time") or "") or None,
    )


def _unresolved(
    play: SharpPlay,
    away: TeamRef | None,
    home: TeamRef | None,
    side: TeamRef | None,
    away_raw: str,
    home_raw: str,
) -> KalshiMatchedPlay:
    return KalshiMatchedPlay(
        play=play,
        away=away or _dummy_team(away_raw),
        home=home or _dummy_team(home_raw),
        side_team=side or _dummy_team(play.side),
        event_date=None,
        ticker=None,
        kalshi_side=None,
        line=None,
        ask=None,
        status="unresolved_team",
        detail="could not map team to Kalshi identity",
    )


def _events_for(
    league: str,
    market: str,
    *,
    client: KalshiClient | None,
    series_cache: dict[str, list[dict[str, Any]]],
    resolved_series: dict[str, str | None],
) -> list[dict[str, Any]]:
    league_l = normalize_league(league)
    candidates = GAME_LINE_SERIES.get(league_l, {}).get(market, ())
    if not candidates:
        return []
    cache_key = f"{league_l}:{market}"
    if cache_key in resolved_series:
        ticker = resolved_series[cache_key]
        if not ticker:
            return []
        return series_cache.get(ticker, [])

    for ticker in candidates:
        if ticker in series_cache:
            resolved_series[cache_key] = ticker
            return series_cache[ticker]
        if client is None:
            continue
        series = client.get_series(ticker)
        if series is None:
            series_cache[ticker] = []
            continue
        events = client.list_open_events(ticker)
        series_cache[ticker] = events
        resolved_series[cache_key] = ticker
        return events
    resolved_series[cache_key] = None
    return []


def _find_event(
    play: SharpPlay,
    *,
    away: TeamRef,
    home: TeamRef,
    event_dates: list[date],
    events: list[dict[str, Any]],
) -> dict[str, Any] | None:
    date_set = set(event_dates)
    fallback: dict[str, Any] | None = None
    league = normalize_league(play.league)
    for event in events:
        if not _event_matches_teams(event, league, away, home):
            continue
        ev_date = _event_date(event)
        if date_set and ev_date is not None and ev_date in date_set:
            return event
        if fallback is None:
            fallback = event
    if fallback is not None and not date_set:
        return fallback
    # Date on ticker can miss; still accept unique team match.
    if fallback is not None and date_set:
        return fallback
    return None


def _event_matches_teams(
    event: dict[str, Any],
    league: str,
    away: TeamRef,
    home: TeamRef,
) -> bool:
    names = _event_participant_names(event)
    if league == "ufc":
        if len(names) < 2:
            return False
        return any(_fighter_names_match(away.full_name, n) for n in names) and any(
            _fighter_names_match(home.full_name, n) for n in names
        )
    if league == "ncaaf":
        if len(names) >= 2:
            return any(_cfb_names_match(away.full_name, n) for n in names) and any(
                _cfb_names_match(home.full_name, n) for n in names
            )
        return False

    parsed = parse_kalshi_sub_title(str(event.get("sub_title") or ""))
    if parsed is not None:
        ev_away, ev_home = parsed
        return _abbr_match(ev_away, away) and _abbr_match(ev_home, home)

    if len(names) >= 2:
        return (
            _label_is_team(names[0], away) or _label_is_team(names[1], away)
        ) and (
            _label_is_team(names[0], home) or _label_is_team(names[1], home)
        )
    return False


def _abbr_match(raw: str, team: TeamRef) -> bool:
    can = canonical_abbr(raw)
    return can.lower() in {
        canonical_abbr(team.betting_abbr).lower(),
        team.poly_code.lower(),
        team.betting_abbr.lower(),
    }


def _label_is_team(label: str, team: TeamRef) -> bool:
    key = (label or "").strip().lower()
    if not key:
        return False
    if key in {team.full_name.lower(), team.poly_code.lower(), team.betting_abbr.lower()}:
        return True
    nick = team.full_name.lower().rsplit(" ", 1)[-1]
    if key == nick or nick in key.split():
        return True
    return _abbr_match(label, team)


def _event_participant_names(event: dict[str, Any]) -> list[str]:
    parsed = parse_kalshi_sub_title(str(event.get("sub_title") or ""))
    if parsed is not None:
        return [parsed[0], parsed[1]]
    title = str(event.get("title") or "")
    for suffix in (": Spread", ": Total Points", ": Moneyline", ": Winner"):
        if title.endswith(suffix):
            title = title[: -len(suffix)]
    match = _TITLE_VS_RE.search(title)
    if match:
        return [match.group(1).strip(), match.group(2).strip()]
    sub = str(event.get("sub_title") or "")
    match = _TITLE_VS_RE.search(sub)
    if match:
        return [match.group(1).strip(), match.group(2).strip()]
    return []


def _event_date(event: dict[str, Any]) -> date | None:
    ticker = str(event.get("event_ticker") or "")
    match = _TICKER_DATE_RE.search(ticker)
    if match:
        try:
            return date(
                2000 + int(match.group("yy")),
                _MONTHS[match.group("mon").upper()],
                int(match.group("dd")),
            )
        except ValueError:
            pass
    close = event.get("close_time") or event.get("expiration_time")
    if isinstance(close, str) and close:
        try:
            return datetime.fromisoformat(close.replace("Z", "+00:00")).astimezone(UTC).date()
        except ValueError:
            pass
    return None


def _pick_market(
    play: SharpPlay,
    event: dict[str, Any],
    *,
    away: TeamRef,
    home: TeamRef,
    side: TeamRef,
) -> tuple[tuple[str, str, float | None, float | None] | None, str]:
    markets = [m for m in (event.get("markets") or []) if isinstance(m, dict)]
    if not markets:
        return None, f"no nested Kalshi markets on {event.get('event_ticker')}"

    if play.market == "moneyline":
        return _pick_moneyline(play, markets, away=away, home=home, side=side)
    if play.market == "spread":
        return _pick_spread(play, markets, away=away, home=home, side=side)
    if play.market == "total":
        return _pick_total(play, markets)
    return None, f"unsupported market {play.market}"


def _pick_moneyline(
    play: SharpPlay,
    markets: list[dict[str, Any]],
    *,
    away: TeamRef,
    home: TeamRef,
    side: TeamRef,
) -> tuple[tuple[str, str, float | None, float | None] | None, str]:
    want_home = _same_team(side, home)
    ranked: list[tuple[int, str, str, float]] = []
    for market in markets:
        ticker = str(market.get("ticker") or "")
        if not ticker:
            continue
        team_abbr = kalshi_ticker_team(ticker)
        team_label = str(market.get("yes_sub_title") or market.get("title") or "")
        mapped = match_team_side(
            team_label,
            away_abbr=away.betting_abbr,
            home_abbr=home.betting_abbr,
            away_name=away.full_name,
            home_name=home.full_name,
            team_abbr=team_abbr,
        )
        if mapped is None and team_abbr:
            if _abbr_match(team_abbr, side):
                mapped = "home" if want_home else "away"
            elif _abbr_match(team_abbr, away):
                mapped = "away"
            elif _abbr_match(team_abbr, home):
                mapped = "home"
        if mapped is None:
            continue
        ours = (mapped == "home" and want_home) or (mapped == "away" and not want_home)
        kalshi_side = "yes" if ours else "no"
        ask = kalshi_yes_ask(market) if kalshi_side == "yes" else kalshi_no_ask(market)
        if ask is None:
            continue
        ranked.append((0 if ours else 1, ticker, kalshi_side, ask))
    if not ranked:
        return None, "no Kalshi moneyline ticker for side"
    ranked.sort(key=lambda row: row[0])
    _, ticker, kalshi_side, ask = ranked[0]
    return (ticker, kalshi_side, None, ask), ""


def _pick_spread(
    play: SharpPlay,
    markets: list[dict[str, Any]],
    *,
    away: TeamRef,
    home: TeamRef,
    side: TeamRef,
) -> tuple[tuple[str, str, float | None, float | None] | None, str]:
    target = _play_target_line(play, away=away, home=home, side=side)
    play_line = _play_line_number(play)
    usable: list[tuple[float, dict[str, Any], float]] = []
    for market in markets:
        home_line = kalshi_spread_home_line(
            market,
            away_abbr=away.betting_abbr,
            home_abbr=home.betting_abbr,
            away_name=away.full_name,
            home_name=home.full_name,
        )
        if home_line is None:
            continue
        delta = abs(home_line - target) if target is not None else 0.0
        usable.append((delta, market, home_line))
    if not usable:
        return None, "no open spread"
    usable.sort(key=lambda row: row[0])
    delta, market, home_line = usable[0]
    if target is not None and delta > LINE_MATCH_TOLERANCE:
        shown_play = play_line if play_line is not None else target
        return None, f"spread line mismatch play={shown_play:g} kalshi={home_line:g}"

    ticker = str(market.get("ticker") or "")
    team_abbr = kalshi_ticker_team(ticker)
    team_label = str(market.get("team") or market.get("yes_sub_title") or "")
    mapped = match_team_side(
        team_label,
        away_abbr=away.betting_abbr,
        home_abbr=home.betting_abbr,
        away_name=away.full_name,
        home_name=home.full_name,
        team_abbr=team_abbr,
    )
    want_home = _same_team(side, home)
    # YES = listed team covers (wins by over X). We want our side to cover.
    ours = (mapped == "home" and want_home) or (mapped == "away" and not want_home)
    if mapped is None:
        return None, "could not map Kalshi spread team"
    kalshi_side = "yes" if ours else "no"
    ask = kalshi_yes_ask(market) if kalshi_side == "yes" else kalshi_no_ask(market)
    if ask is None:
        return None, "Kalshi spread ask missing"
    return (ticker, kalshi_side, home_line, ask), ""


def _pick_total(
    play: SharpPlay,
    markets: list[dict[str, Any]],
) -> tuple[tuple[str, str, float | None, float | None] | None, str]:
    target = _play_line_number(play)
    usable: list[tuple[float, dict[str, Any], float]] = []
    for market in markets:
        line = kalshi_total_line(market)
        if line is None:
            continue
        delta = abs(line - target) if target is not None else 0.0
        usable.append((delta, market, line))
    if not usable:
        return None, "no open total"
    usable.sort(key=lambda row: row[0])
    delta, market, line = usable[0]
    if target is not None and delta > LINE_MATCH_TOLERANCE:
        return None, f"total line mismatch play={target:g} kalshi={line:g}"
    want = play.side.strip().lower()
    if want not in _TOTAL_SIDES:
        return None, f"total side {play.side!r} is not Over/Under"
    kalshi_side = "yes" if want == "over" else "no"
    ask = kalshi_yes_ask(market) if kalshi_side == "yes" else kalshi_no_ask(market)
    ticker = str(market.get("ticker") or "")
    if not ticker or ask is None:
        return None, "Kalshi total ask missing"
    return (ticker, kalshi_side, line, ask), ""


def _same_team(a: TeamRef, b: TeamRef) -> bool:
    if a.poly_code and b.poly_code and a.poly_code.lower() == b.poly_code.lower():
        return True
    return canonical_abbr(a.betting_abbr).lower() == canonical_abbr(b.betting_abbr).lower()
