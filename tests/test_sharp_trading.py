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

    spread_and_total = filter_plays(
        plays,
        SharpTradeConfig(min_tier="B", markets=frozenset({"moneyline", "spread", "total"})),
    )
    assert [p.matchup for p in spread_and_total] == ["AZ @ ATL", "BOS @ NYY", "LAD @ SF", "DAL @ GS"]

    total_play = SharpPlay(
        league="NCAAF",
        matchup="OHIO @ NEB",
        side="Under",
        market="total",
        tier="A",
        home_away="under",
        game_time_utc=None,
        implied_fair_prob=0.5,
        rlm_confirmed=True,
        composite_gap=12.0,
        source_path="x",
        raw={"live": 46.0, "open": 46.5},
    )
    with_total = filter_plays(
        [*plays, total_play],
        SharpTradeConfig(min_tier="B", markets=frozenset({"moneyline", "spread", "total"})),
    )
    assert any(p.market == "total" and p.side == "Under" for p in with_total)
    ml_only_drops_total = filter_plays(
        [*plays, total_play],
        SharpTradeConfig(min_tier="B", markets=frozenset({"moneyline"})),
    )
    assert all(p.market == "moneyline" for p in ml_only_drops_total)


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


def _gamma_binary(
    slug: str,
    condition_id: str,
    outcomes: tuple[str, str],
    tokens: tuple[str, str],
    *,
    liquidity: float = 5000.0,
) -> dict:
    return {
        "slug": slug,
        "conditionId": condition_id,
        "closed": False,
        "acceptingOrders": True,
        "clobTokenIds": json.dumps(list(tokens)),
        "outcomes": json.dumps(list(outcomes)),
        "orderPriceMinTickSize": 0.01,
        "orderMinSize": 5,
        "negRisk": False,
        "liquidityNum": liquidity,
        "bestAsk": 0.48,
        "bestBid": 0.46,
        "volume24hr": 100.0,
    }


class _EventGamma:
    def __init__(self, events: dict[str, dict]) -> None:
        self.events = events

    async def event_by_slug(self, slug: str) -> dict | None:
        return self.events.get(slug)

    async def market_by_slug(self, slug: str) -> None:
        return None

    async def iter_events(self, **kwargs: object):  # noqa: ANN003
        if False:
            yield {}

    async def aclose(self) -> None:
        return None


@pytest.mark.asyncio
async def test_match_cfb_total_over_under(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from datetime import UTC, datetime, timedelta

    import polymaker.trading.match as match_mod
    from polymaker.catalog.store import CatalogStore
    from polymaker.trading.match import match_sharp_plays

    real_resolve = match_mod.resolve_team

    def _guard(league: str, raw: str):
        assert str(raw).strip().lower() not in {"over", "under"}, raw
        return real_resolve(league, raw)

    monkeypatch.setattr(match_mod, "resolve_team", _guard)

    kickoff = datetime.now(UTC) + timedelta(days=4)
    ymd = kickoff.date().isoformat()
    event_slug = f"cfb-ohio-neb-{ymd}"
    total_slug = f"{event_slug}-total-46"
    start = kickoff.strftime("%Y-%m-%dT%H:%M:%SZ")
    event = {
        "slug": event_slug,
        "eventDate": ymd,
        "startTime": start,
        "markets": [
            _gamma_binary(
                event_slug,
                "0xml-ohio",
                ("Ohio", "Nebraska"),
                ("tok-ohio", "tok-neb"),
            ),
            _gamma_binary(
                total_slug,
                "0xtot-ohio",
                ("Over", "Under"),
                ("tok-over", "tok-under"),
            ),
            _gamma_binary(
                f"{event_slug}-total-49pt5",
                "0xtot-far",
                ("Over", "Under"),
                ("tok-over-far", "tok-under-far"),
                liquidity=9000.0,
            ),
            {
                "slug": f"{event_slug}-1h-total-24pt5",
                "closed": False,
                "liquidityNum": 8000,
                "acceptingOrders": True,
            },
        ],
    }
    store = CatalogStore(tmp_path / "s.db")
    play = SharpPlay(
        league="NCAAF",
        matchup="OHIO @ NEB",
        side="Under",
        market="total",
        tier="A",
        home_away="under",
        game_time_utc=kickoff.strftime("%Y-%m-%dT%H:%M:%S.000Z"),
        implied_fair_prob=0.51,
        rlm_confirmed=True,
        composite_gap=12.0,
        source_path="x",
        raw={"live": 46.0, "open": 46.5, "date": ymd},
    )
    matched = await match_sharp_plays(
        [play],
        store=store,
        gamma=_EventGamma({event_slug: event}),  # type: ignore[arg-type]
        markets=frozenset({"moneyline", "spread", "total"}),
    )
    store.close()
    assert matched[0].status == "matched"
    assert matched[0].slug == total_slug
    assert matched[0].token is not None
    assert matched[0].token.outcome == "Under"


@pytest.mark.asyncio
async def test_match_cfb_spread_fresno(tmp_path: Path) -> None:
    from datetime import UTC, datetime, timedelta

    from polymaker.catalog.store import CatalogStore
    from polymaker.trading.match import match_sharp_plays

    kickoff = datetime.now(UTC) + timedelta(days=4)
    ymd = kickoff.date().isoformat()
    event_slug = f"cfb-fres-usc-{ymd}"
    spread_slug = f"{event_slug}-spread-home-21pt5"
    start = kickoff.strftime("%Y-%m-%dT%H:%M:%SZ")
    event = {
        "slug": event_slug,
        "eventDate": ymd,
        "startTime": start,
        "markets": [
            _gamma_binary(
                event_slug,
                "0xml-fres",
                ("Fresno State", "USC"),
                ("tok-fres", "tok-usc"),
            ),
            _gamma_binary(
                f"{event_slug}-spread-home-14pt5",
                "0xsp-14",
                ("Fresno State", "USC"),
                ("tok-fres-14", "tok-usc-14"),
                liquidity=9000.0,
            ),
            _gamma_binary(
                spread_slug,
                "0xsp-21",
                ("Fresno State", "USC"),
                ("tok-fres-21", "tok-usc-21"),
                liquidity=2500.0,
            ),
        ],
    }
    store = CatalogStore(tmp_path / "s.db")
    play = SharpPlay(
        league="NCAAF",
        matchup="FRES @ USC",
        side="FRES",
        market="spread",
        tier="B",
        home_away="away",
        game_time_utc=kickoff.strftime("%Y-%m-%dT%H:%M:%S.000Z"),
        implied_fair_prob=0.48,
        rlm_confirmed=True,
        composite_gap=15.0,
        source_path="x",
        raw={"live": 21.5, "open": 23.0, "date": ymd},
    )
    matched = await match_sharp_plays(
        [play],
        store=store,
        gamma=_EventGamma({event_slug: event}),  # type: ignore[arg-type]
        markets=frozenset({"moneyline", "spread", "total"}),
    )
    store.close()
    assert matched[0].status == "matched"
    assert matched[0].slug == spread_slug
    assert matched[0].token is not None
    assert matched[0].token.outcome == "Fresno State"


@pytest.mark.asyncio
async def test_match_spread_line_mismatch_skips(tmp_path: Path) -> None:
    from datetime import UTC, datetime, timedelta

    from polymaker.catalog.store import CatalogStore
    from polymaker.trading.match import match_sharp_plays

    kickoff = datetime.now(UTC) + timedelta(days=4)
    ymd = kickoff.date().isoformat()
    event_slug = f"cfb-fres-usc-{ymd}"
    start = kickoff.strftime("%Y-%m-%dT%H:%M:%SZ")
    event = {
        "slug": event_slug,
        "eventDate": ymd,
        "startTime": start,
        "markets": [
            _gamma_binary(
                f"{event_slug}-spread-home-14pt5",
                "0xsp-14",
                ("Fresno State", "USC"),
                ("tok-fres-14", "tok-usc-14"),
            ),
        ],
    }
    store = CatalogStore(tmp_path / "s.db")
    play = SharpPlay(
        league="NCAAF",
        matchup="FRES @ USC",
        side="FRES",
        market="spread",
        tier="B",
        home_away="away",
        game_time_utc=kickoff.strftime("%Y-%m-%dT%H:%M:%S.000Z"),
        implied_fair_prob=0.48,
        rlm_confirmed=True,
        composite_gap=15.0,
        source_path="x",
        raw={"live": 21.5, "open": 23.0, "date": ymd},
    )
    matched = await match_sharp_plays(
        [play],
        store=store,
        gamma=_EventGamma({event_slug: event}),  # type: ignore[arg-type]
        markets=frozenset({"spread"}),
    )
    store.close()
    assert matched[0].status == "no_market"
    assert "spread line mismatch play=21.5 poly=14.5" in matched[0].detail


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


def test_stake_to_win_plus_250() -> None:
    from polymaker.trading.execute import stake_to_win

    # +250 American = 100/350 ≈ 0.2857 → $10 to profit $25
    p = 100.0 / 350.0
    assert round(stake_to_win(25.0, p), 2) == 10.0


def test_usd_for_play_low_volume_dog() -> None:
    from polymaker.trading.execute import SharpTradeConfig, _usd_for_play

    cfg = SharpTradeConfig(usd_tier_a=25.0, usd_tier_b=10.0)
    dog = SharpPlay(
        league="NCAAF",
        matchup="UNC @ TCU",
        side="UNC",
        market="moneyline",
        tier="A",
        home_away="away",
        game_time_utc=None,
        implied_fair_prob=None,
        rlm_confirmed=True,
        composite_gap=45.75,
        source_path="x",
        raw={"live": 250},
        low_volume_dog_flag=True,
    )
    # Prefer Polymarket ask when present (+250 ≈ 28.6¢)
    assert _usd_for_play(dog, cfg, ask=100.0 / 350.0) == 10.0
    # Fall back to sportsbook American odds on the play
    assert _usd_for_play(dog, cfg, ask=None) == 10.0
    # Never size above the flat tier stake (short price)
    assert _usd_for_play(dog, cfg, ask=0.60) == 25.0

    favorite = SharpPlay(
        league="MLB",
        matchup="AZ @ ATL",
        side="AZ",
        market="moneyline",
        tier="A",
        home_away="away",
        game_time_utc=None,
        implied_fair_prob=0.55,
        rlm_confirmed=True,
        composite_gap=20.0,
        source_path="x",
        raw={},
        low_volume_dog_flag=False,
    )
    assert _usd_for_play(favorite, cfg, ask=0.48) == 25.0

    tier_b_dog = SharpPlay(
        league="NCAAF",
        matchup="UNC @ TCU",
        side="UNC",
        market="moneyline",
        tier="B",
        home_away="away",
        game_time_utc=None,
        implied_fair_prob=None,
        rlm_confirmed=True,
        composite_gap=20.0,
        source_path="x",
        raw={"live": 250},
        low_volume_dog_flag=True,
    )
    # Tier B to-win $10 at +250 → $4 stake
    assert _usd_for_play(tier_b_dog, cfg, ask=None) == 4.0


def test_load_sharp_file_dog_flag(tmp_path: Path) -> None:
    path = tmp_path / "ncaaf_sharp_money.json"
    path.write_text(
        json.dumps(
            {
                "league": "NCAAF",
                "plays": [
                    {
                        "matchup": "UNC @ TCU",
                        "side": "UNC",
                        "market": "moneyline",
                        "tier": "A",
                        "rlm_confirmed": True,
                        "low_volume_dog_flag": True,
                        "live": 250,
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    plays = load_sharp_file(path)
    assert plays[0].low_volume_dog_flag is True
    assert plays[0].raw["live"] == 250
