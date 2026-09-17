"""Watch-list export: NFL moneylines plus the 50¢ consensus spread/total."""

from __future__ import annotations

import sys
from datetime import UTC, date, datetime
from pathlib import Path

from polymaker.catalog.store import CatalogStore
from polymaker.domain import MarketMeta, TokenMeta

_FINDER = Path(__file__).resolve().parents[1] / "poly-sharp-finder"
if str(_FINDER) not in sys.path:
    sys.path.insert(0, str(_FINDER))

from export_watch_list import (  # noqa: E402
    WATCH_SLUG_PREFIXES,
    _fresh_watch_markets,
    _league_from_slug,
    markets_to_watch_rows,
)


def _meta(
    slug: str,
    *,
    cid: str,
    yes: str,
    no: str,
    start: str,
    bid: float = 0.0,
    ask: float = 0.0,
    liq: float = 0.0,
) -> MarketMeta:
    return MarketMeta(
        condition_id=cid,
        question=f"{yes} vs {no}",
        slug=slug,
        tokens=(TokenMeta("yes-token", yes), TokenMeta("no-token", no)),
        tick_size=0.01,
        neg_risk=False,
        min_order_size=5.0,
        rewards_min_size=10.0,
        rewards_max_spread=3.0,
        rewards_daily_rate=50.0,
        maker_fee_bps=0,
        taker_fee_bps=100,
        fees_enabled=True,
        end_date_iso=start,
        event_id="evt-1",
        start_time_iso=start,
        best_bid=bid,
        best_ask=ask,
        liquidity_num=liq,
    )


def test_watch_prefixes_include_nfl() -> None:
    assert WATCH_SLUG_PREFIXES == ("mlb-", "wnba-", "nfl-")
    assert _league_from_slug("nfl-dal-nyg-2026-09-20") == "NFL"
    assert _league_from_slug("nfl-dal-nyg-2026-09-20-spread-home-3pt5") == "NFL"
    assert _league_from_slug("nfl-dal-nyg-2026-09-20-total-44pt5") == "NFL"


def test_fresh_watch_markets_picks_consensus_spread_and_total(tmp_path) -> None:
    store = CatalogStore(tmp_path / "s.db")
    now = datetime(2026, 9, 17, 16, 0, tzinfo=UTC)
    today = date(2026, 9, 17)
    start = "2026-09-20T17:00:00Z"
    extras = [
        _meta(
            f"nfl-dal-nyg-2026-09-20-total-{n}pt5",
            cid=f"0xalt{n}",
            yes="Over",
            no="Under",
            start=start,
            bid=0.10,
            ask=0.12,
            liq=500.0,
        )
        for n in range(20, 80)
    ]
    store.upsert_many(
        [
            _meta(
                "mlb-bos-nyy-2026-09-17",
                cid="0xmlb",
                yes="New York Yankees",
                no="Boston Red Sox",
                start="2026-09-17T23:05:00Z",
            ),
            _meta(
                "nfl-dal-nyg-2026-09-20",
                cid="0xnfl",
                yes="New York Giants",
                no="Dallas Cowboys",
                start=start,
            ),
            _meta(
                "nfl-dal-nyg-2026-09-20-spread-home-14pt5",
                cid="0xaltspd",
                yes="New York Giants",
                no="Dallas Cowboys",
                start=start,
                bid=0.22,
                ask=0.24,
                liq=50_000.0,
            ),
            _meta(
                "nfl-dal-nyg-2026-09-20-spread-home-3pt5",
                cid="0xmainspd",
                yes="New York Giants",
                no="Dallas Cowboys",
                start=start,
                bid=0.50,
                ask=0.51,
                liq=20_000.0,
            ),
            _meta(
                "nfl-dal-nyg-2026-09-20-total-61pt5",
                cid="0xalttot",
                yes="Over",
                no="Under",
                start=start,
                bid=0.10,
                ask=0.13,
                liq=9_000.0,
            ),
            _meta(
                "nfl-dal-nyg-2026-09-20-total-44pt5",
                cid="0xmaintot",
                yes="Over",
                no="Under",
                start=start,
                bid=0.50,
                ask=0.51,
                liq=8_000.0,
            ),
            _meta(
                "cfb-ohio-mich-2026-09-19",
                cid="0xcfb",
                yes="Michigan Wolverines",
                no="Ohio State Buckeyes",
                start="2026-09-19T16:00:00Z",
            ),
            *extras,
        ]
    )
    try:
        markets = _fresh_watch_markets(store, look_ahead_days=3, today=today, now=now)
    finally:
        store.close()

    slugs = {m.slug for m in markets}
    assert "nfl-dal-nyg-2026-09-20" in slugs
    assert "nfl-dal-nyg-2026-09-20-spread-home-3pt5" in slugs
    assert "nfl-dal-nyg-2026-09-20-total-44pt5" in slugs
    assert "nfl-dal-nyg-2026-09-20-spread-home-14pt5" not in slugs
    assert "nfl-dal-nyg-2026-09-20-total-61pt5" not in slugs
    assert "mlb-bos-nyy-2026-09-17" in slugs
    assert "cfb-ohio-mich-2026-09-19" not in slugs
    assert len([s for s in slugs if s.startswith("nfl-dal-nyg")]) == 3

    rows = markets_to_watch_rows(markets)
    nfl_ml = next(r for r in rows if r["slug"] == "nfl-dal-nyg-2026-09-20")
    assert nfl_ml["league"] == "NFL"
    assert nfl_ml["label"] == "Dallas Cowboys vs New York Giants ML"
    nfl_ou = next(r for r in rows if r["slug"].endswith("total-44pt5"))
    assert nfl_ou["label"] == "O/U 44.5"


def test_fresh_watch_markets_uses_nfl_weekend_window(tmp_path) -> None:
    """NFL look-ahead is at least 7 days even when catalog default is 3."""
    store = CatalogStore(tmp_path / "s.db")
    now = datetime(2026, 9, 17, 16, 0, tzinfo=UTC)
    today = date(2026, 9, 17)
    store.upsert_many(
        [
            _meta(
                "nfl-kc-phi-2026-09-24",
                cid="0xnflthu",
                yes="Philadelphia Eagles",
                no="Kansas City Chiefs",
                start="2026-09-24T20:15:00Z",
            ),
            _meta(
                "mlb-chc-stl-2026-09-24",
                cid="0xmlblate",
                yes="St. Louis Cardinals",
                no="Chicago Cubs",
                start="2026-09-24T23:15:00Z",
            ),
        ]
    )
    try:
        markets = _fresh_watch_markets(store, look_ahead_days=3, today=today, now=now)
    finally:
        store.close()

    slugs = {m.slug for m in markets}
    assert "nfl-kc-phi-2026-09-24" in slugs
    assert "mlb-chc-stl-2026-09-24" not in slugs
