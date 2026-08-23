"""Tests for research search query construction."""

from __future__ import annotations

from datetime import datetime

from polymaker.research.search import build_daily_search_query, build_search_query


def test_build_search_query_strips_at_and_adds_date() -> None:
    when = datetime(2026, 8, 8)
    q = build_search_query("Baltimore Orioles @ Texas Rangers", when=when)
    assert q == "Baltimore Orioles Texas Rangers Aug 8 best bets"
    assert "@" not in q


def test_build_search_query_collapses_whitespace() -> None:
    when = datetime(2026, 8, 8)
    q = build_search_query("Chicago Cubs  @   Kansas City Royals", when=when)
    assert "  " not in q
    assert q.endswith("Aug 8 best bets")


def test_build_daily_search_query() -> None:
    when = datetime(2026, 8, 11)
    assert build_daily_search_query(when=when) == "MLB August 11 Best Bets"
