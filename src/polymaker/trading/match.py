"""Match sharp-money plays to Polymarket moneyline / spread / total markets."""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

from polymaker.catalog.gamma import GammaClient, parse_market
from polymaker.catalog.sports import (
    DEFAULT_PREGAME_BUFFER_MINUTES,
    EVENT_SLUG_RE,
    LINE_MATCH_TOLERANCE,
    SPREAD_SLUG_RE,
    TOTAL_SLUG_RE,
    implied_home_spread,
    is_moneyline_slug,
    is_pre_game,
    parse_pt_number,
    pick_market_for_kind,
    pick_moneyline_market,
)
from polymaker.catalog.store import CatalogStore
from polymaker.domain import MarketMeta, TokenMeta
from polymaker.trading.sharp import SharpPlay
from polymaker.trading.teams import TeamRef, parse_matchup, resolve_team

_ET = ZoneInfo("America/New_York")
_PT = ZoneInfo("America/Los_Angeles")

_TOTAL_SIDES = frozenset({"over", "under"})


@dataclass(frozen=True, slots=True)
class MatchedPlay:
    play: SharpPlay
    away: TeamRef
    home: TeamRef
    side_team: TeamRef
    event_date: date | None
    slug: str | None
    meta: MarketMeta | None
    token: TokenMeta | None
    status: str  # matched | no_market | unsupported_market | unresolved_team | no_outcome
    detail: str = ""


async def match_sharp_plays(
    plays: list[SharpPlay],
    *,
    store: CatalogStore | None = None,
    gamma: GammaClient | None = None,
    markets: frozenset[str] = frozenset({"moneyline"}),
    reward_rates: dict[str, float] | None = None,
) -> list[MatchedPlay]:
    """Resolve each play to a Polymarket market + outcome token."""
    owns_gamma = gamma is None
    client = gamma or GammaClient()
    # Cache Gamma events per series so we only page once per league.
    series_cache: dict[str, list[dict[str, Any]]] = {}
    try:
        out: list[MatchedPlay] = []
        for play in plays:
            out.append(
                await _match_one(
                    play,
                    store=store,
                    gamma=client,
                    markets=markets,
                    reward_rates=reward_rates,
                    series_cache=series_cache,
                )
            )
        return out
    finally:
        if owns_gamma:
            await client.aclose()


async def _match_one(
    play: SharpPlay,
    *,
    store: CatalogStore | None,
    gamma: GammaClient,
    markets: frozenset[str],
    reward_rates: dict[str, float] | None,
    series_cache: dict[str, list[dict[str, Any]]],
) -> MatchedPlay:
    if play.market not in markets:
        return MatchedPlay(
            play=play,
            away=_dummy_team(),
            home=_dummy_team(),
            side_team=_dummy_team(),
            event_date=None,
            slug=None,
            meta=None,
            token=None,
            status="unsupported_market",
            detail=f"market {play.market!r} not enabled (want {sorted(markets)})",
        )

    parsed = parse_matchup(play.matchup)
    if parsed is None:
        return MatchedPlay(
            play=play,
            away=_dummy_team(),
            home=_dummy_team(),
            side_team=_dummy_team(),
            event_date=None,
            slug=None,
            meta=None,
            token=None,
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
            return MatchedPlay(
                play=play,
                away=away or _dummy_team(away_raw),
                home=home or _dummy_team(home_raw),
                side_team=side,
                event_date=None,
                slug=None,
                meta=None,
                token=None,
                status="unresolved_team",
                detail="could not map team abbr to Polymarket code",
            )
    else:
        side = resolve_team(play.league, play.side)
        if away is None or home is None or side is None:
            return MatchedPlay(
                play=play,
                away=away or _dummy_team(away_raw),
                home=home or _dummy_team(home_raw),
                side_team=side or _dummy_team(play.side),
                event_date=None,
                slug=None,
                meta=None,
                token=None,
                status="unresolved_team",
                detail="could not map team abbr to Polymarket code",
            )

    event_dates = candidate_event_dates(play.game_time_utc)
    raw_date = play.raw.get("date") if isinstance(play.raw, dict) else None
    if isinstance(raw_date, str) and raw_date:
        try:
            extra = date.fromisoformat(raw_date[:10])
            if extra not in event_dates:
                event_dates.insert(0, extra)
        except ValueError:
            pass
    league = play.league.lower()
    if league == "cfb":
        league = "ncaaf"
    slug_league = "cfb" if league == "ncaaf" else league

    play_line = _play_line_number(play)
    target_line = _play_target_line(play, away=away, home=home, side=side)

    event = await _find_event(
        league=league,
        slug_league=slug_league,
        away=away,
        home=home,
        event_dates=event_dates,
        store=store,
        gamma=gamma,
        series_cache=series_cache,
    )

    meta: MarketMeta | None = None
    slug: str | None = None
    pick_detail = ""

    if event is not None:
        raw, pick_detail = pick_market_for_kind(
            event,
            play.market,
            target_line=target_line,
            max_line_delta=LINE_MATCH_TOLERANCE,
            play_line=play_line,
            home_away=play.home_away,
        )
        if raw is not None:
            parsed_meta = parse_market(_attach_parent_event(raw, event), reward_rates)
            if parsed_meta is not None:
                meta = parsed_meta
                slug = parsed_meta.slug
            else:
                pick_detail = pick_detail or f"no open {play.market}"
    else:
        # Catalog / constructed-slug fallback only when Gamma never returned the event.
        meta, slug, pick_detail = await _catalog_or_moneyline_fallback(
            play,
            slug_league=slug_league,
            away=away,
            home=home,
            event_dates=event_dates,
            store=store,
            gamma=gamma,
            reward_rates=reward_rates,
            target_line=target_line,
            play_line=play_line,
        )

    if meta is None:
        tried = ", ".join(d.isoformat() for d in event_dates) or "no-date"
        if pick_detail and ("mismatch" in pick_detail or pick_detail.startswith("no open ")):
            detail = pick_detail
        elif league in {"ufc", "ncaaf"}:
            detail = (
                f"no open {play.market} for {away.full_name} vs {home.full_name} on {tried}"
            )
        else:
            detail = (
                f"no open {play.market} for {away.poly_code}@{home.poly_code} on {tried}"
            )
        return MatchedPlay(
            play=play,
            away=away,
            home=home,
            side_team=side,
            event_date=event_dates[0] if event_dates else None,
            slug=slug,
            meta=None,
            token=None,
            status="no_market",
            detail=detail,
        )

    try:
        if is_total:
            token = resolve_total_outcome_token(meta, play.side)
        else:
            token = resolve_outcome_token(meta, side.full_name, side.poly_code)
    except ValueError as exc:
        return MatchedPlay(
            play=play,
            away=away,
            home=home,
            side_team=side,
            event_date=event_dates[0] if event_dates else None,
            slug=meta.slug,
            meta=meta,
            token=None,
            status="no_outcome",
            detail=str(exc),
        )

    return MatchedPlay(
        play=play,
        away=away,
        home=home,
        side_team=side,
        event_date=event_dates[0] if event_dates else None,
        slug=meta.slug,
        meta=meta,
        token=token,
        status="matched",
        detail="",
    )


def candidate_event_dates(game_time_utc: str | None) -> list[date]:
    """Plausible Polymarket `eventDate` values for a UTC tip-off."""
    if not game_time_utc:
        return []
    try:
        dt = datetime.fromisoformat(game_time_utc.replace("Z", "+00:00")).astimezone(UTC)
    except ValueError:
        return []
    dates = [
        dt.astimezone(_ET).date(),
        dt.astimezone(_PT).date(),
        dt.date(),
    ]
    # Late games can list on either side of midnight in some feeds.
    dates.extend([dates[0] - timedelta(days=1), dates[0] + timedelta(days=1)])
    # Preserve order, unique.
    seen: set[date] = set()
    out: list[date] = []
    for d in dates:
        if d not in seen:
            seen.add(d)
            out.append(d)
    return out


def resolve_outcome_token(meta: MarketMeta, full_name: str, poly_code: str) -> TokenMeta:
    """Map a team to the market's outcome token (exact / fuzzy)."""
    key = full_name.strip().lower()
    nick = key.rsplit(" ", 1)[-1]
    for t in meta.tokens:
        label = t.outcome.strip().lower()
        if label in (key, nick):
            return t
        if key in label or nick in label.split():
            return t
    # Last resort: token_for_outcome exact helpers
    try:
        return meta.token_for_outcome(full_name)
    except ValueError:
        pass
    raise ValueError(
        f"outcome {full_name!r} ({poly_code}) not in tokens={[t.outcome for t in meta.tokens]}"
    )


def resolve_total_outcome_token(meta: MarketMeta, side: str) -> TokenMeta:
    """Map Over/Under onto a total market's outcome tokens."""
    want = side.strip().lower()
    if want not in _TOTAL_SIDES:
        raise ValueError(f"total side {side!r} is not Over/Under")
    for t in meta.tokens:
        if t.outcome.strip().lower() == want:
            return t
    raise ValueError(
        f"outcome {side!r} not in tokens={[t.outcome for t in meta.tokens]}"
    )


def _play_line_number(play: SharpPlay) -> float | None:
    raw = play.raw if isinstance(play.raw, dict) else {}
    for key in ("live", "open"):
        val = raw.get(key)
        if isinstance(val, (int, float)) and not isinstance(val, bool):
            return float(val)
    return None


def _play_target_line(
    play: SharpPlay,
    *,
    away: TeamRef,
    home: TeamRef,
    side: TeamRef,
) -> float | None:
    """Home-spread or total number used to pick the closest Polymarket line."""
    line = _play_line_number(play)
    if line is None:
        return None
    if play.market == "total":
        return line
    if play.market != "spread":
        return None
    ha = (play.home_away or "").lower()
    if ha == "away":
        return -line
    if ha == "home":
        return line
    side_code = (side.poly_code or "").lower()
    if side_code and side_code == (away.poly_code or "").lower():
        return -line
    if side_code and side_code == (home.poly_code or "").lower():
        return line
    return line


def _attach_parent_event(raw: dict[str, Any], event: dict[str, Any]) -> dict[str, Any]:
    if raw.get("events"):
        return raw
    return {**raw, "events": [event]}


def _known_not_pre_game(event: dict[str, Any] | None) -> bool:
    """True when startTime is present and the game is not safe to trade."""
    if not event:
        return False
    start = event.get("startTime") or event.get("start_time")
    if not start:
        return False
    return not is_pre_game(event, DEFAULT_PREGAME_BUFFER_MINUTES)


def _meta_known_not_pre_game(meta: MarketMeta | None) -> bool:
    if meta is None or not meta.start_time_iso:
        return False
    return not is_pre_game({"startTime": meta.start_time_iso}, DEFAULT_PREGAME_BUFFER_MINUTES)


async def _find_event(
    *,
    league: str,
    slug_league: str,
    away: TeamRef,
    home: TeamRef,
    event_dates: list[date],
    store: CatalogStore | None,
    gamma: GammaClient,
    series_cache: dict[str, list[dict[str, Any]]],
) -> dict[str, Any] | None:
    """Resolve the Gamma sports EVENT (moneyline slug) for a matchup."""
    for d in event_dates:
        candidate = f"{slug_league}-{away.poly_code}-{home.poly_code}-{d.isoformat()}"
        event = await gamma.event_by_slug(candidate)
        if event and not _known_not_pre_game(event):
            return event

    if store is not None:
        _, cat_slug = _match_from_catalog(
            store, slug_league, away.poly_code, home.poly_code, event_dates
        )
        if cat_slug:
            event = await gamma.event_by_slug(cat_slug)
            if event and not _known_not_pre_game(event):
                return event

    if league == "ufc":
        events = await _series_events(gamma, league, series_cache)
        return _find_ufc_event(
            events,
            away_name=away.full_name,
            home_name=home.full_name,
            event_dates=event_dates,
        )
    if league == "ncaaf":
        events = await _series_events_for_slugs(
            gamma, _cfb_series_slugs(event_dates), series_cache
        )
        return _find_ncaaf_event(
            events,
            away_name=away.full_name,
            home_name=home.full_name,
            away_code=away.poly_code,
            home_code=home.poly_code,
            event_dates=event_dates,
        )
    events = await _series_events(gamma, league, series_cache)
    return _find_coded_event(
        events,
        league=league,
        away=away.poly_code,
        home=home.poly_code,
        event_dates=event_dates,
    )


async def _catalog_or_moneyline_fallback(
    play: SharpPlay,
    *,
    slug_league: str,
    away: TeamRef,
    home: TeamRef,
    event_dates: list[date],
    store: CatalogStore | None,
    gamma: GammaClient,
    reward_rates: dict[str, float] | None,
    target_line: float | None,
    play_line: float | None,
) -> tuple[MarketMeta | None, str | None, str]:
    """When Gamma did not yield a nested market, try catalog / moneyline slug lookup."""
    if play.market == "moneyline":
        for d in event_dates:
            candidate = f"{slug_league}-{away.poly_code}-{home.poly_code}-{d.isoformat()}"
            meta = await _resolve_meta(
                candidate, store=store, gamma=gamma, reward_rates=reward_rates
            )
            if meta is not None:
                return meta, meta.slug, ""
        if store is not None:
            meta, slug = _match_from_catalog(
                store, slug_league, away.poly_code, home.poly_code, event_dates
            )
            if meta is not None:
                return meta, slug, ""
        return None, None, ""

    if play.market in {"spread", "total"} and store is not None:
        return _match_kind_from_catalog(
            store,
            slug_league,
            away.poly_code,
            home.poly_code,
            event_dates,
            play.market,
            target_line=target_line,
            play_line=play_line,
            home_away=play.home_away,
        )
    return None, None, ""


async def _resolve_meta(
    slug: str,
    *,
    store: CatalogStore | None,
    gamma: GammaClient,
    reward_rates: dict[str, float] | None,
) -> MarketMeta | None:
    if store is not None:
        cached = store.get_by_slug(slug)
        if cached is not None and not _meta_known_not_pre_game(cached):
            return cached

    event = await gamma.event_by_slug(slug)
    if event and not _known_not_pre_game(event):
        raw = pick_moneyline_market(event)
        if raw:
            return parse_market(_attach_parent_event(raw, event), reward_rates)

    raw_m = await gamma.market_by_slug(slug)
    if raw_m:
        nested = (raw_m.get("events") or [None])[0]
        if isinstance(nested, dict) and _known_not_pre_game(nested):
            return None
        meta = parse_market(raw_m, reward_rates)
        if meta is not None and not _meta_known_not_pre_game(meta):
            return meta
    return None


def _match_from_catalog(
    store: CatalogStore,
    league: str,
    away: str,
    home: str,
    event_dates: list[date],
) -> tuple[MarketMeta | None, str | None]:
    prefix = f"{league}-{away}-{home}-"
    date_set = {d.isoformat() for d in event_dates}
    fallback: MarketMeta | None = None
    for meta in store.by_slug_prefix(prefix, limit=100):
        slug = meta.slug or ""
        if not is_moneyline_slug(slug):
            continue
        m = EVENT_SLUG_RE.match(slug)
        if not m:
            continue
        if date_set and m.group("ymd") in date_set:
            if _meta_known_not_pre_game(meta):
                continue
            return meta, slug
        if fallback is None and not _meta_known_not_pre_game(meta):
            fallback = meta
    if fallback is not None and not date_set:
        return fallback, fallback.slug
    return None, None


def _match_kind_from_catalog(
    store: CatalogStore,
    league: str,
    away: str,
    home: str,
    event_dates: list[date],
    kind: str,
    *,
    target_line: float | None,
    play_line: float | None,
    home_away: str | None,
) -> tuple[MarketMeta | None, str | None, str]:
    prefix = f"{league}-{away}-{home}-"
    date_set = {d.isoformat() for d in event_dates}
    usable: list[tuple[MarketMeta, float]] = []
    for meta in store.by_slug_prefix(prefix, limit=100):
        slug = meta.slug or ""
        if _meta_known_not_pre_game(meta):
            continue
        if kind == "spread":
            m = SPREAD_SLUG_RE.match(slug)
            if not m:
                continue
            if date_set and m.group("ymd") not in date_set:
                continue
            pts = parse_pt_number(m.group("pts"))
            if pts is None:
                continue
            usable.append((meta, implied_home_spread(m.group("favored"), pts)))
        elif kind == "total":
            m = TOTAL_SLUG_RE.match(slug)
            if not m:
                continue
            if date_set and m.group("ymd") not in date_set:
                continue
            pts = parse_pt_number(m.group("pts"))
            if pts is None:
                continue
            usable.append((meta, pts))
    if not usable:
        return None, None, f"no open {kind}"
    if target_line is None:
        meta = usable[0][0]
        return meta, meta.slug, ""
    best_meta, best_val = min(usable, key=lambda row: abs(row[1] - target_line))
    delta = abs(best_val - target_line)
    if delta < 0.01 or delta <= LINE_MATCH_TOLERANCE:
        return best_meta, best_meta.slug, ""
    ha = (home_away or "").lower()
    poly_for_play = -best_val if kind == "spread" and ha == "away" else best_val
    shown_play = play_line if play_line is not None else target_line
    shown_poly = poly_for_play if play_line is not None else best_val
    return None, None, f"{kind} line mismatch play={shown_play:g} poly={shown_poly:g}"


async def _series_events(
    gamma: GammaClient,
    league: str,
    cache: dict[str, list[dict[str, Any]]],
) -> list[dict[str, Any]]:
    if league in cache:
        return cache[league]
    events: list[dict[str, Any]] = []
    async for event in gamma.iter_events(series_slug=league, limit=50, max_pages=10):
        events.append(event)
    cache[league] = events
    return events


def _find_coded_event(
    events: list[dict[str, Any]],
    *,
    league: str,
    away: str,
    home: str,
    event_dates: list[date],
) -> dict[str, Any] | None:
    date_set = {d.isoformat() for d in event_dates}
    fallback: dict[str, Any] | None = None
    for event in events:
        slug = str(event.get("slug") or "")
        m = EVENT_SLUG_RE.match(slug)
        if not m:
            continue
        if m.group("league") != league or m.group("away") != away or m.group("home") != home:
            continue
        if _known_not_pre_game(event):
            continue
        event_date = str(event.get("eventDate") or "")[:10]
        if date_set and event_date in date_set:
            return event
        if fallback is None:
            fallback = event
    if fallback is not None and not date_set:
        return fallback
    return None


def _fighter_names_match(a: str, b: str) -> bool:
    ka, kb = a.strip().lower(), b.strip().lower()
    if not ka or not kb:
        return False
    if ka == kb or ka in kb or kb in ka:
        return True
    return ka.rsplit(" ", 1)[-1] == kb.rsplit(" ", 1)[-1]


def _ufc_event_fighter_names(event: dict[str, Any]) -> list[str]:
    event_slug = str(event.get("slug") or "")
    for raw in event.get("markets") or []:
        if not isinstance(raw, dict) or raw.get("closed"):
            continue
        if str(raw.get("slug") or "") != event_slug:
            continue
        outcomes = raw.get("outcomes") or []
        if isinstance(outcomes, str):
            try:
                import json

                parsed = json.loads(outcomes)
            except (json.JSONDecodeError, TypeError):
                parsed = []
            outcomes = parsed
        if isinstance(outcomes, list):
            return [str(x).strip() for x in outcomes if str(x).strip()]
    title = str(event.get("title") or "")
    m = re.search(r":\s*(.+?)\s+vs\.?\s+(.+?)\s*\(", title, re.IGNORECASE)
    if m:
        return [m.group(1).strip(), m.group(2).strip()]
    return []


def _find_ufc_event(
    events: list[dict[str, Any]],
    *,
    away_name: str,
    home_name: str,
    event_dates: list[date],
) -> dict[str, Any] | None:
    """Match a UFC bout by fighter names in Gamma outcomes / title."""
    date_set = {d.isoformat() for d in event_dates}
    fallback: dict[str, Any] | None = None
    for event in events:
        names = _ufc_event_fighter_names(event)
        if len(names) < 2:
            continue
        if not any(_fighter_names_match(away_name, n) for n in names):
            continue
        if not any(_fighter_names_match(home_name, n) for n in names):
            continue
        if _known_not_pre_game(event):
            continue
        event_date = str(event.get("eventDate") or "")[:10]
        if date_set and event_date in date_set:
            return event
        if fallback is None:
            fallback = event
    return fallback


async def _series_events_for_slugs(
    gamma: GammaClient,
    slugs: tuple[str, ...],
    cache: dict[str, list[dict[str, Any]]],
) -> list[dict[str, Any]]:
    events: list[dict[str, Any]] = []
    seen: set[str] = set()
    for slug in slugs:
        if slug not in cache:
            page: list[dict[str, Any]] = []
            async for event in gamma.iter_events(series_slug=slug, limit=50, max_pages=10):
                page.append(event)
            cache[slug] = page
        for event in cache[slug]:
            key = str(event.get("slug") or event.get("id") or id(event))
            if key in seen:
                continue
            seen.add(key)
            events.append(event)
    return events


def _cfb_series_slugs(event_dates: list[date]) -> tuple[str, ...]:
    years = {d.year for d in event_dates} or {datetime.now(UTC).year}
    slugs: list[str] = []
    for year in sorted(years):
        slugs.extend([f"cfb-{year}", f"cfb-{year - 1}", f"cfb-{year + 1}"])
    slugs.append("cfb")
    return tuple(dict.fromkeys(slugs))


def _cfb_names_match(a: str, b: str) -> bool:
    try:
        from cfb_team_map import names_match
    except ImportError:
        import sys
        from pathlib import Path

        agg = Path(__file__).resolve().parents[3] / "data-aggregation"
        if str(agg) not in sys.path:
            sys.path.insert(0, str(agg))
        from cfb_team_map import names_match
    return bool(names_match(a, b))


def _find_ncaaf_event(
    events: list[dict[str, Any]],
    *,
    away_name: str,
    home_name: str,
    away_code: str,
    home_code: str,
    event_dates: list[date],
) -> dict[str, Any] | None:
    """Match a CFB game by slug codes or school names in Gamma outcomes / title."""
    date_set = {d.isoformat() for d in event_dates}
    fallback: dict[str, Any] | None = None
    for event in events:
        slug = str(event.get("slug") or "")
        m = EVENT_SLUG_RE.match(slug)
        codes_ok = (
            m is not None
            and m.group("league") == "cfb"
            and m.group("away") == away_code
            and m.group("home") == home_code
        )
        names = _ufc_event_fighter_names(event)
        names_ok = (
            len(names) >= 2
            and any(_cfb_names_match(away_name, n) for n in names)
            and any(_cfb_names_match(home_name, n) for n in names)
        )
        if not codes_ok and not names_ok:
            continue
        if _known_not_pre_game(event):
            continue
        event_date = str(event.get("eventDate") or "")[:10]
        if date_set and event_date in date_set:
            return event
        if fallback is None:
            fallback = event
    return fallback


def _dummy_team(label: str = "?") -> TeamRef:
    return TeamRef(label, "", label)
