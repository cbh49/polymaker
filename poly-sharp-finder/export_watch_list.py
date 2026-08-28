"""
Export today's MLB/WNBA Polymarket moneylines into watch_list.json.

Uses the parent polymaker catalog (optionally refreshes via sports scan) and
merges optional sharp-money annotations from data-aggregation JSON.

Usage (from trading-bot/):
    uv run python poly-sharp-finder/export_watch_list.py
    uv run python poly-sharp-finder/export_watch_list.py --refresh --out poly-sharp-finder/watch_list.json
"""

from __future__ import annotations

import argparse
import asyncio
import json
import re
import sys
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any

from bootstrap import ensure_paths

_ROOT = ensure_paths()

from polymaker.catalog.scanner import ScanConfig, run_scan  # noqa: E402
from polymaker.catalog.sports import (  # noqa: E402
    DEFAULT_PREGAME_BUFFER_MINUTES,
    SPORTS_SERIES_SLUGS,
    event_date_in_window,
    is_moneyline_slug,
    is_pre_game,
    parse_event_date,
)
from polymaker.catalog.store import CatalogStore  # noqa: E402
from polymaker.config import Config  # noqa: E402
from polymaker.domain import MarketMeta  # noqa: E402
from polymaker.trading.sharp import SharpPlay, load_sharp_plays  # noqa: E402
from polymaker.trading.teams import resolve_team  # noqa: E402

_SLUG_RE = re.compile(
    r"^(?P<league>mlb|wnba)-(?P<away>[a-z0-9]+)-(?P<home>[a-z0-9]+)-(?P<ymd>\d{4}-\d{2}-\d{2})$"
)


def _league_from_slug(slug: str) -> str:
    m = _SLUG_RE.match(slug or "")
    return (m.group("league") if m else "mlb").upper()


def _label_from_meta(meta: MarketMeta) -> str:
    yes = meta.tokens[0].outcome
    no = meta.tokens[1].outcome
    return f"{no} vs {yes} ML" if yes and no else (meta.question or meta.slug)


def _event_date_from_slug(slug: str) -> date | None:
    m = _SLUG_RE.match(slug or "")
    if not m:
        return None
    return parse_event_date(m.group("ymd"))


def _fresh_moneylines(
    store: CatalogStore,
    *,
    look_ahead_days: int,
    today: date | None = None,
    limit_per_league: int = 200,
    pregame_buffer_minutes: float = DEFAULT_PREGAME_BUFFER_MINUTES,
    now: datetime | None = None,
) -> list[MarketMeta]:
    today = today or datetime.now(UTC).date()
    clock = now or datetime.now(UTC)
    out: list[MarketMeta] = []
    seen: set[str] = set()
    for prefix in ("mlb-", "wnba-"):
        for meta in store.by_slug_prefix(prefix, limit=limit_per_league):
            if not is_moneyline_slug(meta.slug):
                continue
            ed = _event_date_from_slug(meta.slug)
            if ed is None:
                # fall back to end_date window if slug date missing
                if not event_date_in_window(
                    (meta.end_date_iso or "")[:10] or None,
                    look_ahead_days=look_ahead_days,
                    today=today,
                ):
                    continue
            else:
                end = today + timedelta(days=look_ahead_days)
                if not (today <= ed <= end):
                    continue
            if not is_pre_game(
                {"startTime": meta.start_time_iso},
                pregame_buffer_minutes,
                now=clock,
            ):
                continue
            if meta.condition_id in seen:
                continue
            seen.add(meta.condition_id)
            out.append(meta)
    return out


def _sharp_annotation_map(
    plays: list[SharpPlay],
    markets: list[MarketMeta],
) -> dict[str, dict[str, Any]]:
    """Map condition_id → sharp fields by matching side team to an outcome label."""
    by_cid: dict[str, dict[str, Any]] = {}
    # index markets by league + poly codes in slug
    indexed: list[tuple[MarketMeta, str, str, str]] = []
    for meta in markets:
        m = _SLUG_RE.match(meta.slug or "")
        if not m:
            continue
        indexed.append((meta, m.group("league").upper(), m.group("away"), m.group("home")))

    for play in plays:
        if play.market != "moneyline":
            continue
        side = resolve_team(play.league, play.side)
        if side is None:
            continue
        for meta, league, away, home in indexed:
            if league != play.league.upper():
                continue
            # prefer markets that include this team code
            if side.poly_code not in (away, home):
                continue
            # match outcome label to team
            matched_outcome = None
            for t in meta.tokens:
                label = t.outcome.strip().lower()
                full = side.full_name.strip().lower()
                nick = full.rsplit(" ", 1)[-1]
                if label in (full, nick) or full in label or nick in label.split():
                    matched_outcome = t.outcome
                    break
            if matched_outcome is None:
                continue
            prev = by_cid.get(meta.condition_id)
            # keep highest tier (A > B) / largest gap
            tier = play.tier.upper()
            gap = play.composite_gap
            if prev is not None:
                if prev.get("sharp_tier") == "A" and tier != "A":
                    continue
                if prev.get("sharp_tier") == tier and (
                    gap is None or (prev.get("sharp_composite_gap") or 0) >= gap
                ):
                    continue
            by_cid[meta.condition_id] = {
                "sharp_tier": tier,
                "sharp_side": matched_outcome,
                "sharp_composite_gap": gap,
            }
            break
    return by_cid


def markets_to_watch_rows(
    markets: list[MarketMeta],
    sharp_by_cid: dict[str, dict[str, Any]] | None = None,
) -> list[dict[str, Any]]:
    sharp_by_cid = sharp_by_cid or {}
    rows: list[dict[str, Any]] = []
    for meta in markets:
        ann = sharp_by_cid.get(meta.condition_id, {})
        rows.append(
            {
                "condition_id": meta.condition_id,
                "league": _league_from_slug(meta.slug),
                "label": _label_from_meta(meta),
                "yes_token_id": meta.tokens[0].token_id,
                "no_token_id": meta.tokens[1].token_id,
                "yes_outcome": meta.tokens[0].outcome,
                "no_outcome": meta.tokens[1].outcome,
                "slug": meta.slug,
                "start_time": meta.start_time_iso,
                "sharp_tier": ann.get("sharp_tier"),
                "sharp_side": ann.get("sharp_side"),
                "sharp_composite_gap": ann.get("sharp_composite_gap"),
            }
        )
    rows.sort(key=lambda r: (r["league"], r["slug"] or r["label"]))
    return rows


async def _refresh_catalog(cfg: Config) -> int:
    store = CatalogStore(cfg.paths.db)
    try:
        cat = cfg.catalog
        scan_cfg = ScanConfig(
            include_politics=False,
            series_slugs=tuple(cat.series_slugs) or SPORTS_SERIES_SLUGS,
            look_ahead_days=cat.look_ahead_days,
            skip_live_events=cat.skip_live_events,
            pregame_buffer_minutes=cat.pregame_buffer_minutes,
            min_liquidity=cat.min_liquidity,
            rewards_only=False,
            sports_rewards_only=cat.sports_rewards_only,
            gamma_host=cfg.wallet.gamma_host,
            clob_host=cfg.wallet.clob_host,
        )
        metas = await run_scan(store, scan_cfg)
        return len(metas)
    finally:
        store.close()


def export_watch_list(
    *,
    config_dir: str = "config",
    out_path: Path,
    refresh: bool = False,
    include_sharp: bool = True,
) -> list[dict[str, Any]]:
    cfg = Config.load(config_dir)
    if refresh:
        n = asyncio.run(_refresh_catalog(cfg))
        print(f"Refreshed sports catalog: {n} markets")

    store = CatalogStore(cfg.paths.db)
    try:
        markets = _fresh_moneylines(
            store,
            look_ahead_days=cfg.catalog.look_ahead_days,
            pregame_buffer_minutes=cfg.catalog.pregame_buffer_minutes,
        )
    finally:
        store.close()

    sharp_by_cid: dict[str, dict[str, Any]] = {}
    if include_sharp:
        paths = [
            Path(cfg.sharp.mlb_path),
            Path(cfg.sharp.wnba_path),
            Path(cfg.sharp.ufc_path),
            Path(cfg.sharp.ncaaf_path),
        ]
        existing = [p for p in paths if p.is_file()]
        if existing:
            plays = load_sharp_plays(existing)
            sharp_by_cid = _sharp_annotation_map(plays, markets)
            print(f"Merged sharp annotations for {len(sharp_by_cid)} markets "
                  f"({len(plays)} plays loaded)")

    rows = markets_to_watch_rows(markets, sharp_by_cid)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(rows, indent=2) + "\n", encoding="utf-8")
    print(f"Wrote {len(rows)} markets → {out_path}")
    return rows


def main() -> None:
    parser = argparse.ArgumentParser(description="Export Polymarket watch list for poly-sharp-finder")
    parser.add_argument("--config-dir", default="config", help="polymaker config directory")
    parser.add_argument(
        "--out",
        default="poly-sharp-finder/watch_list.json",
        help="output JSON path",
    )
    parser.add_argument("--refresh", action="store_true", help="refresh sports catalog first")
    parser.add_argument("--no-sharp", action="store_true", help="skip sharp-money annotations")
    args = parser.parse_args()

    # Resolve relative to trading-bot root when cwd differs
    out = Path(args.out)
    if not out.is_absolute():
        out = _ROOT / out

    config_dir = args.config_dir
    cfg_path = Path(config_dir)
    if not cfg_path.is_absolute() and not cfg_path.exists():
        alt = _ROOT / config_dir
        if alt.exists():
            config_dir = str(alt)

    rows = export_watch_list(
        config_dir=config_dir,
        out_path=out,
        refresh=args.refresh,
        include_sharp=not args.no_sharp,
    )
    if not rows:
        print(
            "WARNING: empty watch list. Run with --refresh or `uv run polymaker scan` first.",
            file=sys.stderr,
        )
        raise SystemExit(1)


if __name__ == "__main__":
    main()
