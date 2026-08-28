"""Same-day source alignment for MLB/WNBA/NCAAF sharp-money trading.

A league is tradeable only when every *required* splits source is on the
Pacific slate day (NCAAF: a weekend window) and at least one game has
overlapping fields from all of those sources. EVA / Covers are enrichment
and never block trading. Pinnacle is not used for NCAAF.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

PAGE_TZ = ZoneInfo("America/Los_Angeles")

MLB_REQUIRED: tuple[str, ...] = ("primary", "vsin", "sbd")
WNBA_REQUIRED: tuple[str, ...] = ("primary", "vsin", "thespread")
NCAAF_REQUIRED: tuple[str, ...] = ("primary", "vsin", "sbd")
NCAAF_SLATE_WINDOW_DAYS = 6

# Combined-file `sources` object keys for each logical source.
MLB_SOURCE_KEYS: dict[str, str] = {
    "primary": "playerprops",
    "vsin": "vsin",
    "sbd": "sportsbettingdime",
}
WNBA_SOURCE_KEYS: dict[str, str] = {
    "primary": "draftkings",
    "vsin": "vsin",
    "thespread": "thespread",
}
NCAAF_SOURCE_KEYS: dict[str, str] = {
    "primary": "draftkings",
    "vsin": "vsin",
    "sbd": "sportsbettingdime",
}

# Fields that prove a source merged onto a game (checked on moneyline unless noted).
_SOURCE_FIELDS: dict[str, tuple[str, str]] = {
    "primary": ("moneyline", "public_bet_pct"),
    "vsin": ("moneyline", "vsin_handle_bet_pct"),
    "sbd": ("moneyline", "sbd_handle_bet_pct"),
    "thespread": ("spread", "open"),
}


def pacific_today(now: datetime | None = None) -> date:
    stamp = now or datetime.now(PAGE_TZ)
    if stamp.tzinfo is None:
        stamp = stamp.replace(tzinfo=PAGE_TZ)
    return stamp.astimezone(PAGE_TZ).date()


def game_slate_date(game: dict[str, Any], tz: ZoneInfo = PAGE_TZ) -> date | None:
    raw = game.get("date")
    if isinstance(raw, str) and raw:
        try:
            return date.fromisoformat(raw[:10])
        except ValueError:
            pass
    ts = game.get("game_time_utc") or game.get("scheduled")
    if not ts:
        return None
    try:
        dt = datetime.fromisoformat(str(ts).replace("Z", "+00:00"))
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=tz)
    return dt.astimezone(tz).date()


def same_slate(src_game: dict[str, Any], dest_game: dict[str, Any], day: date, window_days: int = 0) -> bool:
    """True when src belongs on this slate (not yesterday's rematch)."""
    src_day = game_slate_date(src_game)
    dest_day = game_slate_date(dest_game)
    if window_days > 0:
        if src_day is None:
            return dest_day is None or in_slate_window(dest_day, day, window_days)
        if dest_day is not None:
            return src_day == dest_day
        return in_slate_window(src_day, day, window_days)
    if src_day is None:
        return dest_day == day or dest_day is None
    if dest_day is not None:
        return src_day == dest_day
    return src_day == day


def native_dates(games: list[dict[str, Any]]) -> list[str]:
    found: set[str] = set()
    for game in games:
        if not isinstance(game, dict):
            continue
        day = game_slate_date(game)
        if day is not None:
            found.add(day.isoformat())
    return sorted(found)


def normalize_league(league: str | None) -> str:
    key = str(league or "MLB").strip().upper()
    if key == "CFB":
        return "NCAAF"
    return key


def slate_window_days(league: str | None) -> int:
    return NCAAF_SLATE_WINDOW_DAYS if normalize_league(league) == "NCAAF" else 0


def in_slate_window(game_day: date | None, slate_day: date, window_days: int) -> bool:
    if game_day is None:
        return True
    if window_days <= 0:
        return game_day == slate_day
    return slate_day <= game_day <= slate_day + timedelta(days=window_days)


def required_sources(league: str | None) -> tuple[str, ...]:
    key = normalize_league(league)
    if key == "WNBA":
        return WNBA_REQUIRED
    if key == "NCAAF":
        return NCAAF_REQUIRED
    return MLB_REQUIRED


def source_block_key(league: str | None, logical: str) -> str:
    key = normalize_league(league)
    if key == "WNBA":
        mapping = WNBA_SOURCE_KEYS
    elif key == "NCAAF":
        mapping = NCAAF_SOURCE_KEYS
    else:
        mapping = MLB_SOURCE_KEYS
    return mapping.get(logical, logical)


def _side_has_field(game: dict[str, Any], market: str, field: str) -> bool:
    block = game.get(market)
    if not isinstance(block, dict):
        return False
    for side in ("away", "home", "over", "under"):
        row = block.get(side)
        if isinstance(row, dict) and row.get(field) is not None:
            return True
    return False


def game_has_source(game: dict[str, Any], logical: str) -> bool:
    spec = _SOURCE_FIELDS.get(logical)
    if spec is None:
        return False
    market, field = spec
    if _side_has_field(game, market, field):
        return True
    if logical in {"primary", "vsin", "sbd"}:
        for extra in ("spread", "total", "moneyline"):
            if extra != market and _side_has_field(game, extra, field):
                return True
    if logical == "thespread":
        for extra in ("moneyline", "total"):
            if _side_has_field(game, extra, "open"):
                return True
    return False


def overlap_games(
    games: list[dict[str, Any]],
    slate_day: date,
    sources: tuple[str, ...],
    window_days: int = 0,
) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for game in games:
        if not isinstance(game, dict):
            continue
        gday = game_slate_date(game)
        if gday is not None and not in_slate_window(gday, slate_day, window_days):
            continue
        if all(game_has_source(game, src) for src in sources):
            out.append(game)
    return out


@dataclass(frozen=True, slots=True)
class SourceStatus:
    logical: str
    key: str
    native_dates: list[str]
    game_count: int
    on_slate: bool


@dataclass(frozen=True, slots=True)
class AlignmentResult:
    aligned: bool
    league: str
    slate_day: date
    overlap_count: int
    sources: dict[str, SourceStatus] = field(default_factory=dict)
    reason: str = ""

    def as_dict(self) -> dict[str, Any]:
        return {
            "aligned": self.aligned,
            "league": self.league,
            "slate_day": self.slate_day.isoformat(),
            "overlap_count": self.overlap_count,
            "reason": self.reason,
            "sources": {
                name: {
                    "key": st.key,
                    "native_dates": st.native_dates,
                    "game_count": st.game_count,
                    "on_slate": st.on_slate,
                }
                for name, st in self.sources.items()
            },
        }


def _source_native_dates(
    payload: dict[str, Any],
    logical: str,
    block_key: str,
    slate_day: date,
    window_days: int = 0,
) -> tuple[list[str], int]:
    sources = payload.get("sources") if isinstance(payload.get("sources"), dict) else {}
    block = sources.get(block_key) if isinstance(sources, dict) else None
    dates: list[str] = []
    count = 0
    if isinstance(block, dict):
        raw_dates = block.get("native_dates")
        if isinstance(raw_dates, list):
            dates = [str(d)[:10] for d in raw_dates if d]
        count = int(block.get("game_count") or 0)
        if count == 0:
            count = int(
                block.get("merged_into_playerprops_games")
                or block.get("merged_into_draftkings_games")
                or 0
            )
    if not dates:
        # Fall back to inspecting merged games on the slate.
        games = [g for g in (payload.get("games") or []) if isinstance(g, dict)]
        matching = [
            g
            for g in games
            if in_slate_window(game_slate_date(g), slate_day, window_days)
            and game_has_source(g, logical)
        ]
        dates = native_dates(matching) or (
            [slate_day.isoformat()] if matching else []
        )
        if count == 0:
            count = len(matching)
    return dates, count


def evaluate_payload(
    payload: dict[str, Any],
    *,
    slate_day: date | None = None,
) -> AlignmentResult:
    league = normalize_league(payload.get("league") or "MLB")
    day = slate_day or _payload_slate_day(payload)
    needed = required_sources(league)
    window = slate_window_days(league)
    games = [g for g in (payload.get("games") or []) if isinstance(g, dict)]
    statuses: dict[str, SourceStatus] = {}
    missing: list[str] = []

    for logical in needed:
        key = source_block_key(league, logical)
        dates, count = _source_native_dates(payload, logical, key, day, window)
        on_slate = count > 0 and any(
            in_slate_window(date.fromisoformat(d), day, window) for d in dates if len(d) >= 10
        )
        if not on_slate and not dates and count > 0 and window > 0:
            on_slate = True
        statuses[logical] = SourceStatus(
            logical=logical,
            key=key,
            native_dates=dates,
            game_count=count,
            on_slate=on_slate,
        )
        if not on_slate:
            missing.append(f"{logical}({key})")

    overlap = overlap_games(games, day, needed, window_days=window)
    if missing:
        reason = (
            f"{league} sources not on {day.isoformat()}: {', '.join(missing)}. "
            "Skip trading and poll again."
        )
        return AlignmentResult(
            aligned=False,
            league=league,
            slate_day=day,
            overlap_count=len(overlap),
            sources=statuses,
            reason=reason,
        )
    if not overlap:
        reason = (
            f"{league} sources report {day.isoformat()} but no overlapping matchups "
            "have all required fields. Skip trading and poll again."
        )
        return AlignmentResult(
            aligned=False,
            league=league,
            slate_day=day,
            overlap_count=0,
            sources=statuses,
            reason=reason,
        )
    return AlignmentResult(
        aligned=True,
        league=league,
        slate_day=day,
        overlap_count=len(overlap),
        sources=statuses,
        reason=f"{league} aligned on {day.isoformat()} ({len(overlap)} overlapping games)",
    )


def _payload_slate_day(payload: dict[str, Any]) -> date:
    raw = payload.get("date")
    if isinstance(raw, str) and len(raw) >= 10:
        try:
            return date.fromisoformat(raw[:10])
        except ValueError:
            pass
    return pacific_today()
