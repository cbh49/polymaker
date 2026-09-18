"""Canonical sharp-money trade keys that are venue-agnostic."""

from __future__ import annotations

from datetime import date

from polymaker.trading.match import candidate_event_dates
from polymaker.trading.sharp import SharpPlay
from polymaker.trading.teams import TeamRef, parse_matchup, resolve_team

_TOTAL_SIDES = frozenset({"over", "under"})


def normalize_league(league: str) -> str:
    key = (league or "").strip().lower()
    if key == "cfb":
        return "ncaaf"
    return key


def event_date_for_play(play: SharpPlay) -> date | None:
    """Best single date for the canonical key (ET game day when possible)."""
    raw = play.raw if isinstance(play.raw, dict) else {}
    raw_date = raw.get("date")
    if isinstance(raw_date, str) and raw_date:
        try:
            return date.fromisoformat(raw_date[:10])
        except ValueError:
            pass
    dates = candidate_event_dates(play.game_time_utc)
    return dates[0] if dates else None


def resolve_play_teams(
    play: SharpPlay,
) -> tuple[TeamRef, TeamRef, TeamRef] | None:
    """Return (away, home, side_team) or None if the matchup cannot be parsed."""
    parsed = parse_matchup(play.matchup)
    if parsed is None:
        return None
    away_raw, home_raw = parsed
    away = resolve_team(play.league, away_raw)
    home = resolve_team(play.league, home_raw)
    if away is None or home is None:
        return None
    if play.market == "total":
        side = TeamRef(play.side, play.side.strip().lower(), play.side)
        return away, home, side
    side = resolve_team(play.league, play.side)
    if side is None:
        return None
    return away, home, side


def canonical_side(play: SharpPlay, side_team: TeamRef) -> str:
    if play.market == "total":
        return play.side.strip().lower()
    return (side_team.poly_code or side_team.betting_abbr).strip().lower()


def canonical_trade_key(
    play: SharpPlay,
    *,
    away: TeamRef,
    home: TeamRef,
    side_team: TeamRef,
    event_date: date | None = None,
) -> str:
    """One moneyline / spread-side / total-side per game, independent of venue ids.

    `sharp|{league}|{ymd}|{away}|{home}|{market}|{side}`
    """
    league = normalize_league(play.league)
    ymd = (event_date or event_date_for_play(play))
    ymd_s = ymd.isoformat() if ymd is not None else "na"
    away_c = (away.poly_code or away.betting_abbr).strip().lower()
    home_c = (home.poly_code or home.betting_abbr).strip().lower()
    market = (play.market or "moneyline").strip().lower()
    side = canonical_side(play, side_team)
    if market == "total" and side not in _TOTAL_SIDES:
        side = side.lower()
    return f"sharp|{league}|{ymd_s}|{away_c}|{home_c}|{market}|{side}"


def canonical_trade_key_for_play(play: SharpPlay) -> str | None:
    teams = resolve_play_teams(play)
    if teams is None:
        return None
    away, home, side_team = teams
    return canonical_trade_key(
        play,
        away=away,
        home=home,
        side_team=side_team,
        event_date=event_date_for_play(play),
    )


def poly_slug_key(slug: str, outcome: str) -> str:
    return f"{slug.strip()}|{outcome.strip().lower()}"
