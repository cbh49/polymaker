"""Convex trade-ledger client (mocked HTTP)."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

from polymaker.trading.convex_trades import ConvexTradeClient, trade_key


def test_trade_key_normalizes_outcome() -> None:
    assert trade_key("mlb-ari-atl-2026-08-22", "Arizona Diamondbacks") == (
        "mlb-ari-atl-2026-08-22|arizona diamondbacks"
    )


def test_claim_true() -> None:
    client = ConvexTradeClient(http_url="https://example.convex.site", token="secret")
    with patch("polymaker.trading.convex_trades.requests.post") as post:
        post.return_value = MagicMock(status_code=200, json=lambda: {"ok": True, "claimed": True})
        result = client.claim(
            trade_key_value="mlb-x|yes",
            league="MLB",
            source="sharp_money",
            matchup="ARI @ ATL",
            side="Arizona Diamondbacks",
            usd=25.0,
            prediction_date="2026-08-22",
        )
    assert result.claimed is True
    post.assert_called_once()
    assert post.call_args.args[0].endswith("/trades/claim")


def test_claim_already_taken() -> None:
    client = ConvexTradeClient(http_url="https://example.convex.site", token="secret")
    with patch("polymaker.trading.convex_trades.requests.post") as post:
        post.return_value = MagicMock(
            status_code=200, json=lambda: {"ok": True, "claimed": False}
        )
        result = client.claim(
            trade_key_value="mlb-x|yes",
            league="MLB",
            source="whale_trade",
            matchup="ARI @ ATL",
            side="Arizona Diamondbacks",
            usd=10.0,
            prediction_date="2026-08-22",
        )
    assert result.claimed is False
    assert "already traded" in result.detail


def test_claim_fail_closed_on_http_error() -> None:
    client = ConvexTradeClient(http_url="https://example.convex.site", token="secret")
    with patch("polymaker.trading.convex_trades.requests.post") as post:
        post.return_value = MagicMock(status_code=500, text="boom")
        result = client.claim(
            trade_key_value="mlb-x|yes",
            league="MLB",
            source="sharp_money",
            matchup="ARI @ ATL",
            side="Arizona Diamondbacks",
            usd=25.0,
            prediction_date="2026-08-22",
        )
    assert result.claimed is False
    assert "failed" in result.detail.lower()


def test_unconfigured_fail_closed() -> None:
    client = ConvexTradeClient(http_url="", token="")
    result = client.claim(
        trade_key_value="mlb-x|yes",
        league="MLB",
        source="sharp_money",
        matchup="x",
        side="yes",
        usd=1.0,
        prediction_date="2026-08-22",
    )
    assert result.claimed is False
