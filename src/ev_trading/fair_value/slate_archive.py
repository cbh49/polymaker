"""Publish each day's scored NFL rows without rewriting the first quote.

Tradable venue sides and informational book rows go to
``nfl_ev_slate:YYYY-MM-DD``. A later refresh keeps the earlier
``books_quoted_at`` for an alert key that is already stored.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any
from zoneinfo import ZoneInfo

from ev_trading.fair_value.report import FairValueReport

EASTERN = ZoneInfo("America/New_York")
SLATE_PREFIX = "nfl_ev_slate"

VENUE_FIELDS = (
    "market",
    "matchup",
    "player",
    "stat",
    "venue",
    "side",
    "fair_prob",
    "fair_line",
    "market_line",
    "market_price",
    "raw_edge",
    "fee_adjusted_edge",
    "edge_pct",
    "confidence",
    "rank_score",
    "n_books",
    "sigma_source",
    "role_bucket",
    "fit_r2",
    "line_delta",
    "is_extrapolation",
    "extrapolation_distance",
    "volume",
    "liquidity",
    "low_confidence",
    "market_id",
    "ticker",
    "kickoff",
    "books_quoted_at",
)


def slate_report_type(day: str) -> str:
    return f"{SLATE_PREFIX}:{day}"


def eastern_day(value: str | None) -> str | None:
    text = str(value or "").strip()
    if not text:
        return None
    if len(text) == 10 and text[4] == "-" and text[7] == "-":
        return text
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed.astimezone(EASTERN).date().isoformat()


def venue_alert_key(row: dict[str, Any]) -> str | None:
    venue = str(row.get("venue") or "").strip()
    market = str(row.get("ticker") or row.get("market_id") or "").strip()
    side = str(row.get("side") or "").strip().lower()
    if not venue or not market or not side:
        return None
    return f"nfl-ev|{venue}|{market}|{side}"


def book_alert_key(row: dict[str, Any]) -> str:
    line = row.get("book_line")
    line_text = "" if line is None else str(line)
    return "|".join(
        [
            "nfl-ev",
            "book",
            str(row.get("matchup") or ""),
            str(row.get("player") or ""),
            str(row.get("stat") or ""),
            str(row.get("book") or ""),
            str(row.get("side") or ""),
            line_text,
        ]
    )


def _copy_fields(row: dict[str, Any], fields: tuple[str, ...]) -> dict[str, Any]:
    copied: dict[str, Any] = {}
    for key in fields:
        value = row.get(key)
        if value is not None:
            copied[key] = value
    return copied


def _book_side(row: dict[str, Any]) -> str:
    stat = str(row.get("stat") or "").strip().lower()
    edge = row.get("raw_edge")
    positive = not isinstance(edge, (int, float)) or edge >= 0
    if stat in {"spread", "moneyline", "ml"}:
        return "home" if positive else "away"
    return "over" if positive else "under"


def rows_from_report(report: FairValueReport) -> list[dict[str, Any]]:
    """Venue contracts plus book rows, each tagged with a population."""
    payload = report.to_dict()
    kickoff_by_matchup: dict[str, str] = {}
    rows: list[dict[str, Any]] = []
    venue_lists = (
        payload.get("tradable"),
        payload.get("tradable_low_liquidity"),
        payload.get("tradable_td"),
        payload.get("tradable_td_low_liquidity"),
    )
    for bucket in venue_lists:
        if not isinstance(bucket, list):
            continue
        for raw in bucket:
            if not isinstance(raw, dict):
                continue
            key = venue_alert_key(raw)
            if not key:
                continue
            matchup = str(raw.get("matchup") or "")
            kickoff = raw.get("kickoff")
            if matchup and isinstance(kickoff, str) and kickoff:
                kickoff_by_matchup.setdefault(matchup, kickoff)
            row = _copy_fields(raw, VENUE_FIELDS)
            row["alertKey"] = key
            row["population"] = "venue"
            rows.append(row)

    for raw in payload.get("informational") or []:
        if not isinstance(raw, dict):
            continue
        matchup = str(raw.get("matchup") or "")
        kickoff = kickoff_by_matchup.get(matchup)
        side = _book_side(raw)
        line = raw.get("book_line")
        if side == "away" and isinstance(line, (int, float)) and not isinstance(line, bool):
            line = -float(line)
        stored = {
            "alertKey": book_alert_key({**raw, "side": side}),
            "population": "book",
            "market": raw.get("market"),
            "matchup": matchup or None,
            "player": raw.get("player"),
            "stat": raw.get("stat"),
            "book": raw.get("book"),
            "side": side,
            "book_line": line,
            "market_line": line,
            "market_price": raw.get("book_prob"),
            "fair_prob": raw.get("fair_prob"),
            "raw_edge": raw.get("raw_edge"),
            "edge_pct": raw.get("edge_pct"),
            "n_books": raw.get("n_books"),
            "kickoff": kickoff,
            "books_quoted_at": report.generated_at or None,
        }
        rows.append({key: value for key, value in stored.items() if value is not None})
    return rows


def keep_earlier(prior: dict[str, Any], incoming: dict[str, Any]) -> dict[str, Any]:
    """Keep the row whose book quote is earlier. A missing time stays put."""
    prior_at = str(prior.get("books_quoted_at") or "")
    incoming_at = str(incoming.get("books_quoted_at") or "")
    if prior_at and incoming_at and incoming_at < prior_at:
        return incoming
    return prior


def merge_slate_rows(
    existing: list[dict[str, Any]], incoming: list[dict[str, Any]]
) -> list[dict[str, Any]]:
    by_key: dict[str, dict[str, Any]] = {}
    for row in existing:
        key = str(row.get("alertKey") or "")
        if key:
            by_key[key] = row
    for row in incoming:
        key = str(row.get("alertKey") or "")
        if not key:
            continue
        prior = by_key.get(key)
        by_key[key] = row if prior is None else keep_earlier(prior, row)
    return list(by_key.values())


def group_by_slate_day(
    rows: list[dict[str, Any]], *, fallback_day: str | None
) -> dict[str, list[dict[str, Any]]]:
    grouped: dict[str, list[dict[str, Any]]] = {}
    for row in rows:
        day = eastern_day(row.get("kickoff") if isinstance(row.get("kickoff"), str) else None)
        if day is None:
            day = eastern_day(
                row.get("books_quoted_at") if isinstance(row.get("books_quoted_at"), str) else None
            )
        if day is None:
            day = fallback_day
        if not day:
            continue
        grouped.setdefault(day, []).append(row)
    return grouped


def _existing_rows(payload: dict[str, Any] | None) -> list[dict[str, Any]]:
    if not isinstance(payload, dict):
        return []
    body = payload.get("payload")
    source = body if isinstance(body, dict) else payload
    rows = source.get("rows") if isinstance(source, dict) else None
    if not isinstance(rows, list):
        return []
    return [row for row in rows if isinstance(row, dict)]


def publish_slate(report: FairValueReport) -> int:
    """Upsert one report per Eastern slate day. Returns how many days were posted."""
    from polymaker.trading.convex_trades import ConvexTradeClient

    client = ConvexTradeClient()
    if not client.configured:
        return 0
    fallback = eastern_day(report.generated_at)
    grouped = group_by_slate_day(rows_from_report(report), fallback_day=fallback)
    posted = 0
    for day, incoming in sorted(grouped.items()):
        report_type = slate_report_type(day)
        merged = merge_slate_rows(_existing_rows(client.get_report(report_type)), incoming)
        client.publish_snapshot(
            prediction_date=day,
            report_type=report_type,
            payload={"date": day, "rows": merged},
        )
        posted += 1
    return posted
