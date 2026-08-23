"""Match sharp-money plays to Polymarket moneyline markets."""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

from polymaker.catalog.gamma import GammaClient, parse_market
from polymaker.catalog.sports import (
    DEFAULT_PREGAME_BUFFER_MINUTES,
    is_moneyline_slug,
    is_pre_game,
    pick_moneyline_market,
)
from polymaker.catalog.store import CatalogStore
from polymaker.domain import MarketMeta, TokenMeta
from polymaker.trading.sharp import SharpPlay
from polymaker.trading.teams import TeamRef, parse_matchup, resolve_team

_ET = ZoneInfo("America/New_York")
_PT = ZoneInfo("America/Los_Angeles")

_SLUG_TEAMS_RE = re.compile(
    r"^(?P<league>mlb|wnba|ufc)-(?P<away>[a-z0-9]+)-(?P<home>[a-z0-9]+)-(?P<ymd>\d{4}-\d{2}-\d{2})$"
)


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
    """Resolve each play to a Polymarket moneyline market + outcome token."""
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

    meta: MarketMeta | None = None
    slug: str | None = None

    if league == "ufc":
        events = await _series_events(gamma, league, series_cache)
        meta, slug = await _match_ufc_from_events(
            events,
            away_name=away.full_name,
            home_name=home.full_name,
            event_dates=event_dates,
            gamma=gamma,
            reward_rates=reward_rates,
        )
    else:
        # 1) Try constructed slugs for each plausible calendar date.
        for d in event_dates:
            candidate = f"{league}-{away.poly_code}-{home.poly_code}-{d.isoformat()}"
            meta = await _resolve_meta(candidate, store=store, gamma=gamma, reward_rates=reward_rates)
            if meta is not None:
                slug = meta.slug
                break

        # 2) Catalog scan: any moneyline with matching away/home codes.
        if meta is None and store is not None:
            meta, slug = _match_from_catalog(store, league, away.poly_code, home.poly_code, event_dates)

        # 3) Gamma series sweep filtered by codes + eventDate.
        if meta is None:
            events = await _series_events(gamma, league, series_cache)
            meta, slug = await _match_from_events(
                events,
                league=league,
                away=away.poly_code,
                home=home.poly_code,
                event_dates=event_dates,
                gamma=gamma,
                reward_rates=reward_rates,
            )

    if meta is None:
        tried = ", ".join(d.isoformat() for d in event_dates) or "no-date"
        return MatchedPlay(
            play=play,
            away=away,
            home=home,
            side_team=side,
            event_date=event_dates[0] if event_dates else None,
            slug=None,
            meta=None,
            token=None,
            status="no_market",
            detail=f"no open moneyline for {away.full_name} vs {home.full_name} on {tried}"
            if league == "ufc"
            else f"no open moneyline for {away.poly_code}@{home.poly_code} on {tried}",
        )

    try:
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
    for meta in store.by_slug_prefix(prefix, limit=20):
        slug = meta.slug or ""
        if not is_moneyline_slug(slug):
            continue
        m = _SLUG_TEAMS_RE.match(slug)
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


async def _match_from_events(
    events: list[dict[str, Any]],
    *,
    league: str,
    away: str,
    home: str,
    event_dates: list[date],
    gamma: GammaClient,
    reward_rates: dict[str, float] | None,
) -> tuple[MarketMeta | None, str | None]:
    date_set = {d.isoformat() for d in event_dates}
    fallback_raw: dict[str, Any] | None = None
    for event in events:
        slug = str(event.get("slug") or "")
        m = _SLUG_TEAMS_RE.match(slug)
        if not m:
            continue
        if m.group("league") != league or m.group("away") != away or m.group("home") != home:
            continue
        if _known_not_pre_game(event):
            continue
        event_date = str(event.get("eventDate") or "")[:10]
        raw = pick_moneyline_market(event)
        if raw is None:
            continue
        raw = _attach_parent_event(raw, event)
        if date_set and event_date in date_set:
            meta = parse_market(raw, reward_rates)
            if meta is not None:
                return meta, meta.slug
        if fallback_raw is None:
            fallback_raw = raw
    if fallback_raw is not None and not date_set:
        meta = parse_market(fallback_raw, reward_rates)
        if meta is not None:
            return meta, meta.slug
    return None, None


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


async def _match_ufc_from_events(
    events: list[dict[str, Any]],
    *,
    away_name: str,
    home_name: str,
    event_dates: list[date],
    gamma: GammaClient,
    reward_rates: dict[str, float] | None,
) -> tuple[MarketMeta | None, str | None]:
    """Match a UFC bout by fighter names in Gamma outcomes / title."""
    _ = gamma
    date_set = {d.isoformat() for d in event_dates}
    fallback_raw: dict[str, Any] | None = None
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
        raw = pick_moneyline_market(event)
        if raw is None:
            continue
        raw = _attach_parent_event(raw, event)
        event_date = str(event.get("eventDate") or "")[:10]
        if date_set and event_date in date_set:
            meta = parse_market(raw, reward_rates)
            if meta is not None:
                return meta, meta.slug
        if fallback_raw is None:
            fallback_raw = raw
    if fallback_raw is not None:
        meta = parse_market(fallback_raw, reward_rates)
        if meta is not None:
            return meta, meta.slug
    return None, None


def _dummy_team(label: str = "?") -> TeamRef:
    return TeamRef(label, "", label)
