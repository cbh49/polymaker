"""No-network smoke test for the NFL refresh orchestrator."""

from __future__ import annotations

from ev_trading.nfl_refresh import AGGREGATED_OUT, run_nfl_refresh


def test_refresh_report_only_uses_existing_aggregate() -> None:
    result = run_nfl_refresh(
        sportsbooks=False,
        kalshi=False,
        polymarket=False,
        aggregate=False,
        report=True,
        quiet=True,
    )
    assert result.aggregated == AGGREGATED_OUT
    assert result.report is not None
    assert "tradable" in result.report.to_dict()
    assert "tradable_td" in result.report.to_dict()
    assert "informational" in result.report.to_dict()
