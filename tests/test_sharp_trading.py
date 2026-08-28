"""Tests for sharp-money → Polymarket matching / team maps."""

from __future__ import annotations

import json
from datetime import date
from pathlib import Path

import pytest

from polymaker.domain import MarketMeta, TokenMeta
from polymaker.trading.execute import SharpTradeConfig, filter_plays
from polymaker.trading.match import candidate_event_dates, resolve_outcome_token
from polymaker.trading.sharp import SharpPlay, load_sharp_file
from polymaker.trading.teams import parse_matchup, resolve_team


def test_parse_matchup_variants() -> None:
    assert parse_matchup("AZ @ ATL") == ("AZ", "ATL")
    assert parse_matchup("AZ@ATL") == ("AZ", "ATL")
    assert parse_matchup("PHX vs LA") == ("PHX", "LA")
    assert parse_matchup("bad") is None


def test_mlb_team_aliases() -> None:
    az = resolve_team("MLB", "AZ")
    assert az is not None
    assert az.poly_code == "ari"
    assert az.full_name == "Arizona Diamondbacks"

    cws = resolve_team("mlb", "CWS")
    assert cws is not None and cws.poly_code == "cws"

    oak = resolve_team("MLB", "ATH")
    assert oak is not None and oak.poly_code == "oak"


def test_wnba_team_aliases() -> None:
    gsv = resolve_team("WNBA", "GS")
    assert gsv is not None and gsv.poly_code == "gsv"

    aces = resolve_team("WNBA", "LV")
    assert aces is not None and aces.poly_code == "las"
    assert "Aces" in aces.full_name

    sparks = resolve_team("WNBA", "LA")
    assert sparks is not None and sparks.poly_code == "la"


def test_ncaaf_team_aliases() -> None:
    haw = resolve_team("NCAAF", "Hawaii")
    assert haw is not None
    assert haw.poly_code == "hawaii"
    assert haw.betting_abbr == "HAW"


def test_candidate_event_dates_et() -> None:
    # 17:35 UTC = 1:35 PM ET on Aug 16
    dates = candidate_event_dates("2026-08-16T17:35:00.000Z")
    assert date(2026, 8, 16) in dates


def test_resolve_outcome_token_fuzzy() -> None:
    meta = MarketMeta(
        condition_id="0x1",
        question="Arizona Diamondbacks vs. Atlanta Braves",
        slug="mlb-ari-atl-2026-08-17",
        tokens=(
            TokenMeta("t1", "Arizona Diamondbacks"),
            TokenMeta("t2", "Atlanta Braves"),
        ),
        tick_size=0.01,
        neg_risk=False,
        min_order_size=5,
        rewards_min_size=0,
        rewards_max_spread=0,
        rewards_daily_rate=0,
        maker_fee_bps=0,
        taker_fee_bps=0,
        fees_enabled=False,
        end_date_iso=None,
        event_id=None,
    )
    tok = resolve_outcome_token(meta, "Arizona Diamondbacks", "ari")
    assert tok.token_id == "t1"
    tok2 = resolve_outcome_token(meta, "Diamondbacks", "ari")
    assert tok2.token_id == "t1"


def test_load_sharp_file(tmp_path: Path) -> None:
    path = tmp_path / "mlb_sharp_money.json"
    path.write_text(
        json.dumps(
            {
                "league": "MLB",
                "plays": [
                    {
                        "matchup": "AZ @ ATL",
                        "side": "AZ",
                        "market": "moneyline",
                        "tier": "A",
                        "game_time_utc": "2026-08-16T17:35:00.000Z",
                        "implied_fair_prob": 0.45,
                        "rlm_confirmed": True,
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    plays = load_sharp_file(path)
    assert len(plays) == 1
    assert plays[0].side == "AZ"
    assert plays[0].tier == "A"


def test_filter_plays_tier_and_market() -> None:
    plays = [
        SharpPlay(
            league="MLB",
            matchup="AZ @ ATL",
            side="AZ",
            market="moneyline",
            tier="A",
            home_away="away",
            game_time_utc=None,
            implied_fair_prob=0.5,
            rlm_confirmed=True,
            composite_gap=20.0,
            source_path="x",
            raw={},
        ),
        SharpPlay(
            league="MLB",
            matchup="BOS @ NYY",
            side="BOS",
            market="spread",
            tier="B",
            home_away="away",
            game_time_utc=None,
            implied_fair_prob=0.5,
            rlm_confirmed=False,
            composite_gap=10.0,
            source_path="x",
            raw={},
        ),
        SharpPlay(
            league="MLB",
            matchup="LAD @ SF",
            side="LAD",
            market="moneyline",
            tier="B",
            home_away="away",
            game_time_utc=None,
            implied_fair_prob=0.5,
            rlm_confirmed=False,
            composite_gap=10.0,
            source_path="x",
            raw={},
        ),
        SharpPlay(
            league="WNBA",
            matchup="DAL @ GS",
            side="DAL",
            market="moneyline",
            tier="A",
            home_away="away",
            game_time_utc=None,
            implied_fair_prob=0.5,
            rlm_confirmed=True,
            composite_gap=20.0,
            source_path="x",
            raw={},
        ),
    ]
    only_a = filter_plays(plays, SharpTradeConfig(min_tier="A", markets=frozenset({"moneyline"})))
    assert [p.matchup for p in only_a] == ["AZ @ ATL", "DAL @ GS"]

    a_plus = SharpPlay(
        league="MLB",
        matchup="SEA @ TEX",
        side="SEA",
        market="moneyline",
        tier="A+",
        home_away="away",
        game_time_utc=None,
        implied_fair_prob=0.5,
        rlm_confirmed=True,
        composite_gap=25.0,
        source_path="x",
        raw={},
    )
    a_plus_only = filter_plays(
        [a_plus, *plays],
        SharpTradeConfig(min_tier="A", markets=frozenset({"moneyline"})),
    )
    assert [p.matchup for p in a_plus_only] == ["SEA @ TEX", "AZ @ ATL", "DAL @ GS"]

    a_and_b = filter_plays(plays, SharpTradeConfig(min_tier="B", markets=frozenset({"moneyline"})))
    assert [p.matchup for p in a_and_b] == ["AZ @ ATL", "LAD @ SF", "DAL @ GS"]

    wnba_only = filter_plays(
        plays,
        SharpTradeConfig(min_tier="B", markets=frozenset({"moneyline"})),
        league="wnba",
    )
    assert [p.matchup for p in wnba_only] == ["DAL @ GS"]


@pytest.mark.asyncio
async def test_match_wnba_play_with_catalog(tmp_path: Path) -> None:
    from polymaker.catalog.store import CatalogStore
    from polymaker.trading.match import match_sharp_plays

    store = CatalogStore(tmp_path / "s.db")
    meta = MarketMeta(
        condition_id="0xdal",
        question="Dallas Wings vs. Golden State Valkyries",
        slug="wnba-dal-gsv-2026-08-17",
        tokens=(
            TokenMeta("tok-dal", "Dallas Wings"),
            TokenMeta("tok-gsv", "Golden State Valkyries"),
        ),
        tick_size=0.01,
        neg_risk=False,
        min_order_size=5,
        rewards_min_size=0,
        rewards_max_spread=0,
        rewards_daily_rate=0,
        maker_fee_bps=0,
        taker_fee_bps=50,
        fees_enabled=True,
        end_date_iso="2026-08-17T23:00:00Z",
        event_id="2",
        best_ask=0.55,
    )
    store.upsert_market(meta)

    play = SharpPlay(
        league="WNBA",
        matchup="DAL @ GS",
        side="DAL",
        market="moneyline",
        tier="A",
        home_away="away",
        game_time_utc="2026-08-17T23:00:00.000Z",
        implied_fair_prob=0.52,
        rlm_confirmed=True,
        composite_gap=18.0,
        source_path="x",
        raw={},
    )

    class _FakeGamma:
        async def event_by_slug(self, slug: str) -> None:
            return None

        async def market_by_slug(self, slug: str) -> None:
            return None

        async def iter_events(self, **kwargs: object):  # noqa: ANN003
            if False:
                yield {}

        async def aclose(self) -> None:
            return None

    matched = await match_sharp_plays([play], store=store, gamma=_FakeGamma())  # type: ignore[arg-type]
    store.close()
    assert matched[0].status == "matched"
    assert matched[0].slug == "wnba-dal-gsv-2026-08-17"
    assert matched[0].token is not None
    assert matched[0].token.outcome == "Dallas Wings"


@pytest.mark.asyncio
async def test_match_play_with_catalog(tmp_path: Path) -> None:
    from polymaker.catalog.store import CatalogStore
    from polymaker.trading.match import match_sharp_plays
    from polymaker.trading.sharp import SharpPlay

    store = CatalogStore(tmp_path / "s.db")
    meta = MarketMeta(
        condition_id="0xari",
        question="Arizona Diamondbacks vs. Atlanta Braves",
        slug="mlb-ari-atl-2026-08-16",
        tokens=(
            TokenMeta("tok-ari", "Arizona Diamondbacks"),
            TokenMeta("tok-atl", "Atlanta Braves"),
        ),
        tick_size=0.01,
        neg_risk=False,
        min_order_size=5,
        rewards_min_size=0,
        rewards_max_spread=0,
        rewards_daily_rate=0,
        maker_fee_bps=0,
        taker_fee_bps=50,
        fees_enabled=True,
        end_date_iso="2026-08-16T23:00:00Z",
        event_id="1",
        best_ask=0.48,
    )
    store.upsert_market(meta)

    play = SharpPlay(
        league="MLB",
        matchup="AZ @ ATL",
        side="AZ",
        market="moneyline",
        tier="A",
        home_away="away",
        game_time_utc="2026-08-16T17:35:00.000Z",
        implied_fair_prob=0.456,
        rlm_confirmed=True,
        composite_gap=30.0,
        source_path="x",
        raw={},
    )

    # Avoid live Gamma: monkeypatch event/market lookups to miss so catalog wins.
    class _FakeGamma:
        async def event_by_slug(self, slug: str) -> None:
            return None

        async def market_by_slug(self, slug: str) -> None:
            return None

        async def iter_events(self, **kwargs: object):  # noqa: ANN003
            if False:
                yield {}

        async def aclose(self) -> None:
            return None

    matched = await match_sharp_plays([play], store=store, gamma=_FakeGamma())  # type: ignore[arg-type]
    store.close()
    assert matched[0].status == "matched"
    assert matched[0].slug == "mlb-ari-atl-2026-08-16"
    assert matched[0].token is not None
    assert matched[0].token.outcome == "Arizona Diamondbacks"


@pytest.mark.asyncio
async def test_trade_skips_when_not_pre_game() -> None:
    from datetime import date

    from polymaker.trading.execute import SharpTradeConfig, _trade_one
    from polymaker.trading.match import MatchedPlay
    from polymaker.trading.teams import TeamRef

    team = TeamRef("ARI", "ari", "Arizona Diamondbacks")
    home = TeamRef("ATL", "atl", "Atlanta Braves")
    meta = MarketMeta(
        condition_id="0xari",
        question="Arizona Diamondbacks vs. Atlanta Braves",
        slug="mlb-ari-atl-2026-08-16",
        tokens=(
            TokenMeta("tok-ari", "Arizona Diamondbacks"),
            TokenMeta("tok-atl", "Atlanta Braves"),
        ),
        tick_size=0.01,
        neg_risk=False,
        min_order_size=5,
        rewards_min_size=0,
        rewards_max_spread=0,
        rewards_daily_rate=0,
        maker_fee_bps=0,
        taker_fee_bps=50,
        fees_enabled=True,
        end_date_iso="2026-08-16T23:00:00Z",
        event_id="1",
        best_ask=0.48,
        start_time_iso="2026-08-16T17:35:00Z",
    )
    play = SharpPlay(
        league="MLB",
        matchup="AZ @ ATL",
        side="AZ",
        market="moneyline",
        tier="A",
        home_away="away",
        game_time_utc="2026-08-16T17:35:00.000Z",
        implied_fair_prob=0.456,
        rlm_confirmed=True,
        composite_gap=30.0,
        source_path="x",
        raw={},
    )
    matched = MatchedPlay(
        play=play,
        away=team,
        home=home,
        side_team=team,
        event_date=date(2026, 8, 16),
        slug=meta.slug,
        meta=meta,
        token=meta.tokens[0],
        status="matched",
    )

    class _Gw:
        async def get_book(self, token_id: str) -> dict:
            raise AssertionError("must not fetch book for a started game")

    result = await _trade_one(matched, _Gw(), SharpTradeConfig(), set())  # type: ignore[arg-type]
    assert result.action == "skipped"
    assert "pre-game" in result.detail


def test_price_gate_max_ask() -> None:
    from polymaker.trading.execute import SharpTradeConfig, _price_gate

    cfg = SharpTradeConfig(max_ask=0.55)
    assert _price_gate(0.45, None, cfg) is None
    assert _price_gate(0.55, None, cfg) is None
    reason = _price_gate(0.999, None, cfg)
    assert reason is not None and "max_ask" in reason
    missing = _price_gate(None, None, cfg)
    assert missing is not None and "no ask" in missing
