"""Select sportsbook + Kalshi/Polymarket +EV rows (>= 5pp vs consensus) and post Discord + X cards."""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from ev_trading.fair_value.config import FairValueConfig
from ev_trading.fair_value.consensus import shift_over_prob
from ev_trading.fair_value.devig import prob_to_american
from ev_trading.fair_value.ev_ledger import DEFAULT_MIN_EDGE_PCT
from ev_trading.fair_value.models import BookPoint, FittedDistribution, InformationalRow
from ev_trading.fair_value.pipeline import (
    _TWO_WAY_STATS,
    _fair_at_line,
    _ou_points,
    _spread_points,
    _threshold_points,
)
from ev_trading.fair_value.report import FairValueReport
from ev_trading.fair_value.tradable_pricer import kalshi_no_ask, kalshi_yes_ask, polymarket_side_ask
from ev_trading.nfl_odds import TEAM_ABBR_TO_NAME, canonical_abbr

PREDICTION_VENUES = frozenset({"kalshi", "polymarket"})


def _bot_root() -> Path:
    here = Path(__file__).resolve()
    for parent in here.parents:
        if (parent / "public" / "nfl-team-logos.json").is_file():
            return parent
    return here.parents[3]


_BOT_ROOT = _bot_root()
PUBLIC_DIR = _BOT_ROOT / "public"
PAGE_TZ = ZoneInfo("America/Los_Angeles")
DEFAULT_CACHE = _BOT_ROOT / "output" / "ev" / ".ev_alerts_sent.json"
DEFAULT_CARD_DIR = _BOT_ROOT / "output" / "ev" / "alert_cards"

BOOK_LOGOS: dict[str, str] = {
    "draftkings": "DraftKings.png",
    "fanduel": "fanduel.png",
    "mgm": "BetMGM.png",
    "hardrock": "HardRock.png",
    "caesars": "caesars.png",
    "betrivers": "betrivers.png",
    "betr": "betrivers.png",
    "thescore": "TheScore.png",
    "circasports": "Circa.png",
    "circa": "Circa.png",
    "kalshi": "kalshi.png",
    "polymarket": "polymarket.png",
}

BOOK_LABELS: dict[str, str] = {
    "draftkings": "DraftKings",
    "fanduel": "FanDuel",
    "mgm": "BetMGM",
    "hardrock": "Hard Rock",
    "caesars": "Caesars",
    "betrivers": "BetRivers",
    "betr": "BetRivers",
    "thescore": "theScore",
    "circasports": "Circa",
    "circa": "Circa",
    "fanatics": "Fanatics",
    "pinnacle": "Pinnacle",
    "kalshi": "Kalshi",
    "polymarket": "Polymarket",
}

BOOK_ALIASES: dict[str, str] = {
    "betr": "betrivers",
    "circa": "circasports",
    "betmgm": "mgm",
    "hardrockbet": "hardrock",
}

CARD_SPORTSBOOKS: tuple[str, ...] = (
    "draftkings",
    "fanduel",
    "mgm",
    "hardrock",
    "caesars",
    "betrivers",
)
CARD_BOOKS: tuple[str, ...] = CARD_SPORTSBOOKS + ("kalshi", "polymarket")

BOOK_ORDER: tuple[str, ...] = CARD_BOOKS

STAT_SHORT: dict[str, str] = {
    "rushing_yards": "Rush Yards",
    "receiving_yards": "Rec Yards",
    "passing_yards": "Pass Yards",
    "rushing_receiving_yards": "Rush+Rec Yards",
    "receptions": "Receptions",
    "interceptions": "INTs",
    "passing_tds": "Pass TDs",
    "rushing_tds": "Rush TDs",
    "receiving_tds": "Rec TDs",
    "anytime_td": "Anytime TD",
    "first_td": "First TD",
    "2plus_td": "2+ TDs",
    "total": "Total",
    "spread": "Spread",
    "moneyline": "ML",
}


def load_env() -> None:
    try:
        from dotenv import load_dotenv
    except ImportError:
        return
    load_dotenv(_BOT_ROOT / ".env")
    load_dotenv()


def _flag(name: str) -> str:
    return (os.environ.get(name) or "").strip().lower()


def format_american(odds: float | None) -> str:
    if odds is None:
        return "—"
    n = int(round(float(odds)))
    return f"+{n}" if n > 0 else str(n)


def implied_american(prob: float) -> int:
    return prob_to_american(prob)


def canonical_book(book: str) -> str:
    key = (book or "").strip().lower()
    return BOOK_ALIASES.get(key, key)


def book_label(book: str) -> str:
    key = canonical_book(book)
    return BOOK_LABELS.get(key, key.replace("_", " ").title() or "Book")


def book_logo_path(book: str, *, public_dir: Path | None = None) -> Path | None:
    key = canonical_book(book)
    filename = BOOK_LOGOS.get(key)
    if not filename:
        return None
    path = (public_dir or PUBLIC_DIR) / filename
    return path if path.is_file() else None


def parse_matchup_abbrs(matchup: str) -> tuple[str, str] | None:
    text = (matchup or "").replace(" vs ", " @ ").replace(" VS ", " @ ")
    if "@" not in text:
        return None
    away, home = (p.strip().upper() for p in text.split("@", 1))
    if not away or not home:
        return None
    return canonical_abbr(away), canonical_abbr(home)


def team_full_name(abbr: str) -> str:
    return TEAM_ABBR_TO_NAME.get(canonical_abbr(abbr), abbr)


def _signed_line(value: float) -> str:
    if value > 0:
        return f"+{value:g}"
    return f"{value:g}"


def _stat_short(stat: str) -> str:
    key = (stat or "").strip().lower()
    if key in STAT_SHORT:
        return STAT_SHORT[key]
    return key.replace("_", " ").title() or "Prop"


def format_bet_title(
    *,
    player: str | None,
    stat: str,
    side: str,
    line: float | None,
    matchup: str,
) -> str:
    abbrs = parse_matchup_abbrs(matchup)
    away, home = abbrs if abbrs else ("AWAY", "HOME")
    key = (stat or "").strip().lower()
    side_key = (side or "").strip().lower()

    if key == "moneyline":
        team = home if side_key == "home" else away
        return f"{team} ML"
    if key == "spread":
        team = home if side_key == "home" else away
        shown = line
        if shown is not None and side_key == "away":
            shown = -shown
        if shown is None:
            return f"{team} Spread"
        return f"{team} {_signed_line(shown)}"
    if key == "total":
        if line is None:
            return "Total"
        if side_key == "under":
            return f"Under {line:g} Total"
        return f"Over {line:g} Total"

    short = _stat_short(key)
    name = (player or "").strip()
    prefix = f"{name} " if name else ""
    if side_key == "yes" or (key.endswith("_td") and not key.endswith("_tds")):
        return f"{prefix}{short}".strip()
    if side_key == "over" and line is not None:
        if abs(line - int(line) - 0.5) < 1e-9:
            return f"{prefix}{int(line) + 1}+ {short}".strip()
        if abs(line - int(line)) < 1e-9:
            return f"{prefix}{int(line)}+ {short}".strip()
        return f"{prefix}Over {line:g} {short}".strip()
    if side_key == "under" and line is not None:
        return f"{prefix}Under {line:g} {short}".strip()
    return f"{prefix}{short}".strip()


@dataclass(frozen=True, slots=True)
class BookQuote:
    book: str
    odds: float | None
    line: float | None = None

    @property
    def american(self) -> str:
        return format_american(self.odds)


@dataclass(frozen=True, slots=True)
class SportsbookEvAlert:
    market: str
    matchup: str
    player: str | None
    stat: str
    side: str
    book: str
    book_line: float | None
    book_odds: float
    book_prob: float
    fair_prob: float
    raw_edge: float
    n_books: int
    quotes: tuple[BookQuote, ...] = field(default_factory=tuple)

    @property
    def edge_pct(self) -> float:
        return self.raw_edge * 100.0

    @property
    def title(self) -> str:
        return format_bet_title(
            player=self.player,
            stat=self.stat,
            side=self.side,
            line=self.book_line,
            matchup=self.matchup,
        )

    @property
    def fair_american(self) -> int:
        return implied_american(self.fair_prob)

    @property
    def play_american(self) -> int:
        return int(round(self.book_odds))

    def alert_key(self, *, day: str) -> str:
        line = f"{self.book_line:g}" if self.book_line is not None else ""
        player = (self.player or "").strip().lower()
        book = (self.book or "").strip().lower()
        return (f"{day}|{self.matchup}|{player}|{self.stat}|{self.side}|{line}|{book}").lower()


class SentCache:
    """Persist posted alert keys so the 30-minute timer does not re-spam."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self.keys: set[str] = set()
        if path.is_file():
            try:
                raw = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                raw = {}
            stored = raw.get("keys") if isinstance(raw, dict) else raw
            if isinstance(stored, list):
                self.keys = {str(k) for k in stored}

    def save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "updated_at": datetime.now(tz=PAGE_TZ).isoformat(),
            "keys": sorted(self.keys),
        }
        self.path.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")


def pacific_today_iso() -> str:
    return datetime.now(tz=PAGE_TZ).date().isoformat()


def x_ev_posts_enabled() -> bool:
    """X_EV_POSTS=0 disables. Unset posts when X credentials are present."""
    explicit = _flag("X_EV_POSTS")
    if explicit in {"0", "false", "no", "off"}:
        return False
    if explicit in {"1", "true", "yes", "on"}:
        return True
    from polymaker.x_client import credentials_ready

    return credentials_ready()


def _f(value: Any) -> float | None:
    if value is None or value == "":
        return None
    try:
        out = float(value)
    except (TypeError, ValueError):
        return None
    if out != out:
        return None
    return out


def _american(value: Any) -> float | None:
    num = _f(value)
    if num is None or abs(num) <= 1.0:
        return None
    return num


def _group_key(row: InformationalRow) -> tuple[str, str, str, str, str, str]:
    line = f"{row.book_line:g}" if row.book_line is not None else ""
    book = (row.book or "").strip().lower()
    lane = book if book in PREDICTION_VENUES else "sportsbook"
    return (
        (row.matchup or "").strip().lower(),
        (row.player or "").strip().lower(),
        (row.stat or "").strip().lower(),
        (row.side or "").strip().lower(),
        line,
        lane,
    )


MAX_POST_AMERICAN = -150
MAX_POST_PROB = 0.60


def _display_odds(row: InformationalRow) -> float | None:
    if row.book_odds is not None:
        return float(row.book_odds)
    if row.book_prob > 0:
        return float(implied_american(row.book_prob))
    return None


def is_heavy_favorite(*, odds: float | None, prob: float | None) -> bool:
    """True when the play is -150 or juicier, or implied p is 60%+."""
    return (odds is not None and odds <= MAX_POST_AMERICAN) or (
        prob is not None and prob >= MAX_POST_PROB
    )


def select_sportsbook_alerts(
    rows: list[InformationalRow],
    *,
    min_edge_pct: float = DEFAULT_MIN_EDGE_PCT,
    min_books: int = 3,
) -> list[InformationalRow]:
    """Best sportsbook plus Kalshi/Polymarket per market/side/line if edge >= cutoff."""
    floor = min_edge_pct / 100.0
    grouped: dict[tuple[str, str, str, str, str, str], list[InformationalRow]] = {}
    for row in rows:
        if row.n_books < min_books:
            continue
        if row.raw_edge < floor:
            continue
        odds = _display_odds(row)
        if odds is None:
            continue
        if is_heavy_favorite(odds=odds, prob=row.book_prob):
            continue
        grouped.setdefault(_group_key(row), []).append(row)

    winners: list[InformationalRow] = []
    for group in grouped.values():
        with_logo = [r for r in group if book_logo_path(r.book) is not None]
        pool = with_logo or group
        pool.sort(key=lambda r: r.raw_edge, reverse=True)
        winners.append(pool[0])
    winners.sort(key=lambda r: r.raw_edge, reverse=True)
    return winners


def _find_game(payload: dict[str, Any], matchup: str) -> dict[str, Any] | None:
    want = (matchup or "").strip().lower()
    for game in payload.get("games") or []:
        if not isinstance(game, dict):
            continue
        if str(game.get("matchup") or "").strip().lower() == want:
            return game
    return None


def _market_blob(game: dict[str, Any], row: InformationalRow) -> dict[str, Any] | None:
    stat = (row.stat or "").strip().lower()
    raw_markets = game.get("markets")
    markets = raw_markets if isinstance(raw_markets, dict) else {}
    if stat in {"moneyline", "spread", "total"}:
        blob = markets.get(stat)
        if isinstance(blob, dict):
            return blob
    player = (row.player or "").strip().lower()
    for prop in game.get("player_props") or []:
        if not isinstance(prop, dict):
            continue
        if str(prop.get("type") or "").strip().lower() != stat:
            continue
        if str(prop.get("player") or "").strip().lower() != player:
            continue
        return prop
    return None


def _quote_from_entry(
    book: str,
    entry: dict[str, Any],
    *,
    side: str,
) -> BookQuote | None:
    side_key = (side or "").strip().lower()
    if side_key in {"over", "under"}:
        odds = _american(entry.get(f"{side_key}_odds"))
        if odds is None:
            odds = _prob_odds(entry.get(f"{side_key}_implied_prob"))
        line = _f(entry.get("line"))
    elif side_key in {"home", "away"}:
        blob = entry.get(side_key)
        if not isinstance(blob, dict):
            return None
        odds = _american(blob.get("odds"))
        if odds is None:
            odds = _prob_odds(blob.get("implied_prob"))
        line = _f(blob.get("line"))
        if line is None:
            line = _f(entry.get("line"))
    elif side_key == "yes":
        odds = _american(entry.get("odds"))
        if odds is None:
            odds = _prob_odds(entry.get("implied_prob"))
        line = _f(entry.get("line"))
    else:
        return None
    if odds is None:
        return None
    return BookQuote(book=canonical_book(book), odds=odds, line=line)


def _venue_line(blob: dict[str, Any]) -> float | None:
    line = _f(blob.get("line"))
    if line is not None and abs(line) >= 100:
        return None
    return line


def _kalshi_side_ask(blob: dict[str, Any], side: str) -> float | None:
    key = (side or "").strip().lower()
    nested = blob.get(key)
    if isinstance(nested, dict):
        yes = kalshi_yes_ask(nested)
        if yes is not None:
            return yes
    if key in {"over", "yes", "home"}:
        return kalshi_yes_ask(blob)
    if key in {"under", "no", "away"}:
        return kalshi_no_ask(blob)
    return None


def _quote_from_venue(
    book: str,
    blob: dict[str, Any] | None,
    *,
    side: str,
) -> BookQuote | None:
    if not isinstance(blob, dict):
        return None
    key = (side or "").strip().lower()
    if book == "kalshi":
        ask = _kalshi_side_ask(blob, key)
        if ask is None and key == "yes":
            ask = _kalshi_side_ask(blob, "over")
    else:
        ask = polymarket_side_ask(blob, key)
        if ask is None and key == "yes":
            ask = polymarket_side_ask(blob, "over")
    if ask is None:
        return None
    line = _venue_line(blob)
    nested = blob.get(key)
    if line is None and isinstance(nested, dict):
        line = _venue_line(nested)
    return BookQuote(book=book, odds=float(prob_to_american(ask)), line=line)


def _prob_odds(value: Any) -> float | None:
    prob = _f(value)
    if prob is None or prob <= 0.0 or prob >= 1.0:
        return None
    return float(prob_to_american(prob))


def _pick_closest(quotes: list[BookQuote], target: float | None) -> BookQuote | None:
    if not quotes:
        return None
    if target is None:
        return quotes[0]
    same = [q for q in quotes if q.line is not None and abs(q.line - target) <= 0.26]
    if same:
        return same[0]
    with_line = [q for q in quotes if q.line is not None]
    if with_line:
        return min(with_line, key=lambda q: abs(float(q.line or 0.0) - target))
    return quotes[0]


_UNDER_SIDES = frozenset({"under", "no", "away"})
_SAME_STRIKE_STATS = frozenset({"moneyline", "anytime_td", "first_td", "2plus_td"})


def _quote_points(
    stat: str,
    books: dict[str, Any],
    cfg: FairValueConfig,
) -> tuple[list[BookPoint], bool]:
    """BookPoints for card translation. ``True`` means fit on margin thresholds."""
    key = (stat or "").strip().lower()
    if key in _SAME_STRIKE_STATS:
        return [], False
    if key == "spread":
        return _spread_points(books, cfg), True
    if key in _TWO_WAY_STATS:
        return _ou_points(books, cfg), False
    return [], False


def _fit_for_title_strike(
    points: list[BookPoint],
    *,
    stat: str,
    target: float,
    threshold: bool,
    cfg: FairValueConfig,
) -> FittedDistribution | None:
    if threshold:
        _, fit, _ = _fair_at_line(
            _threshold_points(points),
            stat=stat,
            target_line=-target,
            cfg=cfg,
        )
        return fit
    _, fit, _ = _fair_at_line(points, stat=stat, target_line=target, cfg=cfg)
    return fit


def _match_book_point(
    quote: BookQuote,
    points: list[BookPoint],
    *,
    side: str,
) -> BookPoint | None:
    if not points:
        return None
    candidates = points
    if quote.line is not None:
        lined = [p for p in candidates if abs(p.line - quote.line) <= 1e-9]
        if lined:
            candidates = lined
    if quote.odds is not None:
        attr = "under_odds" if side in _UNDER_SIDES else "over_odds"
        matched = [
            p
            for p in candidates
            if (raw := getattr(p, attr)) is not None and abs(float(raw) - float(quote.odds)) < 0.51
        ]
        if matched:
            candidates = matched
    return candidates[0]


def _translate_sportsbook_grid(
    grid: list[BookQuote],
    *,
    books: dict[str, Any],
    row: InformationalRow,
    target: float | None,
    cfg: FairValueConfig | None = None,
) -> list[BookQuote]:
    """Walk off-strike sportsbook juice to the play line. Venues stay raw asks."""
    if target is None:
        return grid
    cfg = cfg or FairValueConfig.load()
    points, threshold = _quote_points(row.stat, books, cfg)
    if not points:
        return grid
    fit = _fit_for_title_strike(
        points,
        stat=row.stat,
        target=target,
        threshold=threshold,
        cfg=cfg,
    )
    if fit is None:
        return grid
    by_name: dict[str, list[BookPoint]] = {}
    for book_point in points:
        by_name.setdefault(canonical_book(book_point.book), []).append(book_point)
    side = (row.side or "").strip().lower()
    tol = cfg.same_strike_line_tolerance
    out: list[BookQuote] = []
    for quote in grid:
        if quote.book in PREDICTION_VENUES or quote.odds is None:
            out.append(quote)
            continue
        point = _match_book_point(quote, by_name.get(quote.book) or [], side=side)
        if point is None:
            out.append(quote)
            continue
        src = -point.line if threshold else point.line
        dst = -target if threshold else target
        if abs(src - dst) <= tol:
            out.append(quote)
            continue
        p_over = shift_over_prob(src, point.fair_over, dst, fit, tolerance=tol)
        p_side = (1.0 - p_over) if side in _UNDER_SIDES else p_over
        out.append(
            BookQuote(
                book=quote.book,
                odds=float(prob_to_american(p_side)),
                line=target,
            )
        )
    return out


def collect_book_quotes(
    payload: dict[str, Any],
    row: InformationalRow,
    *,
    tolerance: float = 0.26,
) -> list[BookQuote]:
    """Always return the 8-slot card grid: 6 sportsbooks + Kalshi + Polymarket."""
    del tolerance
    game = _find_game(payload, row.matchup)
    if game is None:
        return [BookQuote(book=name, odds=None) for name in CARD_BOOKS]
    blob = _market_blob(game, row)
    if blob is None:
        return [BookQuote(book=name, odds=None) for name in CARD_BOOKS]

    raw_books = blob.get("books")
    books = raw_books if isinstance(raw_books, dict) else {}
    by_book: dict[str, list[BookQuote]] = {name: [] for name in CARD_BOOKS}

    for book, entry in books.items():
        if not isinstance(entry, dict):
            continue
        quote = _quote_from_entry(str(book), entry, side=row.side)
        if quote is None:
            continue
        slot = quote.book if quote.book in by_book else None
        if slot is None:
            continue
        by_book[slot].append(quote)

    for venue in PREDICTION_VENUES:
        quote = _quote_from_venue(
            venue,
            blob.get(venue) if isinstance(blob.get(venue), dict) else None,
            side=row.side,
        )
        if quote is not None:
            by_book[venue].append(quote)

    target = None if row.stat == "moneyline" else row.book_line
    grid: list[BookQuote] = []
    for name in CARD_BOOKS:
        picked = _pick_closest(by_book[name], target)
        grid.append(picked if picked is not None else BookQuote(book=name, odds=None))
    return _translate_sportsbook_grid(
        grid,
        books=books,
        row=row,
        target=target,
    )


def build_alert(
    row: InformationalRow,
    payload: dict[str, Any],
    *,
    tolerance: float = 0.26,
) -> SportsbookEvAlert | None:
    odds = _display_odds(row)
    if odds is None:
        return None
    quotes = collect_book_quotes(payload, row, tolerance=tolerance)
    canon = canonical_book(row.book)
    filled: list[BookQuote] = []
    for quote in quotes:
        if quote.book == canon and quote.odds is None:
            filled.append(BookQuote(book=canon, odds=odds, line=row.book_line))
        else:
            filled.append(quote)
    quotes = filled
    return SportsbookEvAlert(
        market=row.market,
        matchup=row.matchup,
        player=row.player,
        stat=row.stat,
        side=row.side,
        book=row.book,
        book_line=row.book_line,
        book_odds=odds,
        book_prob=row.book_prob,
        fair_prob=row.fair_prob,
        raw_edge=row.raw_edge,
        n_books=row.n_books,
        quotes=tuple(quotes),
    )


CTA = "Follow @BretonPicks to get up to date opportunities of +EV plays!"
HASHTAGS = "#Gambling𝕏 #SportsBettingX"


def format_ev_message(alert: SportsbookEvAlert, *, promo: bool = False) -> str:
    """Play copy. Promo CTA/hashtags are X-only."""
    body = (
        f"+EV Play🚨\n\n"
        f"{alert.title}\n"
        f"Odds: {format_american(alert.book_odds)}\n"
        f"Book: {book_label(alert.book)}\n"
        f"Implied Fair Price: {format_american(float(alert.fair_american))}"
    )
    if not promo:
        return body
    return f"{body}\n\n{CTA}\n{HASHTAGS}"


def format_ev_tweet(alert: SportsbookEvAlert) -> str:
    return format_ev_message(alert, promo=True)


def _load_aggregated(path: Path) -> dict[str, Any]:
    raw = json.loads(path.read_text(encoding="utf-8"))
    return raw if isinstance(raw, dict) else {}


def post_ev_alerts(
    report: FairValueReport,
    aggregated_path: str | Path,
    *,
    dry_run: bool = False,
    min_edge_pct: float = DEFAULT_MIN_EDGE_PCT,
    min_books: int | None = None,
    cache_path: Path | None = None,
    out_dir: Path | None = None,
    config: FairValueConfig | None = None,
) -> dict[str, Any]:
    """Render +EV cards and post to Discord and X. Never raises to the caller."""
    load_env()
    cfg = config or FairValueConfig()
    books_floor = min_books if min_books is not None else cfg.min_books_hard_floor
    try:
        payload = _load_aggregated(Path(aggregated_path))
        winners = select_sportsbook_alerts(
            list(report.informational),
            min_edge_pct=min_edge_pct,
            min_books=books_floor,
        )
        alerts: list[SportsbookEvAlert] = []
        for row in winners:
            alert = build_alert(row, payload, tolerance=cfg.same_strike_line_tolerance)
            if alert is not None:
                alerts.append(alert)
        cache = SentCache(cache_path or DEFAULT_CACHE)
        day = pacific_today_iso()
        fresh = [a for a in alerts if a.alert_key(day=day) not in cache.keys]
        skipped = len(alerts) - len(fresh)
        if not fresh:
            print(f"EV alerts: nothing new to post (candidates={len(alerts)}, skipped={skipped}).")
            return {"posted": 0, "skipped": skipped, "reason": "none"}

        from ev_trading.fair_value.alert_card import render_alert_card
        from ev_trading.fair_value.discord_ev_alerts import post_ev_discord
        from polymaker.x_client import credentials_ready, post_tweet

        card_dir = out_dir or DEFAULT_CARD_DIR
        card_dir.mkdir(parents=True, exist_ok=True)
        tweet_ok = x_ev_posts_enabled() and (dry_run or credentials_ready())
        posted = 0
        for alert in fresh:
            key = alert.alert_key(day=day)
            png = card_dir / f"{key.replace('|', '_').replace(' ', '_')}.png"
            try:
                render_alert_card(alert, png)
            except Exception as exc:  # noqa: BLE001
                print(f"EV alert graphic failed ({alert.title}): {exc}", flush=True)
                continue

            discord = post_ev_discord(alert, png, dry_run=dry_run)
            tweeted = False
            if tweet_ok:
                try:
                    result = post_tweet(
                        format_ev_tweet(alert),
                        media_paths=[png],
                        dry_run=dry_run,
                        allow_text_fallback=False,
                    )
                    tweeted = True
                    print(f"EV alert tweeted: {result.url}")
                except Exception as exc:  # noqa: BLE001
                    print(f"EV alert tweet failed ({alert.title}): {exc}", flush=True)
            elif not dry_run:
                print("EV alerts: X skipped (X_EV_POSTS off or missing credentials).")

            if dry_run or discord.get("posted") or tweeted:
                cache.keys.add(key)
                posted += 1
            print(
                f"EV alert {alert.title} @ {book_label(alert.book)} "
                f"{format_american(alert.book_odds)} edge={alert.edge_pct:+.2f}pp "
                f"discord={discord.get('reason', 'ok')} x={'ok' if tweeted else 'skip'}"
            )
        cache.save()
        return {"posted": posted, "skipped": skipped, "reason": "ok"}
    except Exception as exc:  # noqa: BLE001
        print(f"EV alerts: {type(exc).__name__}: {exc}", flush=True)
        return {"posted": 0, "skipped": 0, "reason": "error", "error": str(exc)}
