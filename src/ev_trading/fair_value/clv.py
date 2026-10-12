"""Closing-line value for NFL EV fills.

A fill does not know the close. The last book scrape still at or before kickoff,
and inside the closing window, is the close. CLV is close_fair_prob − fill_price
for the side that was bought. A book scrape after kickoff is not a close.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from ev_trading.fair_value.config import FairValueConfig
from ev_trading.fair_value.freshness import parse_utc
from ev_trading.fair_value.models import PricedOpportunity
from ev_trading.fair_value.pipeline import priced_opportunities

_BOT_ROOT = Path(__file__).resolve().parents[3]
DEFAULT_KALSHI_FILLS = _BOT_ROOT / "journal" / "nfl_kalshi_trades.jsonl"
DEFAULT_POLY_FILLS = _BOT_ROOT / "journal" / "nfl_polymarket_trades.jsonl"
DEFAULT_CLV_LOG = _BOT_ROOT / "journal" / "nfl_clv.jsonl"


def _load_jsonl(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    rows: list[dict[str, Any]] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        text = line.strip()
        if not text:
            continue
        try:
            raw = json.loads(text)
        except json.JSONDecodeError:
            continue
        if isinstance(raw, dict):
            rows.append(raw)
    return rows


def _fill_price(fill: dict[str, Any]) -> float | None:
    for key in ("buy_price", "limit_price", "ask"):
        raw = fill.get(key)
        if raw is None or raw == "":
            continue
        try:
            return float(raw)
        except (TypeError, ValueError):
            continue
    return None


def _contract_id(row: dict[str, Any]) -> str:
    return str(row.get("ticker") or row.get("market_id") or "").strip()


def _index_opps(opps: list[PricedOpportunity]) -> dict[tuple[str, str, str], PricedOpportunity]:
    out: dict[tuple[str, str, str], PricedOpportunity] = {}
    for opp in opps:
        ident = (opp.ticker or opp.market_id or "").strip()
        if not ident:
            continue
        out[(opp.venue, ident, opp.side)] = opp
    return out


def closing_row(
    fill: dict[str, Any],
    opp: PricedOpportunity,
    *,
    window_minutes: float,
) -> dict[str, Any] | None:
    """One CLV line, or None when this scrape is not a closing line."""
    price = _fill_price(fill)
    kickoff = parse_utc(fill.get("kickoff") or opp.kickoff)
    books = parse_utc(opp.books_quoted_at)
    if price is None or kickoff is None or books is None:
        return None
    if books > kickoff:
        return None
    if kickoff - books > timedelta(minutes=window_minutes):
        return None
    trade_key = str(fill.get("trade_key") or "").strip()
    if not trade_key:
        venue = str(fill.get("venue") or opp.venue)
        trade_key = f"nfl-ev|{venue}|{_contract_id(fill)}|{opp.side}"
    clv = opp.fair_prob - price
    return {
        "trade_key": trade_key,
        "venue": opp.venue,
        "side": opp.side,
        "stat": opp.stat,
        "player": opp.player,
        "matchup": opp.matchup,
        "fill_price": price,
        "close_fair_prob": opp.fair_prob,
        "close_line": opp.market_line if opp.market_line is not None else opp.fair_line,
        "clv": clv,
        "kickoff": kickoff.isoformat(),
        "books_quoted_at": books.isoformat(),
        "sigma_source": opp.sigma_source,
        "recorded_at": datetime.now(UTC).isoformat(),
    }


def _latest_book_time(rows: list[dict[str, Any]], trade_key: str) -> datetime | None:
    latest: datetime | None = None
    for row in rows:
        if str(row.get("trade_key") or "") != trade_key:
            continue
        stamped = parse_utc(row.get("books_quoted_at"))
        if stamped is None:
            continue
        if latest is None or stamped > latest:
            latest = stamped
    return latest


def record_closing_lines(
    slate: dict[str, Any] | str | Path,
    *,
    cfg: FairValueConfig | None = None,
    fill_paths: list[Path] | None = None,
    out_path: Path | None = None,
) -> int:
    """Append CLV rows for fills whose fresh book scrape is the closing line.

    A later pre-kickoff scrape appends again. The latest line at or before
    kickoff is the close. Returns how many lines were appended.
    """
    cfg = cfg or FairValueConfig()
    if isinstance(slate, (str, Path)):
        payload = json.loads(Path(slate).read_text(encoding="utf-8"))
        if not isinstance(payload, dict):
            raise ValueError(f"expected a JSON object in {slate}")
    else:
        payload = slate
    indexed = _index_opps(priced_opportunities(payload, cfg))
    paths = fill_paths if fill_paths is not None else [DEFAULT_KALSHI_FILLS, DEFAULT_POLY_FILLS]
    fills: list[dict[str, Any]] = []
    for path in paths:
        fills.extend(_load_jsonl(path))
    dest = out_path or DEFAULT_CLV_LOG
    existing = _load_jsonl(dest)
    appended: list[dict[str, Any]] = []
    for fill in fills:
        ident = _contract_id(fill)
        venue = str(fill.get("venue") or "")
        side = str(fill.get("fair_side") or fill.get("side") or "").strip().lower()
        opp = indexed.get((venue, ident, side))
        if opp is None:
            continue
        row = closing_row(fill, opp, window_minutes=cfg.clv_window_minutes)
        if row is None:
            continue
        books = parse_utc(row["books_quoted_at"])
        previous = _latest_book_time(existing + appended, row["trade_key"])
        if books is not None and previous is not None and books <= previous:
            continue
        appended.append(row)
    if not appended:
        return 0
    dest.parent.mkdir(parents=True, exist_ok=True)
    with dest.open("a", encoding="utf-8") as fh:
        for row in appended:
            fh.write(json.dumps(row) + "\n")
    return len(appended)
