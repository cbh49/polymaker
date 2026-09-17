"""Watch-list export includes NFL moneylines (whale monitor + X tweets)."""

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
    _fresh_moneylines,
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
    )


def test_watch_prefixes_include_nfl() -> None:
    assert WATCH_SLUG_PREFIXES == ("mlb-", "wnba-", "nfl-")
    assert _league_from_slug("nfl-dal-nyg-2026-09-20") == "NFL"


def test_fresh_moneylines_includes_nfl_and_skips_spreads(tmp_path) -> None:
    store = CatalogStore(tmp_path / "s.db")
    now = datetime(2026, 9, 17, 16, 0, tzinfo=UTC)
    today = date(2026, 9, 17)
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
                start="2026-09-20T17:00:00Z",
            ),
            _meta(
                "nfl-dal-nyg-2026-09-20-spread-home-3pt5",
                cid="0xnflspread",
                yes="Giants -3.5",
                no="Cowboys +3.5",
                start="2026-09-20T17:00:00Z",
            ),
            _meta(
                "cfb-ohio-mich-2026-09-19",
                cid="0xcfb",
                yes="Michigan Wolverines",
                no="Ohio State Buckeyes",
                start="2026-09-19T16:00:00Z",
            ),
        ]
    )
    try:
        markets = _fresh_moneylines(
            store,
            look_ahead_days=3,
            today=today,
            now=now,
        )
    finally:
        store.close()

    slugs = {m.slug for m in markets}
    assert "nfl-dal-nyg-2026-09-20" in slugs
    assert "mlb-bos-nyy-2026-09-17" in slugs
    assert "nfl-dal-nyg-2026-09-20-spread-home-3pt5" not in slugs
    assert "cfb-ohio-mich-2026-09-19" not in slugs

    rows = markets_to_watch_rows(markets)
    nfl = next(r for r in rows if r["league"] == "NFL")
    assert nfl["label"] == "Dallas Cowboys vs New York Giants ML"
    assert nfl["slug"] == "nfl-dal-nyg-2026-09-20"


def test_fresh_moneylines_uses_nfl_weekend_window(tmp_path) -> None:
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
        markets = _fresh_moneylines(
            store,
            look_ahead_days=3,
            today=today,
            now=now,
        )
    finally:
        store.close()

    slugs = {m.slug for m in markets}
    assert "nfl-kc-phi-2026-09-24" in slugs
    assert "mlb-chc-stl-2026-09-24" not in slugs
