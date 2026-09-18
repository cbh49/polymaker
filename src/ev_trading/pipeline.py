"""Fetch + compare hooks. Venue clients are stubs until API docs land."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

from ev_trading.config import EvConfig
from ev_trading.edge import cross_venue_rows
from ev_trading.models import CrossVenueRow, Venue, VenueQuote
from ev_trading.venues import CLIENTS
from polymaker.logging import get_logger

log = get_logger("ev.pipeline")


async def fetch_quotes(
    cfg: EvConfig,
    *,
    query: str | None = None,
    venues: tuple[Venue, ...] | None = None,
) -> list[VenueQuote]:
    wanted = venues or cfg.venue_list
    quotes: list[VenueQuote] = []
    for venue in wanted:
        factory = CLIENTS.get(venue)
        if factory is None:
            log.warning("unknown_venue", venue=venue.value)
            continue
        client = factory()
        try:
            batch = await client.fetch_quotes(query=query, limit=cfg.fetch_limit)
        except Exception as exc:
            log.warning("venue_fetch_failed", venue=venue.value, err=str(exc))
            batch = []
        finally:
            await client.aclose()
        log.info("venue_fetch", venue=venue.value, n=len(batch))
        quotes.extend(batch)
    return quotes


def write_snapshot(
    quotes: list[VenueQuote],
    rows: list[CrossVenueRow],
    out_dir: Path,
) -> tuple[Path, Path]:
    out_dir.mkdir(parents=True, exist_ok=True)
    quotes_path = out_dir / "quotes.json"
    compare_path = out_dir / "compare.json"
    quotes_path.write_text(
        json.dumps([q.to_dict() for q in quotes], indent=2) + "\n",
        encoding="utf-8",
    )
    compare_path.write_text(
        json.dumps([r.to_dict() for r in rows], indent=2) + "\n",
        encoding="utf-8",
    )
    return quotes_path, compare_path


async def run_once(cfg: EvConfig, *, query: str | None = None) -> dict[str, int]:
    quotes = await fetch_quotes(cfg, query=query)
    rows = cross_venue_rows(quotes, min_spread=cfg.min_spread)
    quotes_path, compare_path = write_snapshot(quotes, rows, Path(cfg.out_dir))
    log.info(
        "ev_snapshot",
        quotes=len(quotes),
        matches=len(rows),
        quotes_path=str(quotes_path),
        compare_path=str(compare_path),
    )
    return {"quotes": len(quotes), "matches": len(rows)}


def run_sync(cfg: EvConfig | None = None, *, query: str | None = None) -> dict[str, int]:
    return asyncio.run(run_once(cfg or EvConfig.load(), query=query))
