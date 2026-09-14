"""Discord A/A+ sharp-money alerts (no network)."""

from __future__ import annotations

import sys
from pathlib import Path

_AGG = Path(__file__).resolve().parents[1] / "data-aggregation"
if str(_AGG) not in sys.path:
    sys.path.insert(0, str(_AGG))

from discord_sharp_alerts import (  # noqa: E402
    alert_plays,
    build_embed,
    pct_bar,
    play_key,
    post_sharp_alerts,
    resolve_play_label,
)
from find_sharp_money import compute_model_confidence, format_play_label  # noqa: E402


def _purdue_play(**overrides) -> dict:
    play = {
        "matchup": "WAKE @ PUR",
        "matchup_display": "Wake Forest @ Purdue",
        "away_team": "Wake Forest",
        "home_team": "Purdue",
        "game_time_local": "9/12, 12:00PM",
        "date": "2026-09-12",
        "event_id": "34603628",
        "market": "spread",
        "side": "PUR",
        "home_away": "home",
        "play_label": "Purdue +3",
        "play_line": 3.0,
        "play_odds": -108.0,
        "line_move": -1.5,
        "public_bet_pct": 45,
        "handle_bet_pct": 93,
        "public_favors_bet_pct": 55,
        "vsin_public_bet_pct": 62,
        "vsin_handle_bet_pct": 92,
        "sbd_public_bet_pct": 45,
        "sbd_handle_bet_pct": 56,
        "tier": "A",
        "composite_gap": 101.25,
        "n_sources_agreeing": 3,
        "agreeing_sources": ["primary", "vsin", "sbd"],
        "rlm_confirmed": True,
        "rlm_source_used": "eva",
        "rlm_source_conflict": False,
        "public_favors": "away",
        "public_favors_name": "Wake Forest",
        "open": 4.5,
        "live": 3.0,
        "open_odds": -115.0,
        "live_odds": -108.0,
        "implied_fair_prob": 0.49567,
        "low_volume_dog_flag": None,
        "ml_spread_divergence": None,
        "polymarket_low_liquidity": False,
        "model_confidence": 90,
        "exchange_confirmation": {
            "exchange_fair_prob": 0.555,
            "exchange_edge_pct": 5.933,
            "exchange_rlm_confirmed": None,
            "polymarket_liquidity": 45850.78,
            "low_liquidity": False,
            "books_used": ["Polymarket"],
        },
    }
    play.update(overrides)
    return play


class _Resp:
    status_code = 204
    text = ""


def test_format_play_labels() -> None:
    assert format_play_label(name="Purdue", market="spread", line=3.0, odds=-108) == "Purdue +3"
    assert format_play_label(name="APP", market="moneyline", line=210) == "APP +210"
    assert format_play_label(name="Under", market="total", line=49.5, odds=-105) == "Under 49.5"


def test_purdue_embed_highlights_play_and_steam() -> None:
    embed = build_embed(_purdue_play(), league="NCAAF", config={"primary_source": "draftkings"})
    assert embed["title"].startswith("A")
    assert "Wake Forest @ Purdue" in embed["description"]
    play_field = next(f for f in embed["fields"] if f["name"] == "PLAY")
    assert "**Purdue +3**" in play_field["value"]
    assert play_field["value"].startswith(">>>")
    assert "(-108)" in play_field["value"]
    steam = next(f for f in embed["fields"] if f["name"] == "Steam")
    assert pct_bar(55) in steam["value"]
    assert pct_bar(93) in steam["value"]
    assert "55%" in steam["value"]
    assert "93%" in steam["value"]
    assert "Wake Forest" in steam["value"]
    assert "Purdue" in steam["value"]
    conf = next(f for f in embed["fields"] if f["name"] == "Confidence")
    assert "**90**" in conf["value"]
    assert "Fair 49.6%" in conf["value"]


def test_resolve_play_label_moneyline_and_total() -> None:
    ml = {
        "market": "moneyline",
        "home_away": "away",
        "away_team": "APP",
        "side": "APP",
        "play_label": "APP +210",
        "live": 210,
    }
    tot = {
        "market": "total",
        "home_away": "under",
        "side": "Under",
        "play_label": "Under 49.5",
        "live": 49.5,
    }
    assert resolve_play_label(ml) == "APP +210"
    assert resolve_play_label(tot) == "Under 49.5"
    rebuilt = dict(ml)
    rebuilt.pop("play_label")
    assert resolve_play_label(rebuilt) == "APP +210"


def test_model_confidence_range_and_penalties() -> None:
    base = _purdue_play()
    score = compute_model_confidence(base)
    assert 70 <= score <= 100
    assert compute_model_confidence(base) == score
    plus = compute_model_confidence({**base, "tier": "A+"})
    assert plus >= score
    dog = compute_model_confidence({**base, "low_volume_dog_flag": True})
    assert dog < score
    assert 70 <= dog <= 100


def test_alert_plays_filters_b() -> None:
    plays = [
        _purdue_play(tier="A"),
        _purdue_play(tier="A+", event_id="1"),
        _purdue_play(tier="B", event_id="2"),
    ]
    alerts = alert_plays(plays)
    assert [p["tier"] for p in alerts] == ["A", "A+"]


def test_dedupe_and_a_plus_upgrade(tmp_path: Path) -> None:
    cache = tmp_path / ".discord_sent.json"
    posted: list[dict] = []

    def capture(_url: str, payload: dict) -> _Resp:
        posted.append(payload)
        return _Resp()

    output = {
        "league": "NCAAF",
        "config": {"primary_source": "draftkings"},
        "plays": [_purdue_play(), _purdue_play(tier="B", event_id="b-only")],
    }
    first = post_sharp_alerts(
        output,
        webhook_url="https://discord.com/api/webhooks/1/token",
        cache_path=cache,
        post_fn=capture,
    )
    assert first["posted"] == 1
    assert first["skipped"] == 0
    assert posted and len(posted[0]["embeds"]) == 1
    assert play_key(_purdue_play(), "NCAAF") in cache.read_text(encoding="utf-8")

    posted.clear()
    second = post_sharp_alerts(
        output,
        webhook_url="https://discord.com/api/webhooks/1/token",
        cache_path=cache,
        post_fn=capture,
    )
    assert second["posted"] == 0
    assert second["skipped"] == 1
    assert posted == []

    posted.clear()
    upgraded = {
        "league": "NCAAF",
        "config": {"primary_source": "draftkings"},
        "plays": [_purdue_play(tier="A+")],
    }
    third = post_sharp_alerts(
        upgraded,
        webhook_url="https://discord.com/api/webhooks/1/token",
        cache_path=cache,
        post_fn=capture,
    )
    assert third["posted"] == 1
    assert "A+" in posted[0]["embeds"][0]["title"]
