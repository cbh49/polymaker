"""Whale tweet formatter: amount, play, price, league hashtags, 280 cap."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

_FINDER = Path(__file__).resolve().parents[1] / "poly-sharp-finder"
if str(_FINDER) not in sys.path:
    sys.path.insert(0, str(_FINDER))

from detector import Signal  # noqa: E402
from registry import WatchedMarket  # noqa: E402
from whale_tweets import (  # noqa: E402
    TWEET_CHAR_LIMIT,
    format_whale_tweet,
    maybe_post_whale,
    team_nickname,
)


def _market(**overrides: object) -> WatchedMarket:
    row = dict(
        condition_id="0xabc",
        league="MLB",
        label="CHC vs STL ML",
        yes_token_id="yes",
        no_token_id="no",
        yes_outcome="Chicago Cubs",
        no_outcome="St. Louis Cardinals",
        slug="mlb-chc-stl",
    )
    row.update(overrides)
    return WatchedMarket(**row)  # type: ignore[arg-type]


def _sig(**overrides: object) -> Signal:
    row: dict = dict(
        ts=1.0,
        condition_id="0xabc",
        league="MLB",
        label="CHC vs STL ML",
        signal_type="whale_trade",
        side="yes",
        detail={"size_usd": 60_000, "price": 0.62},
    )
    row.update(overrides)
    return Signal(**row)


def test_team_nickname_last_word() -> None:
    assert team_nickname("Chicago Cubs") == "Cubs"
    assert team_nickname("New York Yankees") == "Yankees"


def test_team_nickname_two_word() -> None:
    assert team_nickname("Boston Red Sox") == "Red Sox"
    assert team_nickname("Toronto Blue Jays") == "Blue Jays"


def test_format_price_as_cents() -> None:
    from whale_tweets import format_price

    assert format_price(0.46) == "46¢"
    assert format_price(0.4) == "40¢"
    assert format_price(0.999) == "100¢"


def test_format_whale_tweet_mlb_cubs() -> None:
    text = format_whale_tweet(_sig(), _market())
    assert "🐋 WHALE MLB PLAY" in text
    assert "A Polymarket trader just placed" in text
    assert "$60,000" in text
    assert "Cubs ML" in text
    assert "at 62¢" in text
    assert "#MLB" in text
    assert "#Polymarket" in text
    assert "#Gambling𝕏" in text
    assert "#SportsBettingX" in text
    assert len(text) <= TWEET_CHAR_LIMIT


def test_format_whale_tweet_red_sox_no_side() -> None:
    market = _market(yes_outcome="New York Yankees", no_outcome="Boston Red Sox")
    sig = _sig(side="no", detail={"size_usd": 125_500.4, "price": 0.4})
    text = format_whale_tweet(sig, market)
    assert "Red Sox ML" in text
    assert "$125,500" in text
    assert "at 40¢" in text
    assert len(text) <= TWEET_CHAR_LIMIT


def test_format_whale_tweet_wnba_hashtags() -> None:
    market = _market(
        league="WNBA",
        label="NYL vs LVA ML",
        yes_outcome="New York Liberty",
        no_outcome="Las Vegas Aces",
    )
    sig = _sig(league="WNBA", label="NYL vs LVA ML", detail={"size_usd": 50_000, "price": 0.55})
    text = format_whale_tweet(sig, market)
    assert "🐋 WHALE WNBA PLAY" in text
    assert "Liberty ML" in text
    assert "#WNBA" in text
    assert "#MLB" not in text
    assert len(text) <= TWEET_CHAR_LIMIT


def test_format_whale_tweet_nfl_hashtags() -> None:
    market = _market(
        league="NFL",
        label="DAL vs NYG ML",
        yes_outcome="New York Giants",
        no_outcome="Dallas Cowboys",
        slug="nfl-dal-nyg-2026-09-20",
    )
    sig = _sig(
        league="NFL",
        label="DAL vs NYG ML",
        side="no",
        detail={"size_usd": 85_000, "price": 0.58},
    )
    text = format_whale_tweet(sig, market)
    assert "🐋 WHALE NFL PLAY" in text
    assert "Cowboys ML" in text
    assert "$85,000" in text
    assert "at 58¢" in text
    assert "#NFL" in text
    assert "#MLB" not in text
    assert "#Polymarket" in text


def test_format_whale_tweet_nfl_spread() -> None:
    market = _market(
        league="NFL",
        label="49ers -12.5",
        yes_outcome="San Francisco 49ers",
        no_outcome="Miami Dolphins",
        slug="nfl-mia-sf-2026-09-20-spread-home-12pt5",
    )
    text = format_whale_tweet(
        _sig(league="NFL", label="49ers -12.5", detail={"size_usd": 75_000, "price": 0.51}),
        market,
    )
    assert "🐋 WHALE NFL PLAY" in text
    assert "49ers -12.5" in text
    assert "49ers ML" not in text
    assert "$75,000" in text
    assert "#NFL" in text
    assert len(text) <= TWEET_CHAR_LIMIT


def test_format_whale_tweet_nfl_total_under() -> None:
    market = _market(
        league="NFL",
        label="O/U 44.5",
        yes_outcome="Over",
        no_outcome="Under",
        slug="nfl-mia-sf-2026-09-20-total-44pt5",
    )
    text = format_whale_tweet(
        _sig(league="NFL", side="no", label="O/U 44.5", detail={"size_usd": 90_000, "price": 0.49}),
        market,
    )
    assert "Miami Dolphins vs San Francisco 49ers Under 44.5" in text
    assert "Over 44.5" not in text
    assert "#NFL" in text


def test_format_whale_tweet_mlb_total_includes_matchup() -> None:
    market = _market(
        label="O/U 7.5",
        yes_outcome="Over",
        no_outcome="Under",
        slug="mlb-nym-nyy-2026-09-24-total-7pt5",
    )
    text = format_whale_tweet(
        _sig(side="no", label="O/U 7.5", detail={"size_usd": 52_800, "price": 0.48}),
        market,
    )
    assert "New York Mets vs New York Yankees Under 7.5" in text
    assert "at 48¢" in text


def test_maybe_post_skips_when_flag_off(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("X_WHALE_POSTS", raising=False)
    called: list[str] = []

    def poster(text: str, *, dry_run: bool = False) -> object:
        called.append(text)
        return type("R", (), {"url": "https://x.com/i/web/status/1"})()

    result = maybe_post_whale(_sig(), _market(), poster=poster)
    assert result is not None
    assert result["action"] == "skipped"
    assert called == []


def test_maybe_post_skips_non_whale(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("X_WHALE_POSTS", "1")
    sig = _sig(signal_type="convergence")
    assert maybe_post_whale(sig, _market(), poster=lambda *a, **k: None) is None


def test_maybe_post_when_enabled(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("X_WHALE_POSTS", "1")
    posted: list[str] = []

    def poster(text: str, *, dry_run: bool = False) -> object:
        posted.append(text)
        return type("R", (), {"url": "https://x.com/i/web/status/99"})()

    result = maybe_post_whale(_sig(), _market(), poster=poster)
    assert result is not None
    assert result["action"] == "posted"
    assert result["url"] == "https://x.com/i/web/status/99"
    assert len(posted) == 1
    assert "Cubs ML" in posted[0]
    assert "$60,000" in posted[0]
