"""Slate archive keeps the earliest quote and tags venue vs book rows."""

from __future__ import annotations

from ev_trading.fair_value.models import InformationalRow, PricedOpportunity
from ev_trading.fair_value.report import FairValueReport
from ev_trading.fair_value.slate_archive import (
    keep_earlier,
    merge_slate_rows,
    rows_from_report,
)


def _opp(**overrides: object) -> PricedOpportunity:
    base = dict(
        market="DEN @ LAC receiving yards",
        matchup="DEN @ LAC",
        player="Evan Engram",
        stat="receiving_yards",
        venue="kalshi",
        side="over",
        fair_prob=0.62,
        fair_line=14.5,
        market_line=15.0,
        market_price=0.55,
        raw_edge=0.07,
        fee_adjusted_edge=0.06,
        expected_value_per_contract=0.06,
        confidence=0.85,
        volume=1000.0,
        liquidity=None,
        volume_24hr=None,
        line_delta=0.5,
        low_liquidity=False,
        low_confidence=False,
        n_books=5,
        fit_r2=0.9,
        ticker="KXNFLRECYDS-26OCT11DENLAC-DENEENGRAM1-15",
        kickoff="2026-10-11T17:00:00+00:00",
        books_quoted_at="2026-10-11T15:00:00+00:00",
    )
    base.update(overrides)
    return PricedOpportunity(**base)


def test_rows_from_report_tags_venue_and_book() -> None:
    report = FairValueReport(
        tradable=[_opp()],
        informational=[
            InformationalRow(
                market="DEN @ LAC spread",
                matchup="DEN @ LAC",
                player=None,
                stat="spread",
                book="circa",
                book_line=-3.5,
                book_prob=0.48,
                fair_prob=0.52,
                raw_edge=0.04,
                n_books=4,
            )
        ],
        generated_at="2026-10-11T16:00:00+00:00",
    )
    rows = rows_from_report(report)
    venue = next(row for row in rows if row["population"] == "venue")
    book = next(row for row in rows if row["population"] == "book")
    assert venue["alertKey"] == "nfl-ev|kalshi|KXNFLRECYDS-26OCT11DENLAC-DENEENGRAM1-15|over"
    assert venue["edge_pct"] == 6.0
    assert book["side"] == "home"
    assert book["kickoff"] == "2026-10-11T17:00:00+00:00"


def test_merge_keeps_the_earlier_quote() -> None:
    early = {"alertKey": "k", "books_quoted_at": "2026-10-11T15:00:00+00:00", "edge_pct": 4}
    late = {"alertKey": "k", "books_quoted_at": "2026-10-11T16:00:00+00:00", "edge_pct": 9}
    assert keep_earlier(early, late)["edge_pct"] == 4
    assert merge_slate_rows([late], [early])[0]["edge_pct"] == 4
    assert merge_slate_rows([early], [late])[0]["edge_pct"] == 4
