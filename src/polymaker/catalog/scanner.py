"""The scanner: sweep Gamma for political + sports markets, score, persist.

Replaces the v1 data_updater (hour-long crawl of every order book, written to
Google Sheets). A politics-filtered sweep here is seconds; sports series
(MLB/WNBA) are fetched via `/events?series_slug=` and filtered to moneylines
in a short look-ahead window.
"""

from __future__ import annotations

from dataclasses import dataclass

from polymaker.catalog.gamma import (
    POLITICS_TAG_SLUG,
    GammaClient,
    fetch_reward_rates,
    parse_market,
)
from polymaker.catalog.scoring import score_market
from polymaker.catalog.sports import (
    DEFAULT_PREGAME_BUFFER_MINUTES,
    SPORTS_SERIES_SLUGS,
    event_date_in_window,
    pick_moneyline_market,
    should_skip_live_event,
)
from polymaker.catalog.store import CatalogStore
from polymaker.domain import MarketMeta
from polymaker.logging import get_logger

log = get_logger("catalog.scanner")


@dataclass(frozen=True, slots=True)
class ScanConfig:
    tag_slug: str = POLITICS_TAG_SLUG
    include_politics: bool = True
    # Empty tuple = skip sports discovery; default registers mlb + wnba.
    series_slugs: tuple[str, ...] = SPORTS_SERIES_SLUGS
    look_ahead_days: int = 3
    skip_live_events: bool = True
    pregame_buffer_minutes: float = DEFAULT_PREGAME_BUFFER_MINUTES
    min_liquidity: float = 1000.0
    min_volume_24hr: float = 0.0
    rewards_only: bool = True  # politics path: keep only liquidity-rewards markets
    # Sports moneylines are discovered for trading eligibility even without a
    # daily reward pool; set True to apply the same rewards gate as politics.
    sports_rewards_only: bool = False
    gamma_host: str = "https://gamma-api.polymarket.com"
    clob_host: str = "https://clob.polymarket.com"


async def run_scan(store: CatalogStore, cfg: ScanConfig) -> list[MarketMeta]:
    """Fetch, parse, filter, score, and persist. Returns the kept markets."""
    reward_rates = await fetch_reward_rates(cfg.clob_host)
    log.info("reward_rates_loaded", n=len(reward_rates))

    kept: list[MarketMeta] = []
    async with GammaClient(cfg.gamma_host) as gamma:
        if cfg.include_politics:
            kept.extend(await _scan_politics(gamma, store, cfg, reward_rates))
        for series in cfg.series_slugs:
            kept.extend(await _scan_sports_series(gamma, cfg, reward_rates, series))

    for m in kept:
        store.upsert_market(m, score_market(m))
    log.info(
        "scan_complete",
        kept=len(kept),
        politics=cfg.include_politics,
        series=list(cfg.series_slugs),
    )
    return kept


async def _scan_politics(
    gamma: GammaClient,
    store: CatalogStore,
    cfg: ScanConfig,
    reward_rates: dict[str, float],
) -> list[MarketMeta]:
    tag_id = store.cached_tag(cfg.tag_slug) or await gamma.resolve_tag_id(cfg.tag_slug)
    if tag_id:
        store.cache_tag(cfg.tag_slug, tag_id)

    kept: list[MarketMeta] = []
    seen = 0
    async for raw in gamma.iter_markets(
        tag_id=tag_id,
        min_liquidity=cfg.min_liquidity,
        min_volume_24hr=cfg.min_volume_24hr,
    ):
        seen += 1
        meta = parse_market(raw, reward_rates)
        if meta is None:
            continue
        if cfg.rewards_only and meta.rewards_daily_rate <= 0:
            continue
        kept.append(meta)
    log.info("politics_scan_complete", seen=seen, kept=len(kept), tag=cfg.tag_slug)
    return kept


async def _scan_sports_series(
    gamma: GammaClient,
    cfg: ScanConfig,
    reward_rates: dict[str, float],
    series_slug: str,
) -> list[MarketMeta]:
    kept: list[MarketMeta] = []
    seen = 0
    skipped_live = 0
    skipped_window = 0
    async for event in gamma.iter_events(series_slug=series_slug):
        seen += 1
        if not event_date_in_window(event.get("eventDate"), look_ahead_days=cfg.look_ahead_days):
            skipped_window += 1
            continue
        if should_skip_live_event(
            event,
            skip_live=cfg.skip_live_events,
            buffer_minutes=cfg.pregame_buffer_minutes,
        ):
            skipped_live += 1
            continue
        raw = pick_moneyline_market(event)
        if raw is None:
            continue
        # Attach the parent event so parse_market can set event_id, and so any
        # downstream live checks see series/live fields on nested events.
        if not raw.get("events"):
            raw = {**raw, "events": [event]}
        meta = parse_market(raw, reward_rates)
        if meta is None:
            continue
        if cfg.min_liquidity > 0 and meta.liquidity_num < cfg.min_liquidity:
            continue
        if cfg.min_volume_24hr > 0 and meta.volume_24hr < cfg.min_volume_24hr:
            continue
        if cfg.sports_rewards_only and meta.rewards_daily_rate <= 0:
            continue
        kept.append(meta)
    log.info(
        "sports_scan_complete",
        series=series_slug,
        seen=seen,
        kept=len(kept),
        skipped_live=skipped_live,
        skipped_window=skipped_window,
    )
    return kept
