"""Fee-aware Kalshi vs Polymarket routing for sharp-money trades."""

from __future__ import annotations

from datetime import date

from polymaker.trading.convex_trades import ClaimResult
from polymaker.trading.event_key import canonical_trade_key, canonical_trade_key_for_play
from polymaker.trading.execute import _claim_event
from polymaker.trading.kalshi_match import match_kalshi_plays
from polymaker.trading.sharp import SharpPlay
from polymaker.trading.teams import resolve_team
from polymaker.trading.venue_quote import PricedVenue, pick_venue, price_venue


def _play(**kwargs: object) -> SharpPlay:
    defaults: dict[str, object] = {
        "league": "NFL",
        "matchup": "DET @ BUF",
        "side": "BUF",
        "market": "moneyline",
        "tier": "A",
        "home_away": "home",
        "game_time_utc": "2026-09-17T00:20:00.000Z",
        "implied_fair_prob": 0.55,
        "rlm_confirmed": True,
        "composite_gap": 20.0,
        "source_path": "x",
        "raw": {"date": "2026-09-17"},
    }
    defaults.update(kwargs)
    return SharpPlay(**defaults)  # type: ignore[arg-type]


def test_kalshi_mid_price_loses_to_poly_after_fees() -> None:
    """Kalshi 49¢ + quadratic fee is worse than Poly 50¢ with ~0 fee."""
    kalshi = price_venue("kalshi", 0.49, 25.0, contracts=51)
    poly = price_venue("polymarket", 0.50, 25.0)
    assert kalshi.ask < poly.ask
    assert kalshi.all_in > poly.all_in
    chosen = pick_venue([kalshi, poly])
    assert chosen is not None
    assert chosen.venue == "polymarket"


def test_exact_all_in_tie_uses_polymarket() -> None:
    poly = PricedVenue("polymarket", ask=0.40, fee=0.01, all_in=0.41, usd=25.0)
    kalshi = PricedVenue("kalshi", ask=0.40, fee=0.01, all_in=0.41, usd=25.0)
    chosen = pick_venue([kalshi, poly])
    assert chosen is not None
    assert chosen.venue == "polymarket"


def test_near_tie_within_epsilon_uses_polymarket() -> None:
    poly = PricedVenue("polymarket", ask=0.40, fee=0.002, all_in=0.4020, usd=25.0)
    kalshi = PricedVenue("kalshi", ask=0.40, fee=0.00205, all_in=0.40205, usd=25.0)
    chosen = pick_venue([poly, kalshi], epsilon=0.0001)
    assert chosen is not None
    assert chosen.venue == "polymarket"


def test_kalshi_wins_when_all_in_is_meaningfully_cheaper() -> None:
    kalshi = price_venue("kalshi", 0.40, 25.0, contracts=62)
    poly = price_venue("polymarket", 0.48, 25.0)
    chosen = pick_venue([kalshi, poly])
    assert chosen is not None
    assert chosen.venue == "kalshi"
    assert chosen.all_in < poly.all_in


def test_poly_only_still_trades() -> None:
    poly = price_venue("polymarket", 0.44, 25.0)
    chosen = pick_venue([poly])
    assert chosen is not None and chosen.venue == "polymarket"


def test_canonical_key_uses_poly_codes_not_betting_aliases() -> None:
    play = SharpPlay(
        league="MLB",
        matchup="AZ @ ATL",
        side="AZ",
        market="moneyline",
        tier="A",
        home_away="away",
        game_time_utc="2026-08-16T17:35:00.000Z",
        implied_fair_prob=0.45,
        rlm_confirmed=True,
        composite_gap=20.0,
        source_path="x",
        raw={"date": "2026-08-16"},
    )
    key = canonical_trade_key_for_play(play)
    assert key == "sharp|mlb|2026-08-16|ari|atl|moneyline|ari"
    ari = resolve_team("MLB", "ARI")
    az = resolve_team("MLB", "AZ")
    atl = resolve_team("MLB", "ATL")
    assert ari is not None and az is not None and atl is not None
    assert canonical_trade_key(play, away=az, home=atl, side_team=az) == canonical_trade_key(
        play, away=ari, home=atl, side_team=ari
    )


def test_canonical_key_normalizes_over_under() -> None:
    play = _play(league="NCAAF", matchup="OHIO @ NEB", side="Under", market="total", home_away="under")
    key = canonical_trade_key_for_play(play)
    assert key is not None
    assert key.endswith("|total|under")


def test_kalshi_moneyline_match_from_cache() -> None:
    play = _play()
    event = {
        "event_ticker": "KXNFLGAME-26SEP17DETBUF",
        "title": "Detroit at Buffalo",
        "sub_title": "DET vs BUF (Sep 17)",
        "close_time": "2026-09-17T00:20:00Z",
        "markets": [
            {
                "ticker": "KXNFLGAME-26SEP17DETBUF-DET",
                "yes_sub_title": "Detroit",
                "yes_ask_dollars": 0.42,
                "yes_bid_dollars": 0.40,
            },
            {
                "ticker": "KXNFLGAME-26SEP17DETBUF-BUF",
                "yes_sub_title": "Buffalo",
                "yes_ask_dollars": 0.58,
                "yes_bid_dollars": 0.56,
            },
        ],
    }
    matched = match_kalshi_plays(
        [play],
        markets=frozenset({"moneyline"}),
        series_cache={"KXNFLGAME": [event]},
        resolved_series={"nfl:moneyline": "KXNFLGAME"},
    )
    assert matched[0].status == "matched"
    assert matched[0].ticker == "KXNFLGAME-26SEP17DETBUF-BUF"
    assert matched[0].kalshi_side == "yes"
    assert matched[0].ask == 0.58


def test_kalshi_spread_within_tolerance() -> None:
    play = _play(
        side="DAL",
        market="spread",
        home_away="home",
        matchup="NYG @ DAL",
        raw={"live": -3.5, "open": -3.0, "date": "2026-09-13"},
        game_time_utc="2026-09-13T17:00:00.000Z",
    )
    event = {
        "event_ticker": "KXNFLSPREAD-26SEP13NYGDAL",
        "sub_title": "NYG vs DAL (Sep 13)",
        "markets": [
            {
                "ticker": "KXNFLSPREAD-26SEP13NYGDAL-DAL3",
                "yes_sub_title": "Dallas wins by over 3.5 points",
                "yes_ask_dollars": 0.48,
                "yes_bid_dollars": 0.46,
                "floor_strike": 3.5,
            }
        ],
    }
    matched = match_kalshi_plays(
        [play],
        markets=frozenset({"spread"}),
        series_cache={"KXNFLSPREAD": [event]},
        resolved_series={"nfl:spread": "KXNFLSPREAD"},
    )
    assert matched[0].status == "matched"
    assert matched[0].kalshi_side == "yes"


def test_kalshi_spread_line_mismatch_skips() -> None:
    play = _play(
        side="DAL",
        market="spread",
        home_away="home",
        matchup="NYG @ DAL",
        raw={"live": 7.5, "open": 7.0, "date": "2026-09-13"},
        game_time_utc="2026-09-13T17:00:00.000Z",
    )
    event = {
        "event_ticker": "KXNFLSPREAD-26SEP13NYGDAL",
        "sub_title": "NYG vs DAL (Sep 13)",
        "markets": [
            {
                "ticker": "KXNFLSPREAD-26SEP13NYGDAL-DAL3",
                "yes_sub_title": "Dallas wins by over 3.5 points",
                "yes_ask_dollars": 0.48,
                "yes_bid_dollars": 0.46,
                "floor_strike": 3.5,
            }
        ],
    }
    matched = match_kalshi_plays(
        [play],
        markets=frozenset({"spread"}),
        series_cache={"KXNFLSPREAD": [event]},
        resolved_series={"nfl:spread": "KXNFLSPREAD"},
    )
    assert matched[0].status == "no_market"
    assert "spread line mismatch" in matched[0].detail


def test_claim_releases_canonical_when_slug_lock_fails() -> None:
    class FakeConvex:
        def __init__(self) -> None:
            self.claimed: list[str] = []
            self.released: list[str] = []

        def claim(self, *, trade_key_value: str, **kwargs: object) -> ClaimResult:
            if trade_key_value.endswith("|buffalo"):
                return ClaimResult(claimed=False, detail="already traded (convex ledger)")
            self.claimed.append(trade_key_value)
            return ClaimResult(claimed=True, detail="claimed")

        def release(self, trade_key_value: str) -> None:
            self.released.append(trade_key_value)

    convex = FakeConvex()
    claimed, err = _claim_event(
        convex,  # type: ignore[arg-type]
        canonical="sharp|nfl|2026-09-17|det|buf|moneyline|buf",
        slug_key="nfl-det-buf-2026-09-17|buffalo",
        league="NFL",
        matchup="DET @ BUF",
        side="Buffalo Bills",
        usd=25.0,
        prediction_date="2026-09-17",
        slug="nfl-det-buf-2026-09-17",
        condition_id="0x1",
        payload={"venue": "kalshi"},
    )
    assert claimed == []
    assert err is not None and "already traded" in err
    assert convex.released == ["sharp|nfl|2026-09-17|det|buf|moneyline|buf"]


def test_claim_both_keys_when_poly_slug_available() -> None:
    class FakeConvex:
        def __init__(self) -> None:
            self.claimed: list[str] = []

        def claim(self, *, trade_key_value: str, payload: dict | None = None, **kwargs: object) -> ClaimResult:
            self.claimed.append(trade_key_value)
            if payload and payload.get("lockOnly"):
                assert payload.get("venue") == "kalshi"
            return ClaimResult(claimed=True, detail="claimed")

        def release(self, trade_key_value: str) -> None:
            raise AssertionError("should not release")

    convex = FakeConvex()
    claimed, err = _claim_event(
        convex,  # type: ignore[arg-type]
        canonical="sharp|nfl|2026-09-17|det|buf|moneyline|buf",
        slug_key="nfl-det-buf-2026-09-17|buffalo bills",
        league="NFL",
        matchup="DET @ BUF",
        side="Buffalo Bills",
        usd=25.0,
        prediction_date="2026-09-17",
        slug="nfl-det-buf-2026-09-17",
        condition_id="0x1",
        payload={"venue": "kalshi"},
    )
    assert err is None
    assert claimed == [
        "sharp|nfl|2026-09-17|det|buf|moneyline|buf",
        "nfl-det-buf-2026-09-17|buffalo bills",
    ]


def test_event_date_in_canonical_key() -> None:
    away = resolve_team("NFL", "DET")
    home = resolve_team("NFL", "BUF")
    assert away is not None and home is not None
    play = _play()
    key = canonical_trade_key(
        play, away=away, home=home, side_team=home, event_date=date(2026, 9, 17)
    )
    assert key == "sharp|nfl|2026-09-17|det|buf|moneyline|buf"


def test_claim_posts_venue() -> None:
    from unittest.mock import MagicMock, patch
    import json

    from polymaker.trading.convex_trades import ConvexTradeClient

    client = ConvexTradeClient(http_url="https://example.convex.site", token="secret")
    with patch("polymaker.trading.convex_trades.requests.post") as post:
        post.return_value = MagicMock(status_code=200, json=lambda: {"ok": True, "claimed": True})
        result = client.claim(
            trade_key_value="sharp|nfl|2026-09-17|det|buf|moneyline|buf",
            league="NFL",
            source="sharp_money",
            matchup="DET @ BUF",
            side="BUF",
            usd=25.0,
            prediction_date="2026-09-17",
            venue="kalshi",
            payload={"venue": "kalshi"},
        )
    assert result.claimed is True
    body = json.loads(post.call_args.kwargs["data"])
    assert body["venue"] == "kalshi"
    assert body["tradeKey"].startswith("sharp|")
