"""Sharp-money X tweets: styling, subscribe CTA, 2-per-Pacific-day cap."""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

_AGG = Path(__file__).resolve().parents[1] / "data-aggregation"
if str(_AGG) not in sys.path:
    sys.path.insert(0, str(_AGG))

from sharp_tweets import (  # noqa: E402
    MAX_PER_DAY,
    SUBSCRIBE_CTA,
    SUBSCRIBE_URL,
    TWEET_CHAR_LIMIT,
    collect_alert_plays,
    format_sharp_tweet,
    post_sharp_tweets,
    tweet_play_key,
    x_weighted_len,
)


def _play(**overrides) -> dict:
    row = {
        "matchup": "WAKE @ PUR",
        "matchup_display": "Wake Forest @ Purdue",
        "away_team": "Wake Forest",
        "home_team": "Purdue",
        "date": "2026-09-12",
        "event_id": "34603628",
        "market": "spread",
        "side": "PUR",
        "home_away": "home",
        "play_label": "Purdue +3",
        "play_odds": -108.0,
        "public_bet_pct": 45,
        "handle_bet_pct": 93,
        "public_favors_bet_pct": 55,
        "public_favors_name": "Wake Forest",
        "tier": "A",
        "composite_gap": 101.25,
        "model_confidence": 90,
        "open": 4.5,
        "live": 3.0,
        "open_odds": -115.0,
        "live_odds": -108.0,
    }
    row.update(overrides)
    return row


def _output(*plays: dict, league: str = "NCAAF") -> dict:
    return {"league": league, "plays": list(plays)}


def test_tweet_includes_cta_and_fits_limit() -> None:
    text = format_sharp_tweet(_play(), league="NCAAF")
    assert text.startswith("💰 SHARP MONEY · NCAAF · A")
    assert "Wake Forest @ Purdue" in text
    assert "PLAY: Purdue +3 (-108)" in text
    assert "Public 55% Wake Forest" in text
    assert "Handle 93% Purdue" in text
    assert "+48pp" in text
    assert SUBSCRIBE_CTA in text
    assert SUBSCRIBE_URL in text
    assert "#NCAAF" in text
    assert "#Gambling𝕏" in text
    assert "#SportsBettingX" in text
    assert "#SharpMoney" not in text
    assert x_weighted_len(text) <= TWEET_CHAR_LIMIT
    assert text.strip().endswith(SUBSCRIBE_URL)


def test_moneyline_and_total_labels() -> None:
    ml = format_sharp_tweet(
        _play(
            market="moneyline",
            home_away="away",
            away_team="APP",
            side="APP",
            play_label="APP +210",
            play_odds=210,
            live=210,
            open=270,
            open_odds=None,
            live_odds=None,
        ),
        league="NCAAF",
    )
    assert "PLAY: APP +210" in ml
    tot = format_sharp_tweet(
        _play(
            market="total",
            home_away="under",
            side="Under",
            play_label="Under 49.5",
            play_odds=-105,
            live=49.5,
            open=50.5,
        ),
        league="NCAAF",
    )
    assert "PLAY: Under 49.5 (-105)" in tot
    assert x_weighted_len(ml) <= TWEET_CHAR_LIMIT
    assert x_weighted_len(tot) <= TWEET_CHAR_LIMIT


def test_collect_ignores_b_and_ranks_a_plus_first() -> None:
    rows = collect_alert_plays(
        [
            _output(_play(tier="B", event_id="b", composite_gap=900)),
            _output(_play(tier="A", event_id="a", composite_gap=400, side="A1")),
            _output(
                _play(tier="A+", event_id="ap", composite_gap=50, side="AP"),
                league="MLB",
            ),
        ]
    )
    assert [p["event_id"] for _, p in rows] == ["ap", "a"]
    assert rows[0][0] == "MLB"


def test_posts_only_two_best_then_caps(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("X_SHARP_POSTS", "1")
    posted: list[str] = []

    def poster(text: str, *, dry_run: bool = False) -> object:
        posted.append(text)
        return type("R", (), {"url": "https://x.com/i/web/status/1"})()

    quota = tmp_path / "quota.json"
    payload = _output(
        _play(tier="A+", event_id="1", side="S1", composite_gap=80, play_label="One +3"),
        _play(tier="A+", event_id="2", side="S2", composite_gap=40, play_label="Two +7"),
        _play(tier="A", event_id="3", side="S3", composite_gap=500, play_label="Three +1"),
    )
    first = post_sharp_tweets(
        payload,
        quota_path=quota,
        day="2026-09-13",
        poster=poster,
    )
    assert first["posted"] == MAX_PER_DAY
    assert len(posted) == 2
    assert "PLAY: One +3" in posted[0]
    assert "PLAY: Two +7" in posted[1]
    assert all(SUBSCRIBE_URL in t for t in posted)

    posted.clear()
    second = post_sharp_tweets(
        payload,
        quota_path=quota,
        day="2026-09-13",
        poster=poster,
    )
    assert second["posted"] == 0
    assert second["reason"] == "daily_cap"
    assert posted == []


def test_new_pacific_day_resets_cap(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("X_SHARP_POSTS", "1")
    quota = tmp_path / "quota.json"
    quota.write_text(
        json.dumps({"date": "2026-09-12", "keys": ["NCAAF|2026-09-12|old|spread|PUR"]}),
        encoding="utf-8",
    )
    posted: list[str] = []

    def poster(text: str, *, dry_run: bool = False) -> object:
        posted.append(text)
        return type("R", (), {"url": "u"})()

    result = post_sharp_tweets(
        _output(_play()),
        quota_path=quota,
        day="2026-09-13",
        poster=poster,
    )
    assert result["posted"] == 1
    assert len(posted) == 1


def test_same_play_not_tweeted_twice_even_if_tier_upgrades(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("X_SHARP_POSTS", "1")
    quota = tmp_path / "quota.json"
    posted: list[str] = []

    def poster(text: str, *, dry_run: bool = False) -> object:
        posted.append(text)
        return type("R", (), {"url": "u"})()

    post_sharp_tweets(_output(_play(tier="A")), quota_path=quota, day="2026-09-13", poster=poster)
    posted.clear()
    again = post_sharp_tweets(
        _output(_play(tier="A+")),
        quota_path=quota,
        day="2026-09-13",
        poster=poster,
    )
    assert tweet_play_key(_play(tier="A"), "NCAAF") == tweet_play_key(_play(tier="A+"), "NCAAF")
    assert again["posted"] == 0
    assert posted == []


def test_disabled_when_flags_off(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("X_SHARP_POSTS", raising=False)
    monkeypatch.delenv("X_WHALE_POSTS", raising=False)
    called: list[str] = []

    def poster(text: str, *, dry_run: bool = False) -> object:
        called.append(text)
        return type("R", (), {"url": "u"})()

    result = post_sharp_tweets(
        _output(_play()),
        quota_path=tmp_path / "q.json",
        day="2026-09-13",
        poster=poster,
    )
    assert result["reason"] == "disabled"
    assert called == []
