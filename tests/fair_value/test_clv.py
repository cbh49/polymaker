"""Closing-line value uses the last pre-kickoff book scrape."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from ev_trading.fair_value.clv import closing_row, record_closing_lines
from ev_trading.fair_value.config import FairValueConfig
from ev_trading.fair_value.models import PricedOpportunity


def _opp(*, books_quoted_at: str, fair: float = 0.48) -> PricedOpportunity:
    return PricedOpportunity(
        market="DAL @ NYG ML home",
        matchup="DAL @ NYG",
        player=None,
        stat="moneyline",
        venue="kalshi",
        side="yes",
        fair_prob=fair,
        fair_line=None,
        market_line=None,
        market_price=0.40,
        raw_edge=fair - 0.40,
        fee_adjusted_edge=fair - 0.40,
        expected_value_per_contract=fair - 0.40,
        confidence=0.9,
        volume=1000.0,
        liquidity=None,
        volume_24hr=None,
        line_delta=None,
        low_liquidity=False,
        low_confidence=False,
        n_books=6,
        fit_r2=None,
        market_id="T",
        ticker="T",
        books_quoted_at=books_quoted_at,
        kickoff="2026-10-12T17:00:00+00:00",
    )


def _fill() -> dict:
    return {
        "trade_key": "nfl-ev|kalshi|T|yes",
        "venue": "kalshi",
        "ticker": "T",
        "side": "yes",
        "fair_side": "yes",
        "buy_price": 0.40,
        "kickoff": "2026-10-12T17:00:00+00:00",
    }


def test_clv_is_close_fair_minus_fill_price() -> None:
    row = closing_row(
        _fill(),
        _opp(books_quoted_at="2026-10-12T16:50:00+00:00"),
        window_minutes=15,
    )
    assert row is not None
    assert row["fill_price"] == pytest.approx(0.40)
    assert row["close_fair_prob"] == pytest.approx(0.48)
    assert row["clv"] == pytest.approx(0.08)


def test_post_kickoff_books_are_not_a_close() -> None:
    row = closing_row(
        _fill(),
        _opp(books_quoted_at="2026-10-12T17:05:00+00:00"),
        window_minutes=15,
    )
    assert row is None


def test_record_closing_lines_appends_and_skips_after_kickoff(tmp_path: Path) -> None:
    kickoff = "2026-10-12T17:00:00+00:00"
    books = "2026-10-12T16:50:00+00:00"
    slate = {
        "games": [
            {
                "matchup": "DAL @ NYG",
                "kickoff": kickoff,
                "books_quoted_at": books,
                "kalshi_quoted_at": books,
                "markets": {
                    "moneyline": {
                        "books": {
                            "draftkings": {
                                "home": {"odds": 0.50},
                                "away": {"odds": 0.5416667},
                            },
                            "fanduel": {
                                "home": {"odds": 0.50},
                                "away": {"odds": 0.5416667},
                            },
                            "caesars": {
                                "home": {"odds": 0.50},
                                "away": {"odds": 0.5416667},
                            },
                            "betr": {
                                "home": {"odds": 0.50},
                                "away": {"odds": 0.5416667},
                            },
                        },
                        "kalshi": {
                            "home": {
                                "ticker": "T",
                                "yes_ask": 0.40,
                                "yes_bid": 0.39,
                                "volume": 1000,
                            }
                        },
                    }
                },
                "player_props": [],
            }
        ]
    }
    fills = tmp_path / "fills.jsonl"
    fills.write_text(json.dumps(_fill()) + "\n", encoding="utf-8")
    out = tmp_path / "clv.jsonl"
    n = record_closing_lines(
        slate,
        cfg=FairValueConfig(),
        fill_paths=[fills],
        out_path=out,
    )
    assert n == 1
    row = json.loads(out.read_text(encoding="utf-8"))
    assert row["fill_price"] == 0.40
    assert abs(row["close_fair_prob"] - 0.48) < 1e-4
    assert abs(row["clv"] - 0.08) < 1e-4

    late = {
        "games": [
            {
                **slate["games"][0],
                "books_quoted_at": "2026-10-12T17:05:00+00:00",
                "kalshi_quoted_at": "2026-10-12T17:05:00+00:00",
            }
        ]
    }
    assert record_closing_lines(late, cfg=FairValueConfig(), fill_paths=[fills], out_path=out) == 0
