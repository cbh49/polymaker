"""Refresh sportsbook + Kalshi + Polymarket NFL odds, aggregate, then score +EV.

The RotoWire / Kalshi / Polymarket pulls do not depend on each other, so they
run in parallel. Aggregation and the fair-value report wait until those finish.
"""

from __future__ import annotations

import importlib.util
import json
import sys
import time
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from ev_trading.fair_value.config import FairValueConfig
from ev_trading.fair_value.pipeline import run_fair_value
from ev_trading.fair_value.report import FairValueReport, render_report

_ROOT = Path(__file__).resolve().parents[2]
_SUPPORT = Path(__file__).resolve().parent / "supporting-lines"
_KALSHI = Path(__file__).resolve().parent / "venues" / "kalshi" / "kalshi_nfl.py"
_POLY = _ROOT / "data-aggregation" / "scrape_polymarket_odds.py"
_AGG = _SUPPORT / "aggregate_nfl_odds.py"

GAME_ODDS_OUT = _SUPPORT / "nfl_game_odds.json"
PLAYER_PROPS_OUT = _SUPPORT / "nfl_player_props.json"
KALSHI_OUT = Path(__file__).resolve().parent / "venues" / "kalshi" / "nfl_week_markets.json"
POLY_OUT = _ROOT / "data-aggregation" / "output" / "polymarket_nfl_odds.json"
AGGREGATED_OUT = _SUPPORT / "nfl_aggregated_odds.json"
REPORT_OUT = _ROOT / "output" / "ev" / "nfl_fair_value.json"


@dataclass(frozen=True, slots=True)
class RefreshResult:
    aggregated: Path
    report: FairValueReport | None
    report_path: Path | None
    elapsed_s: float


def _load_script(path: Path, name: str) -> Any:
    spec = importlib.util.spec_from_file_location(name, path)
    if spec is None or spec.loader is None:
        raise ImportError(f"Cannot load {path}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


def _timed(label: str, fn: Callable[[], Any], log: Callable[[str], None]) -> Any:
    log(f"→ {label}")
    start = time.perf_counter()
    try:
        result = fn()
    except Exception as exc:
        log(f"✗ {label} failed after {time.perf_counter() - start:.1f}s: {exc}")
        raise
    log(f"✓ {label} ({time.perf_counter() - start:.1f}s)")
    return result


def refresh_rotowire_games() -> Path:
    return Path(_load_script(_SUPPORT / "scrape_nfl_odds.py", "scrape_nfl_odds").main())


def refresh_rotowire_props() -> Path:
    return Path(
        _load_script(_SUPPORT / "scrape_nfl_prop_lines.py", "scrape_nfl_prop_lines").main()
    )


def refresh_kalshi() -> Path:
    from ev_trading.venues.kalshi.kalshi_nfl import main as kalshi_main

    kalshi_main()
    return KALSHI_OUT


def refresh_polymarket() -> Path:
    poly = _load_script(_POLY, "scrape_polymarket_odds")
    result = poly.scrape(league="NFL")
    out = Path(poly.DEFAULT_OUT["NFL"])
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(result, indent=2) + "\n", encoding="utf-8")
    n_props = sum(len(g.get("player_props") or []) for g in result.get("games") or [])
    print(f"Wrote {result.get('game_count', 0)} NFL Polymarket games ({n_props} player props) → {out}")
    return out


def aggregate_odds() -> Path:
    agg = _load_script(_AGG, "aggregate_nfl_odds")
    result = agg.aggregate_nfl_slate(
        agg._load_json(GAME_ODDS_OUT),
        agg._load_json(PLAYER_PROPS_OUT),
        agg._load_json(KALSHI_OUT),
        agg._load_json(POLY_OUT),
    )
    AGGREGATED_OUT.parent.mkdir(parents=True, exist_ok=True)
    AGGREGATED_OUT.write_text(json.dumps(result, indent=2) + "\n", encoding="utf-8")
    stats = result["prop_stats"]
    print(
        f"Joined {result['game_count']} games → {AGGREGATED_OUT}  |  "
        f"props books={stats['with_books']} kalshi={stats['kalshi']} "
        f"polymarket={stats['polymarket']}  |  "
        f"kalshi skipped (off-slate)={result['kalshi_games_skipped']}"
    )
    return AGGREGATED_OUT


def _run_jobs(
    jobs: list[tuple[str, Callable[[], Any]]],
    *,
    parallel: bool,
    log: Callable[[str], None],
) -> None:
    if not jobs:
        return
    if not parallel or len(jobs) == 1:
        for label, fn in jobs:
            _timed(label, fn, log)
        return
    log(f"Refreshing {len(jobs)} sources in parallel…")
    with ThreadPoolExecutor(max_workers=len(jobs)) as pool:
        futures = {pool.submit(_timed, label, fn, log): label for label, fn in jobs}
        errors: list[BaseException] = []
        for fut in as_completed(futures):
            try:
                fut.result()
            except Exception as exc:
                errors.append(exc)
        if errors:
            raise RuntimeError(
                "odds refresh failed: " + "; ".join(f"{type(e).__name__}: {e}" for e in errors)
            ) from errors[0]


def run_nfl_refresh(
    *,
    sportsbooks: bool = True,
    kalshi: bool = True,
    polymarket: bool = True,
    aggregate: bool = True,
    report: bool = True,
    parallel: bool = True,
    config_path: str | Path | None = None,
    report_out: str | Path | None = None,
    quiet: bool = False,
    log: Callable[[str], None] | None = None,
) -> RefreshResult:
    """Pull live odds, rebuild the aggregated JSON, and optionally print +EV."""
    emit = log or print
    t0 = time.perf_counter()
    jobs: list[tuple[str, Callable[[], Any]]] = []
    if sportsbooks:
        jobs.append(("RotoWire game odds", refresh_rotowire_games))
        jobs.append(("RotoWire player props", refresh_rotowire_props))
    if kalshi:
        jobs.append(("Kalshi NFL week markets", refresh_kalshi))
    if polymarket:
        jobs.append(("Polymarket NFL odds", refresh_polymarket))
    _run_jobs(jobs, parallel=parallel, log=emit)

    aggregated = AGGREGATED_OUT
    if aggregate:
        aggregated = _timed("aggregate sportsbooks + Kalshi + Polymarket", aggregate_odds, emit)
    elif not aggregated.exists():
        raise FileNotFoundError(f"missing aggregated odds: {aggregated}")

    fv_report: FairValueReport | None = None
    out_path: Path | None = None
    if report:
        cfg = FairValueConfig.load(config_path)
        out_path = Path(report_out) if report_out is not None else REPORT_OUT
        fv_report = _timed(
            "fair-value EV report",
            lambda: run_fair_value(aggregated, cfg=cfg, out_path=out_path),
            emit,
        )
        if not quiet:
            render_report(fv_report)
        emit(
            f"tradable={len(fv_report.tradable)} "
            f"low_liq={len(fv_report.tradable_low_liquidity)} "
            f"td={len(fv_report.tradable_td)} "
            f"td_low_liq={len(fv_report.tradable_td_low_liquidity)} "
            f"info={len(fv_report.informational)} → {out_path}"
        )

    return RefreshResult(
        aggregated=aggregated,
        report=fv_report,
        report_path=out_path,
        elapsed_s=time.perf_counter() - t0,
    )
