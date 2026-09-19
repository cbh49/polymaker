"""Sportsbook +EV alerts: both-side informational rows, titles, and selection."""

from __future__ import annotations

from pathlib import Path

from ev_trading.fair_value.config import FairValueConfig
from ev_trading.fair_value.devig import american_to_prob, prob_to_american
from ev_trading.fair_value.discord_ev_alerts import build_embed
from ev_trading.fair_value.ev_alerts import (
    BookQuote,
    SportsbookEvAlert,
    format_american,
    format_bet_title,
    format_ev_tweet,
    select_sportsbook_alerts,
)
from ev_trading.fair_value.models import BookPoint, InformationalRow
from ev_trading.fair_value.pipeline import _info_rows, process_slate


def _ou_book(odds_over: int, odds_under: int, line: float = 29.5) -> dict[str, float | int]:
    return {
        "line": line,
        "over_odds": odds_over,
        "under_odds": odds_under,
        "over_implied_prob": american_to_prob(odds_over),
        "under_implied_prob": american_to_prob(odds_under),
    }


def test_prob_to_american_round_trip() -> None:
    assert prob_to_american(0.5) == -100
    assert format_american(-110) == "-110"
    assert format_american(120) == "+120"
    assert abs(american_to_prob(-110) - 0.5238) < 0.001


def test_format_bet_title_player_over() -> None:
    title = format_bet_title(
        player="Kaelon Black",
        stat="rushing_yards",
        side="over",
        line=29.5,
        matchup="PIT @ NE",
    )
    assert title == "Kaelon Black 30+ Rush Yards"


def test_format_bet_title_under_spread_ml() -> None:
    under = format_bet_title(
        player="Kaelon Black",
        stat="rushing_yards",
        side="under",
        line=29.5,
        matchup="PIT @ NE",
    )
    assert under == "Kaelon Black Under 29.5 Rush Yards"
    spread = format_bet_title(
        player=None,
        stat="spread",
        side="home",
        line=-3.5,
        matchup="PIT @ NE",
    )
    assert spread == "NE -3.5"
    away_spread = format_bet_title(
        player=None,
        stat="spread",
        side="away",
        line=-3.5,
        matchup="PIT @ NE",
    )
    assert away_spread == "PIT +3.5"
    ml = format_bet_title(
        player=None,
        stat="moneyline",
        side="away",
        line=None,
        matchup="PIT @ NE",
    )
    assert ml == "PIT ML"


def test_info_rows_emits_over_and_under() -> None:
    points = [
        BookPoint(
            book="draftkings",
            line=29.5,
            fair_over=0.55,
            weight=1.0,
            raw_over=0.52,
            raw_under=0.52,
            over_odds=-110,
            under_odds=-110,
        )
    ]
    rows = _info_rows(
        points,
        market="Kaelon Black rushing_yards",
        matchup="PIT @ NE",
        stat="rushing_yards",
        player="Kaelon Black",
        fair_at=0.58,
        yes_side="over",
        no_side="under",
    )
    sides = {r.side: r for r in rows}
    assert set(sides) == {"over", "under"}
    assert abs(sides["over"].raw_edge - 0.06) < 1e-9
    assert abs(sides["under"].raw_edge - (0.42 - 0.52)) < 1e-9
    assert sides["over"].book_odds == -110
    assert sides["under"].book_odds == -110


def test_select_best_book_per_market() -> None:
    rows = [
        InformationalRow(
            market="Kaelon Black rushing_yards",
            matchup="PIT @ NE",
            player="Kaelon Black",
            stat="rushing_yards",
            book="draftkings",
            book_line=29.5,
            book_prob=0.50,
            fair_prob=0.58,
            raw_edge=0.08,
            n_books=4,
            side="over",
            book_odds=-100,
        ),
        InformationalRow(
            market="Kaelon Black rushing_yards",
            matchup="PIT @ NE",
            player="Kaelon Black",
            stat="rushing_yards",
            book="mgm",
            book_line=29.5,
            book_prob=0.48,
            fair_prob=0.58,
            raw_edge=0.10,
            n_books=4,
            side="over",
            book_odds=108,
        ),
        InformationalRow(
            market="Kaelon Black rushing_yards",
            matchup="PIT @ NE",
            player="Kaelon Black",
            stat="rushing_yards",
            book="fanduel",
            book_line=29.5,
            book_prob=0.55,
            fair_prob=0.58,
            raw_edge=0.03,
            n_books=4,
            side="over",
            book_odds=-122,
        ),
    ]
    picked = select_sportsbook_alerts(rows, min_edge_pct=5.0, min_books=3)
    assert len(picked) == 1
    assert picked[0].book == "mgm"
    assert picked[0].raw_edge == 0.10


def test_process_slate_both_sides_and_alert_threshold() -> None:
    payload = {
        "games": [
            {
                "matchup": "PIT @ NE",
                "markets": {},
                "player_props": [
                    {
                        "player": "Kaelon Black",
                        "type": "rushing_yards",
                        "books": {
                            "draftkings": _ou_book(-110, -110),
                            "fanduel": _ou_book(-110, -110),
                            "mgm": _ou_book(150, -180),
                            "hardrock": _ou_book(-110, -110),
                        },
                    }
                ],
            }
        ]
    }
    report = process_slate(payload, FairValueConfig(min_edge_pct=1.0))
    sides = {(r.book, r.side) for r in report.informational}
    assert ("mgm", "over") in sides
    assert ("mgm", "under") in sides
    picked = select_sportsbook_alerts(report.informational, min_edge_pct=5.0, min_books=3)
    assert picked
    assert picked[0].book == "mgm"
    assert picked[0].side == "over"
    assert picked[0].raw_edge >= 0.05


def test_discord_embed_and_tweet_copy() -> None:
    alert = SportsbookEvAlert(
        market="Kaelon Black rushing_yards",
        matchup="PIT @ NE",
        player="Kaelon Black",
        stat="rushing_yards",
        side="over",
        book="hardrock",
        book_line=29.5,
        book_odds=100,
        book_prob=0.5,
        fair_prob=0.58,
        raw_edge=0.08,
        n_books=6,
        quotes=(BookQuote(book="hardrock", odds=100, line=29.5),),
    )
    embed = build_embed(alert)
    assert embed["title"] == "+EV Play🚨"
    assert embed["image"]["url"] == "attachment://ev_play.png"
    names = {f["name"] for f in embed["fields"]}
    assert {"PLAY", "Odds", "Book", "Implied Fair Price"} <= names
    tweet = format_ev_tweet(alert)
    assert tweet.startswith("+EV Play🚨")
    assert "Kaelon Black 30+ Rush Yards" in tweet
    assert "Odds: +100" in tweet
    assert "Book: Hard Rock" in tweet
    assert "Implied Fair Price: -138" in tweet
    assert "Follow @BretonPicks" in tweet
    assert "#Gambling𝕏 #SportsBettingX" in tweet
    assert len(tweet) <= 280


def test_render_alert_card(tmp_path: Path) -> None:
    from ev_trading.fair_value.alert_card import render_alert_card

    alert = SportsbookEvAlert(
        market="Kaelon Black rushing_yards",
        matchup="PIT @ NE",
        player="Kaelon Black",
        stat="rushing_yards",
        side="over",
        book="hardrock",
        book_line=29.5,
        book_odds=100,
        book_prob=0.5,
        fair_prob=0.58,
        raw_edge=0.08,
        n_books=4,
        quotes=(
            BookQuote(book="draftkings", odds=-110, line=29.5),
            BookQuote(book="fanduel", odds=-115, line=29.5),
            BookQuote(book="mgm", odds=-105, line=29.5),
            BookQuote(book="hardrock", odds=100, line=29.5),
        ),
    )
    out = tmp_path / "ev.png"
    render_alert_card(alert, out, cache_dir=tmp_path / "logos")
    assert out.is_file()
    assert out.stat().st_size > 1000
