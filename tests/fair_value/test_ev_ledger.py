"""NFL EV trade-key helpers and Convex claim/release."""

from __future__ import annotations

from ev_trading.fair_value.ev_ledger import (
    claim_nfl_ev,
    meets_min_edge,
    nfl_ev_trade_key,
    poly_slug_lock_key,
    venues_from_trade_arg,
)
from polymaker.trading.convex_trades import ClaimResult


def test_venues_from_trade_arg() -> None:
    assert venues_from_trade_arg(None) == ()
    assert venues_from_trade_arg("kalshi") == ("kalshi",)
    assert venues_from_trade_arg("polymarket") == ("polymarket",)
    assert venues_from_trade_arg("both") == ("kalshi", "polymarket")


def test_nfl_ev_trade_key_normalizes_side() -> None:
    assert nfl_ev_trade_key("kalshi", "KXNFL-1", "Over") == "nfl-ev|kalshi|KXNFL-1|over"
    assert nfl_ev_trade_key("polymarket", "3340142", "home") == "nfl-ev|polymarket|3340142|home"


def test_meets_min_edge_uses_fee_adjusted_or_edge_pct() -> None:
    assert meets_min_edge({"fee_adjusted_edge": 0.05}, 5.0) is True
    assert meets_min_edge({"fee_adjusted_edge": 0.049}, 5.0) is False
    assert meets_min_edge({"edge_pct": 6.0}, 5.0) is True
    assert meets_min_edge({}, 5.0) is False


def test_claim_nfl_ev_releases_canonical_when_slug_lock_fails() -> None:
    ledger = _FakeConvex(claim_results=[True, False])
    claimed, err = claim_nfl_ev(
        ledger,
        venue="polymarket",
        market_id="3340142",
        side="home",
        matchup="DAL @ NYG",
        usd=10.0,
        payload={"venue": "polymarket"},
        slug="nfl-dal-nyg",
        slug_lock_key=poly_slug_lock_key("nfl-dal-nyg", "New York Giants"),
    )
    assert claimed == []
    assert err
    assert ledger.releases == ["nfl-ev|polymarket|3340142|home"]


def test_claim_nfl_ev_records_canonical_and_slug_lock() -> None:
    ledger = _FakeConvex()
    claimed, err = claim_nfl_ev(
        ledger,
        venue="polymarket",
        market_id="3340142",
        side="home",
        matchup="DAL @ NYG",
        usd=10.0,
        payload={"venue": "polymarket"},
        slug="nfl-dal-nyg",
        slug_lock_key=poly_slug_lock_key("nfl-dal-nyg", "New York Giants"),
    )
    assert err is None
    assert claimed[0] == "nfl-ev|polymarket|3340142|home"
    assert claimed[1] == "nfl-dal-nyg|new york giants"
    assert ledger.claims[0]["source"] == "nfl-ev"
    assert ledger.claims[0]["league"] == "nfl"
    assert ledger.claims[1]["payload"]["lockOnly"] is True


class _FakeConvex:
    def __init__(self, *, claim_results: list[bool] | None = None) -> None:
        self.configured = True
        self._claim_results = list(claim_results) if claim_results is not None else None
        self.claims: list[dict] = []
        self.releases: list[str] = []

    def claim(self, **kwargs) -> ClaimResult:
        self.claims.append(kwargs)
        ok = True
        if self._claim_results:
            ok = self._claim_results.pop(0)
        return ClaimResult(
            claimed=ok,
            detail="claimed" if ok else "already traded (convex ledger)",
        )

    def complete(self, trade_key_value: str, payload: dict, **kwargs) -> None:
        return None

    def release(self, trade_key_value: str) -> None:
        self.releases.append(trade_key_value)
