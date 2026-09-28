"""Sharp-money X tweets: A+ only, card image, no daily cap."""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

_AGG = Path(__file__).resolve().parents[1] / "data-aggregation"
if str(_AGG) not in sys.path:
    sys.path.insert(0, str(_AGG))

from sharp_card import card_copy, render_sharp_card  # noqa: E402
from sharp_tweets import (  # noqa: E402
    SHARP_URL,
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
        "game_time_utc": "2099-09-12T16:00:00.000Z",
        "game_time_local": "9/12, 12:00PM",
        "date": "2099-09-12",
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
        "tier": "A+",
        "composite_gap": 101.25,
        "model_confidence": 90,
        "open": 4.5,
        "live": 3.0,
        "open_odds": -115.0,
        "live_odds": -108.0,
        "line_move": -1.5,
    }
    row.update(overrides)
    return row


def _output(*plays: dict, league: str = "NCAAF") -> dict:
    return {"league": league, "plays": list(plays)}


def _poster_bucket() -> tuple[list[str], list[list[Path] | None], object]:
    posted: list[str] = []
    media: list[list[Path] | None] = []

    def poster(text: str, *, dry_run: bool = False, media_paths: list[Path] | None = None) -> object:
        del dry_run
        posted.append(text)
        media.append(list(media_paths) if media_paths else None)
        return type("R", (), {"url": "https://x.com/i/web/status/1"})()

    return posted, media, poster


def test_tweet_text_and_limit() -> None:
    text = format_sharp_tweet(_play(), league="NCAAF")
    assert text.startswith("🚨 Sharp Money Play")
    assert "Wake Forest @ Purdue — Purdue +3" in text
    assert "Odds: -108" in text
    assert SHARP_URL in text
    assert text.splitlines()[-1] == "#Gambling𝕏 #SportsBettingX"
    assert "A+" not in text
    assert "SBD" not in text
    assert "DK" not in text
    assert x_weighted_len(text) <= TWEET_CHAR_LIMIT


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
        ),
        league="NCAAF",
    )
    assert "APP +210" in ml
    assert "Odds: +210" in ml
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
    assert "Under 49.5" in tot
    assert "Odds: -105" in tot
    assert x_weighted_len(ml) <= TWEET_CHAR_LIMIT
    assert x_weighted_len(tot) <= TWEET_CHAR_LIMIT


def test_collect_only_a_plus() -> None:
    rows = collect_alert_plays(
        [
            _output(_play(tier="B", event_id="b", composite_gap=900)),
            _output(_play(tier="A", event_id="a", composite_gap=400, side="A1")),
            _output(
                _play(tier="A+", event_id="ap", composite_gap=50, side="AP"),
                league="MLB",
            ),
            _output(_play(tier="A+", event_id="ap2", composite_gap=80, side="AP2")),
        ]
    )
    assert [p["event_id"] for _, p in rows] == ["ap2", "ap"]
    assert rows[1][0] == "MLB"


def test_posts_every_a_plus_and_skips_lower_tiers(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("X_SHARP_POSTS", "1")
    posted, media, poster = _poster_bucket()
    quota = tmp_path / "quota.json"
    cards = tmp_path / "cards"
    payload = _output(
        _play(tier="A+", event_id="1", side="S1", composite_gap=80, play_label="One +3"),
        _play(tier="A+", event_id="2", side="S2", composite_gap=40, play_label="Two +7"),
        _play(tier="A", event_id="3", side="S3", composite_gap=500, play_label="Three +1"),
    )
    first = post_sharp_tweets(
        payload,
        quota_path=quota,
        card_dir=cards,
        fetch_logos=False,
        poster=poster,
    )
    assert first["posted"] == 2
    assert len(posted) == 2
    assert "One +3" in posted[0]
    assert "Two +7" in posted[1]
    assert all(SHARP_URL in t for t in posted)
    assert all(paths and Path(paths[0]).is_file() for paths in media)

    posted.clear()
    extra = _output(_play(tier="A+", event_id="4", side="S4", play_label="Four +2"))
    second = post_sharp_tweets(
        extra,
        quota_path=quota,
        card_dir=cards,
        fetch_logos=False,
        poster=poster,
    )
    assert second["posted"] == 1
    assert "Four +2" in posted[0]

    posted.clear()
    third = post_sharp_tweets(
        payload,
        quota_path=quota,
        card_dir=cards,
        fetch_logos=False,
        poster=poster,
    )
    assert third["posted"] == 0
    assert posted == []


def test_previous_days_still_block_the_same_play(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("X_SHARP_POSTS", "1")
    quota = tmp_path / "quota.json"
    key = tweet_play_key(_play(), "NCAAF")
    quota.write_text(
        json.dumps({"date": "2026-09-12", "keys": [key]}),
        encoding="utf-8",
    )
    posted, _, poster = _poster_bucket()
    result = post_sharp_tweets(
        _output(_play()),
        quota_path=quota,
        card_dir=tmp_path / "cards",
        fetch_logos=False,
        poster=poster,
    )
    assert result["posted"] == 0
    assert posted == []


def test_a_plus_upgrade_posts_once(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("X_SHARP_POSTS", "1")
    quota = tmp_path / "quota.json"
    posted, _, poster = _poster_bucket()
    cards = tmp_path / "cards"
    skipped = post_sharp_tweets(
        _output(_play(tier="A")),
        quota_path=quota,
        card_dir=cards,
        fetch_logos=False,
        poster=poster,
    )
    assert skipped["reason"] == "no_alerts"
    assert posted == []
    again = post_sharp_tweets(
        _output(_play(tier="A+")),
        quota_path=quota,
        card_dir=cards,
        fetch_logos=False,
        poster=poster,
    )
    assert again["posted"] == 1
    posted.clear()
    repeat = post_sharp_tweets(
        _output(_play(tier="A+")),
        quota_path=quota,
        card_dir=cards,
        fetch_logos=False,
        poster=poster,
    )
    assert tweet_play_key(_play(tier="A"), "NCAAF") == tweet_play_key(_play(tier="A+"), "NCAAF")
    assert repeat["posted"] == 0
    assert posted == []


def test_skips_started_games(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("X_SHARP_POSTS", "1")
    posted, _, poster = _poster_bucket()
    result = post_sharp_tweets(
        _output(_play(game_time_utc="2020-01-01T00:00:00.000Z")),
        quota_path=tmp_path / "q.json",
        card_dir=tmp_path / "cards",
        fetch_logos=False,
        poster=poster,
    )
    assert result["reason"] == "started"
    assert result["posted"] == 0
    assert posted == []


def test_disabled_when_flags_off(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("X_SHARP_POSTS", raising=False)
    monkeypatch.delenv("X_WHALE_POSTS", raising=False)
    posted, _, poster = _poster_bucket()
    result = post_sharp_tweets(
        _output(_play()),
        quota_path=tmp_path / "q.json",
        card_dir=tmp_path / "cards",
        fetch_logos=False,
        poster=poster,
    )
    assert result["reason"] == "disabled"
    assert posted == []


def test_card_copy_matches_site_and_hides_books() -> None:
    copy = card_copy(_play(), league="NCAAF")
    blob = json.dumps(copy)
    assert copy["bet"] == "Purdue +3  -108"
    assert copy["public_name"] == "Wake Forest"
    assert copy["public_pct"] == 55
    assert copy["handle_name"] == "Purdue"
    assert copy["handle_pct"] == 93
    assert copy["steam"] == 48
    assert copy["open_text"] == "+4.5 (-115)"
    assert copy["now_text"] == "+3 (-108)"
    assert copy["move_note"] == "Moved 1.5 toward Purdue"
    assert "12:00 PM ET" in copy["starts"]
    assert "A+" not in blob
    assert "SBD" not in blob
    assert "DK" not in blob
    assert "VSiN" not in blob


def test_render_card_png(tmp_path: Path) -> None:
    dest = render_sharp_card(_play(), tmp_path / "card.png", league="NCAAF", fetch_logos=False)
    assert dest.is_file()
    assert dest.stat().st_size > 5000
