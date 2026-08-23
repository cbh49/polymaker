"""Shared test fixtures."""

from __future__ import annotations

import pytest

from polymaker.domain import MarketMeta, TokenMeta


@pytest.fixture
def meta() -> MarketMeta:
    return MarketMeta(
        condition_id="0xcond",
        question="Will X happen?",
        slug="will-x-happen",
        tokens=(TokenMeta("yes-token", "Yes"), TokenMeta("no-token", "No")),
        tick_size=0.01,
        neg_risk=False,
        min_order_size=5.0,
        rewards_min_size=10.0,
        rewards_max_spread=3.0,
        rewards_daily_rate=50.0,
        maker_fee_bps=0,
        taker_fee_bps=100,
        fees_enabled=True,
        end_date_iso="2028-11-07T00:00:00Z",
        event_id="evt-1",
    )
